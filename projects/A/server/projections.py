"""Read-only account-scoped projections of persistent, verified facts."""
import json
from datetime import date,datetime,timezone,timedelta
from decimal import Decimal
from fractions import Fraction
from .common import AppError
from .db import digest
from .local_deletion import guarded,sql_visible
from .identity import IdentityService
from .profile import ProfileService
from .groups import GroupService
from .dates import DateService
from .relations import RelationService,install_schema as install_relations
from .sync.scalar import ScalarStore
from .rules.daily import BJ,maintenance,occurred_day
from .rules.reply import reply_segments,route,next_action
from .rules.spark import spark
from .rules.overview import overview
from .rules.tier import tier,explain_tier
from .rules.opportunity import opportunity
from .rules.recent import aggregate_days,increases,declines
from .rules.slots import time_bins
from .v1_live_profile import V1LiveProfile

def install_schema(db):
    install_relations(db)
    with db.transaction() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS spark_observation(
          account_key TEXT NOT NULL,person_key TEXT NOT NULL,observation_id TEXT NOT NULL,
          source_id TEXT NOT NULL,source_version TEXT NOT NULL,version INTEGER NOT NULL,
          state_day TEXT NOT NULL,state TEXT NOT NULL,special INTEGER NOT NULL,semantic_digest TEXT NOT NULL,event_id TEXT NOT NULL,
          PRIMARY KEY(account_key,person_key,source_id,observation_id))''')
        prior=conn.execute("SELECT value FROM app_meta WHERE key='cd_schema_version'").fetchone()
        if prior and prior[0]!='1': raise AppError(409,'CD_SCHEMA_INCOMPATIBLE','Explicit compatible rules migration required.')
        conn.execute("INSERT OR IGNORE INTO app_meta VALUES('cd_schema_version','1')")

def _parse(text):
    return datetime.fromisoformat(text.replace('Z','+00:00')) if text else None

def _rank_time_key(text):
    return _parse(text) if text else datetime.min.replace(tzinfo=timezone.utc)

def json_ready(value):
    if isinstance(value,(date,datetime)): return value.isoformat()
    if isinstance(value,(Fraction,Decimal)): return str(value)
    if isinstance(value,dict): return {str(k):json_ready(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [json_ready(v) for v in value]
    return value

class ProjectionService:
    def __init__(self,db,collection=None):
        self.db=db;self.profiles=ProfileService(db);self.identity=IdentityService(db)
        with db.lock: version=db.conn.execute("SELECT value FROM app_meta WHERE key='cd_schema_version'").fetchone()
        if not version or version[0]!='1': raise AppError(409,'CD_NOT_INITIALIZED','Explicit phase C/D initialization required.')
        self.groups=GroupService(db);self.dates=DateService(db);self.relations=RelationService(db);self.collection=collection;self.live_profile=V1LiveProfile(db)

    @guarded
    def group_directory(self,account,*,now=None):
        """Finite group/contact directory; no chat or note bodies are returned."""
        now=now or datetime.now(timezone.utc)
        with self.db.lock:
            owned=not self.db.conn.in_transaction
            if owned:self.db.conn.execute('BEGIN')
            try:
                definitions=self.groups.definitions(account)['items']
                items=[]
                for contact in self.profiles.contacts(account)['items']:
                    person=contact['person_key']
                    clip=self.profiles.scalars.get(account,person,'formal_tag.clip')['effective_value']
                    rank=self._rankings(account,person,now)
                    grade=tier(rank.get('contribution'),rank.get('comments'),rank.get('watching'),clip,
                               windows=rank.get('windows',(None,None,None)),identity=bool(contact['verified']))
                    groups=self.groups.groups_for(account,person,grade)
                    items.append({k:contact[k] for k in ('account_key','person_key','display_name','object_kind','verified')}|
                                 {k:groups[k] for k in ('system','custom','conflicts')})
                return {'format':'group-directory-1','account_key':account,'as_of_day':now.astimezone(BJ).date().isoformat(),
                        'definitions':definitions,'items':items}
            finally:
                if owned:self.db.conn.execute('ROLLBACK')

    @guarded
    def _messages(self,account,person):
        with self.db.lock:
            rows=[dict(r) for r in self.db.conn.execute('SELECT m.*,c.kind AS conversation_kind,r.body FROM message m JOIN conversation c ON c.conversation_key=m.conversation_key JOIN message_revision r ON r.message_key=m.message_key AND r.revision=m.active_body_revision WHERE m.account_key=? AND m.person_key=? AND '+sql_visible(self.db)+' ORDER BY m.occurred_lower_utc,m.message_key',(account,person))]
            from .derived_import import check_message
            for row in rows:check_message(self.db,row)
        events=[]
        for row in rows:
            lower,upper=_parse(row['occurred_lower_utc']),_parse(row['occurred_upper_utc'])
            exact=lower is not None and lower==upper and row['time_precision'] in ('second','millisecond','exact')
            quality=json.loads(row['quality_json'])
            events.append({'uid':person,'at':lower if exact else None,'occurred_lower':lower,'occurred_upper':upper,'direction':row['direction'],'kind':'personal' if row['conversation_kind']=='person' and row['kind'] not in ('system','unknown') else 'group','success':row['success_state']=='confirmed','identity_verified':row['identity_state']=='exact' and quality.get('identity')=='verified','msgid':row['message_key'],'body':row['body'],'precision':row['time_precision'],'reply_eligible':all(quality.get(k)=='verified' for k in ('body','direction','success','time'))})
        return events

    def _coverage(self,account,person):
        # Coverage must be a qualified full-conversation interval. A cached zero is insufficient.
        with self.db.lock:
            rows=self.db.conn.execute('SELECT cs.*,c.person_key FROM coverage_segment cs JOIN conversation c ON cs.conversation_key=c.conversation_key WHERE cs.account_key=? AND c.person_key=? ORDER BY cs.observed_at_utc DESC',(account,person)).fetchall()
        seen=set()
        for row in rows:
            source=row['source_id']
            if source in seen: continue
            seen.add(source)
            payload=json.loads(row['coverage_json'])
            if self.identity.trusted(row['source_id'],row['source_version'],'message_coverage',account) and payload.get('state')=='complete' and payload.get('scope')=='full_conversation' and payload.get('no_gaps') is True:
                return True
        return False

    def _rankings(self,account,person,now):
        # Collection projections cannot bypass persisted source/day validation.
        with self.db.lock:
            # Period choice, source checks and rows must share a read snapshot;
            # another connection can otherwise replace a period between reads.
            owned=not self.db.conn.in_transaction
            if owned:self.db.conn.execute('BEGIN')
            try:
                return self._stored_rankings(account,person,now)
            finally:
                if owned:self.db.conn.execute('ROLLBACK')

    def _stored_rankings(self,account,person,now):
        from .rank_facts import RankFacts,thirty_day_period
        ranks=RankFacts(self.db);day=now.astimezone(BJ).date().isoformat()
        periods=[p for p in ranks.periods(account,now=now) if _parse(p['effective_cutoff'])<=now]
        current=[p for p in periods if p.get('period_basis')=='rolling_30d' and p.get('rank_as_of_day')==day]
        result=self._rank_period_projection(ranks,account,person,current,day,now=now)
        windows=result['windows'];ready=all(w is not None for w in windows) and len(set(windows))==1
        result['freshness']={'format':'rolling-rank-state-1','required_day':day,'state':'ready' if ready else 'today_pending',
                             'available_kinds':[r['kind'] for r in result['items']]}
        # Old fixed-period facts remain visible, but never supply today's tier.
        if not current:
            result['items']=self._rank_period_projection(ranks,account,person,periods,day,now=now)['items']
        ended=[p for p in periods if (p.get('rank_as_of_day') is None or p['rank_as_of_day']<=day)
               and thirty_day_period(p)]
        if ended:
            # Anchor on the newest declared day/window, never fill a missing kind
            # from an older complete trio. This reference does not drive routes.
            key=lambda p:(p.get('rank_as_of_day') or '',_rank_time_key(p['period_end_utc']),_rank_time_key(p['period_start_utc']),p['period_key'])
            window=max(key(p) for p in ended);selected=[p for p in ended if key(p)==window]
            saved=self._rank_period_projection(ranks,account,person,selected,day,now=now)
            if saved['windows']!=windows:result['saved_snapshot']=saved
        return result

    def _rank_period_projection(self,ranks,account,person,periods,day,*,now=None):
        from .rank_facts import thirty_day_period
        items=[];positions={};windows=[]
        for kind,key in (('contribution','contribution'),('comment','comments'),('watch','watching')):
            current=[p for p in periods if p['rank_kind']==kind]
            if not current:
                positions[key]=None;windows.append(None);continue
            period=max(current,key=lambda p:(_rank_time_key(p['period_start_utc']),_parse(p['effective_cutoff'])))
            snapshot=ranks.get_active(account,kind,period['period_key'],now=now);row=next((r for r in snapshot['items'] if r['person_key']==person),None)
            # Missing from a short list cannot establish being outside the required top 100.
            observed_positions={r['position'] for r in snapshot['items']}
            outside_top100=snapshot.get('state')=='complete' and set(range(1,101))<=observed_positions
            thirty_day=snapshot['state'] in ('complete','observed') and thirty_day_period(period)
            positions[key]=(row['position'] if row else 101 if outside_top100 else None) if thirty_day else None
            windows.append((period.get('rank_as_of_day'),period['period_key'],_parse(period['period_start_utc']),_parse(period['period_end_utc']),period.get('adoption',{}).get('time_basis'),period.get('adoption',{}).get('statistics_through_day')) if thirty_day else None)
            items.append({'kind':kind,'position':row['position'] if row else None,'metric':row['metric'] if row else {'state':'unknown','value':None},'period_key':period['period_key'],'period_start_utc':period['period_start_utc'],'period_end_utc':period['period_end_utc'],'period_label':snapshot.get('period_label'),'effective_cutoff':period['effective_cutoff'],'revision_id':period['revision_id'],'coverage':snapshot['coverage'],'tier_period_eligible':thirty_day,'outside_top100_verified':outside_top100 and row is None,
                          'period_basis':period.get('period_basis'),'rank_as_of_day':period.get('rank_as_of_day'),'tier_day_eligible':period.get('period_basis')=='rolling_30d' and period.get('rank_as_of_day')==day,**({'adoption':snapshot['adoption']} if 'adoption' in snapshot else {})})
        return {**positions,'windows':tuple(windows),'items':items}

    @staticmethod
    def _tier_details(rank,clip,identity):
        def explain(value):
            return explain_tier(value.get('contribution'),value.get('comments'),value.get('watching'),clip,
                                windows=value.get('windows',(None,None,None)),identity=identity)
        saved=rank.get('saved_snapshot')
        return {'tier_explanation':explain(rank),'tier_freshness':rank.get('freshness'),'saved_tier_reference':
                {'scope':'saved_snapshot','explanation':explain(saved),'rankings':saved.get('items',[])} if saved else None}

    def _session_projection(self,account,person,now,messages):
        with self.db.lock:
            # Catalog completeness, session rows and source checks must describe
            # one database view, including during cross-connection withdrawal.
            owned=not self.db.conn.in_transaction
            if owned:self.db.conn.execute('BEGIN')
            try:
                return self._stored_session_projection(account,person,now,messages)
            finally:
                if owned:self.db.conn.execute('ROLLBACK')

    def _stored_session_projection(self,account,person,now,messages):
        from .session_facts import SessionFacts
        store=SessionFacts(self.db);catalog=store.catalog(account);payloads=store.list_active(account)
        sessions=[];facts={};entries=[];unknown=[];captured=[];support_units={};opportunity_order_known=True
        for p in payloads:
            lower,upper=_parse(p['started_lower_utc']),_parse(p['started_upper_utc']);endlo,endhi=_parse(p['ended_lower_utc']),_parse(p['ended_upper_utc'])
            qualified=p.get('formal') is True and self.identity.trusted(p['source_id'],p['source_version'],'session_formal',account)
            if p.get('formal') is False: continue
            if not qualified or lower is None or upper is None or endlo is None or endhi is None or lower.astimezone(BJ).date()!=upper.astimezone(BJ).date(): unknown.append(p['session_key']);continue
            exact=lower==upper and endlo==endhi
            if now.astimezone(BJ).date()-timedelta(days=6)<=endhi.astimezone(BJ).date()<=now.astimezone(BJ).date() and endlo!=endhi:
                opportunity_order_known=False
            sessions.append({'id':p['session_key'],'formal':qualified,'ended':p['ended_official'],'start':lower,'end':endhi,'duration_seconds':int((endlo-lower).total_seconds()) if exact else 0,'duration_known':exact})
            captured.append(_parse(p['captured_at_utc']))
            viewer=next((v for v in p['viewers'] if v['person_key']==person),None);metrics=viewer['metrics'] if viewer else {}
            if metrics.get('contribution',{}).get('state')=='known': support_units[p['session_key']]=metrics['contribution']['unit']
            row={}
            for source,target in (('contribution','support'),('watch_seconds','watch_seconds'),('comment_count','comments')):
                m=metrics.get(source,{});known=m.get('state')=='known' and m.get('precision')=='exact';value=Decimal(m['value']) if known else None
                row[target]=int(value) if value is not None and value==value.to_integral_value() else value
                row[target+'_complete']=known and p['metric_coverage'][source]=='complete'
            arrival=metrics.get('attendance',{});row['arrival']=arrival.get('value')=='1' if arrival.get('state')=='known' and arrival.get('precision')=='exact' and p['metric_coverage']['attendance']=='complete' else None
            pk=metrics.get('pk_contribution',{});row['pk_verified']=pk.get('state')=='known' and pk.get('precision')=='exact' and p['metric_coverage']['pk_contribution']=='complete';row['pk_value']=Decimal(pk['value']) if row['pk_verified'] else 0
            facts[(p['session_key'],person)]=row
            if viewer:
                entries.extend({'source':'entry','at':_parse(e),'identity':True,'reliable':True} for e in viewer['entry_times_utc'])
        directory=catalog.get('state')=='complete' and not catalog.get('missing_details') and not unknown
        catalog_keys=set(catalog.get('session_keys',[]))
        start=datetime.combine(now.astimezone(BJ).date()-timedelta(days=6),datetime.min.time(),BJ)
        # Keep observed facts, but an enumerated catalog cannot certify omitted
        # sessions. Match each existing rule's window before its sample limit.
        opportunity_members=all(s['id'] in catalog_keys for s in sessions if s['ended'] and start<=s['end']<=now)
        opportunity_directory=directory and opportunity_order_known and opportunity_members and _parse(catalog.get('window_start_utc'))<=start and _parse(catalog.get('window_end_utc'))>=now
        opportunity_result=opportunity(person,sessions,facts,now,opportunity_directory)
        recent={'state':'unknown','reason':'qualified_session_windows_required'}
        if captured:
            collection_day=max(captured).astimezone(BJ).date()
            window_start=datetime.combine(collection_day-timedelta(days=14),datetime.min.time(),BJ);window_end=datetime.combine(collection_day,datetime.min.time(),BJ)
            comparison_sessions=[s for s in sessions if window_start<=s['start']<window_end]
            # Only values consumed by these two windows can conflict. Exact
            # partial values still contribute and must participate in this gate.
            units={support_units[s['id']] for s in comparison_sessions if facts[(s['id'],person)]['support'] is not None}
            if len(units)>1:
                for s in comparison_sessions:
                    row=facts[(s['id'],person)];row['support']=None;row['support_complete']=False
            current=aggregate_days(sessions,facts,person,collection_day);previous=aggregate_days(sessions,facts,person,collection_day-timedelta(days=7))
            comparison_members=all(s['id'] in catalog_keys for s in comparison_sessions)
            comparable=directory and comparison_members and _parse(catalog.get('window_start_utc'))<=window_start and _parse(catalog.get('window_end_utc'))>=window_end
            if not comparable:
                current['complete']={k:False for k in current['complete']};previous['complete']={k:False for k in previous['complete']};current['arrival_ratio']=None;previous['arrival_ratio']=None
            duration_known=all(s['duration_known'] for s in comparison_sessions)
            prev_decline={**previous,'complete':dict(previous['complete'])};cur_decline={**current,'complete':dict(current['complete'])}
            if not duration_known: prev_decline['complete']['support']=False;cur_decline['complete']['support']=False
            observation=declines(prev_decline,cur_decline,identity=True,directory_complete=comparable)
            recent={'state':'known' if comparable else 'partial','effective_collection_day':collection_day,
                    'captured_at_utc':max(captured),'window_basis':'session_start_day',
                    'support_unit':next(iter(units)) if len(units)==1 else None,
                    'current':current,'previous':previous,'increases':increases(previous,current),'declines':observation,'unknown_sessions':unknown}
        incoming=[{'source':'incoming','at':m['at'],'direction':m['direction'],'identity':m['identity_verified'],'reliable':m['kind']=='personal'} for m in messages]
        return recent,time_bins(incoming+entries,now.astimezone(BJ).date()),opportunity_result

    @guarded
    def native_profile_card(self,account,person,*,now=None):
        """Finite saved profile/live/rank card. Never fetch messages or free text."""
        now=now or datetime.now(timezone.utc)
        contact=self.profiles._contact(account,person)
        if contact['object_kind']!='person' or not contact['verified']:
            raise AppError(409,'NATIVE_PROFILE_IDENTITY_UNKNOWN','正式联系人身份尚未核验')
        fields={}
        for name in ('display_name','formal_tag.clip','formal_tag.wechat','formal_tag.maintenance','formal_tag.spark','formal_tag.vip','formal_tag.star','formal_tag.local_time','location'):
            field=self.profiles.scalars.get(account,person,name)
            fields[name]={'state':field['state'],'effective_value':field['effective_value']}
        rank=self._rankings(account,person,now)
        clip=fields['formal_tag.clip']['effective_value']
        grade=tier(rank.get('contribution'),rank.get('comments'),rank.get('watching'),clip,
                   windows=rank.get('windows',(None,None,None)),identity=True)
        # Reuse saved formal-session rules without loading any chat history.
        recent,slots,opportunity=self._session_projection(account,person,now,[])
        # Only derived date states leave this card, not stored evidence text.
        raw_dates=self.dates.project(account,person,now=now)
        date_state=lambda value:{k:v for k,v in value.items() if k in ('status','days','remind','next','basis')}
        dates={'as_of_day':now.astimezone(BJ).date().isoformat(),'calculated_at_utc':now,
               'birthday':date_state(raw_dates['birthday']),'anniversary':date_state(raw_dates['anniversary']),
               'terms':{kind:[date_state(term) for term in raw_dates['terms'][kind]] for kind in ('vip','star')},
               'local_time':raw_dates['local_time']}
        relation=self.relations.get(account,person)
        relations={'directions':relation['directions'],'mutual':relation['mutual'],
                   'changes':[{k:v for k,v in row.items() if k in ('direction','actual_at_utc','discovered_at_utc')} for row in relation['changes'][-20:]]}
        return json_ready({'format':'native-profile-card-1','account_key':account,'person_key':person,
            'display_name':fields['display_name']['effective_value'] or '未记录称呼','object_kind':'person','verified':True,
            'profile':{'fields':fields},'grade':grade,'tier':grade,'rankings':rank.get('items',[]),
            **self._tier_details(rank,clip,True),
            'live':self.live_profile.project(account,person),'recent':recent,'dates':dates,'relations':relations,
            'live_slots':slots,'opportunity':opportunity,
            'message_history':'not_loaded','AI_reference':'not_requested'})

    @guarded
    def sidebar(self,account,person,*,now=None):
        now=now or datetime.now(timezone.utc);today=now.astimezone(BJ).date();profile=self.profiles.get(account,person)
        fields=profile['fields'];value=lambda f:fields.get(f,{}).get('effective_value')
        rank=self._rankings(account,person,now)
        grade=tier(rank.get('contribution'),rank.get('comments'),rank.get('watching'),value('formal_tag.clip'),windows=rank.get('windows',(None,None,None)),identity=bool(profile['verified']))
        messages=self._messages(account,person);segments=reply_segments(messages);coverage=self._coverage(account,person)
        routing=route(segments['pending'],grade,value('formal_tag.wechat') is True)
        maintenance_state=maintenance(person,messages,today,tagged=value('formal_tag.maintenance') is True,complete=coverage)
        observation=self._spark(account,person,today)
        spark_state=spark(person,messages,today,observation['state'],observation['day'],complete=coverage,special=observation['special']) if value('formal_tag.spark') is True else {'state':'stopped','suppress_send':True}
        recent,slots,opportunity_result=self._session_projection(account,person,now,messages)
        dated=[m for m in messages if m['identity_verified'] and m['kind']=='personal' and m['direction'] in ('in','out') and occurred_day(m) is not None]
        earliest=min(dated,key=lambda m:(occurred_day(m),m['msgid'])) if dated else None
        origin={'date':occurred_day(earliest),'body':earliest['body'],'message_key':earliest['msgid']} if earliest else None
        def control(field):
            row=self.profiles.scalars.get(account,person,field)
            return {k:row[k] for k in ('state','effective_value','revision')}
        opportunity_result={**opportunity_result,'control':{'day':today.isoformat(),'time':control('opportunity_time'),'round':control('opportunity_round.'+today.isoformat())}}
        return json_ready({'account_key':account,'person_key':person,'display_name':value('display_name') or '未记录称呼','object_kind':profile['object_kind'],'verified':bool(profile['verified']),'profile':profile,'tier':grade,'grade':grade,**self._tier_details(rank,value('formal_tag.clip'),bool(profile['verified'])),'rankings':rank.get('items',[]),'live':self.live_profile.project(account,person),'groups':self.groups.groups_for(account,person,grade),'dates':self.dates.project(account,person,today=today,now=now,earliest_message=origin,history_complete=coverage),'relations':self.relations.get(account,person),'reply':segments,'pending':segments['pending'] is not None,'route':routing,'coverage':{'state':'complete' if coverage else 'unknown'},'maintenance':maintenance_state,'spark':spark_state,'recent':recent,'slots':slots,'opportunity':opportunity_result})

    @guarded
    def overview(self,account,*,now=None):
        now=now or datetime.now(timezone.utc)
        people=[self.sidebar(account,p['person_key'],now=now) for p in self.profiles.contacts(account)['items'] if p['object_kind']=='person']
        reply=[p['person_key'] for p in people if p['pending'] and p['route']=='douyin'];maint=[p['person_key'] for p in people if p['maintenance']=='pending'];sparks=[p['person_key'] for p in people if p['spark']['state']=='pending'];waiting=[p['person_key'] for p in people if p['spark']['state']=='waiting'];wechat=[p['person_key'] for p in people if p['pending'] and p['route']=='wechat'];unknown=[p['person_key'] for p in people if p['tier']=='unknown' or p['reply']['unknown_time'] or p['maintenance']=='unknown' or p['spark']['state'] in ('unknown','special_unverified')]
        global_s=[p['person_key'] for p in people if p['tier']=='S' and p['pending'] and p['route']=='douyin']
        history_coverage=bool(people) and all(p['coverage']['state']=='complete' for p in people)
        rank_coverage=bool(people) and all(p['tier_freshness']['state']=='ready' and p['tier'] in ('S','A','B','C','D','E') for p in people)
        coverage=history_coverage and rank_coverage
        rows=[{'person_key':p['person_key'],'display_name':p['display_name'],'tier':p['tier'],'route':p['route'],'pending':p['pending'],'maintenance':p['maintenance'],'spark':p['spark'],'reasons':(['reply'] if p['person_key'] in reply else [])+(['maintenance'] if p['person_key'] in maint else [])+(['spark'] if p['person_key'] in sparks else [])} for p in people]
        optional=[]
        now=now or datetime.now(timezone.utc);day=now.astimezone(BJ).date().isoformat()
        for p in people:
            fields=p['profile']['fields'];round_state=self.profiles.scalars.get(account,p['person_key'],'opportunity_round.'+day)['effective_value']
            reminder_time=self.profiles.scalars.get(account,p['person_key'],'opportunity_time')['effective_value'] or '18:30'
            if p['opportunity']['status']=='eligible' and round_state!='canceled' and (round_state=='displayed' or now.astimezone(BJ).strftime('%H:%M')>=reminder_time): optional.append(p['person_key'])
        return {**overview(reply,maint,sparks,waiting,optional,wechat=wechat,unknown=unknown),'global_s':global_s,'coverage_gate':'verified' if coverage else 'verify_coverage','history_coverage_gate':'verified' if history_coverage else 'verify_coverage','rank_day_coverage':'ready' if rank_coverage else 'today_pending','next_action':next_action(global_s,coverage),'people':rows}

    def _spark(self,account,person,today):
        with self.db.lock: rows=[dict(r) for r in self.db.conn.execute('SELECT * FROM spark_observation WHERE account_key=? AND person_key=? AND state_day=? ORDER BY version DESC',(account,person,today.isoformat()))]
        rows=[r for r in rows if self.identity.trusted(r['source_id'],r['source_version'],'spark_state',account)]
        if not rows: return {'state':'unknown','day':None,'special':False}
        source_versions={}
        for row in rows:
            source=(row['source_id'],row['source_version']);source_versions[source]=max(source_versions.get(source,0),row['version'])
        latest=[r for r in rows if r['version']==source_versions[(r['source_id'],r['source_version'])]]
        if len({(r['state'],r['special']) for r in latest})!=1: return {'state':'unknown','day':today,'special':False}
        return {'state':rows[0]['state'],'day':today,'special':bool(rows[0]['special'])}

    @guarded
    def record_spark(self,account,person,payload,*,actor=None):
        with self.db.transaction() as conn:
            self._validate_spark(conn,account,person,payload)
            old=conn.execute('SELECT semantic_digest FROM spark_observation WHERE account_key=? AND person_key=? AND source_id=? AND observation_id=?',(account,person,payload['source_id'],payload['observation_id'])).fetchone()
            if old:
                if old[0]!=digest(payload): raise AppError(409,'SPARK_ID_REUSE','Observation identity was reused.')
                return {'status':'duplicate'}
            event=self.db.append_event(conn,account_key=account,target_key=person,event_type='spark.observation',payload=payload,channel='profile',actor=actor)
            self.apply_spark(conn,json.loads(conn.execute('SELECT event_json FROM domain_event WHERE event_id=?',(event,)).fetchone()[0]))
        return {'status':'accepted'}

    def _validate_spark(self,conn,account,person,p):
        if set(p)!={'observation_id','source_id','source_version','version','state_day','state','special'} or p['state'] not in ('red','blue','gray','unknown') or type(p['version'])is not int or p['version']<1 or type(p['special'])is not bool: raise AppError(422,'INVALID_SPARK','Versioned dated platform spark observation required.')
        try: date.fromisoformat(p['state_day'])
        except (TypeError,ValueError): raise AppError(422,'INVALID_SPARK_DATE','Explicit platform state date required.')
        if not all(isinstance(p[k],str) and p[k] for k in ('observation_id','source_id','source_version')): raise AppError(422,'INVALID_SPARK','Source identity required.')
        if not self.identity.trusted(p['source_id'],p['source_version'],'spark_state',account): raise AppError(403,'SPARK_SOURCE_UNVERIFIED','Qualified spark source required.')
        if not conn.execute("SELECT 1 FROM contact WHERE account_key=? AND person_key=? AND object_kind='person' AND verified=1",(account,person)).fetchone(): raise AppError(409,'SPARK_IDENTITY_UNKNOWN','Verified personal identity required.')

    @guarded
    def apply_spark(self,conn,event):
        p=event['payload'];account,person=event['account_key'],event['target_key'];self._validate_spark(conn,account,person,p)
        old=conn.execute('SELECT semantic_digest FROM spark_observation WHERE account_key=? AND person_key=? AND source_id=? AND observation_id=?',(account,person,p['source_id'],p['observation_id'])).fetchone()
        if old:
            if old[0]!=digest(p): raise AppError(409,'SPARK_ID_REUSE','Observation identity was reused.')
            return
        conn.execute('INSERT INTO spark_observation VALUES(?,?,?,?,?,?,?,?,?,?,?)',(account,person,p['observation_id'],p['source_id'],p['source_version'],p['version'],p['state_day'],p['state'],int(p['special']),digest(p),event['event_id']))

    @guarded
    def cancel_opportunity(self,account,person,day,*,expected_revision,actor):
        date.fromisoformat(day)
        return self.profiles.scalars.set(account,person,'opportunity_round.'+day,'canceled',expected_revision=expected_revision,actor=actor)

    @guarded
    def display_opportunity(self,account,person,day,*,expected_revision,actor):
        date.fromisoformat(day)
        return self.profiles.scalars.set(account,person,'opportunity_round.'+day,'displayed',expected_revision=expected_revision,actor=actor)

    @guarded
    def set_opportunity_time(self,account,person,time,*,expected_revision,actor,now=None):
        now=now or datetime.now(timezone.utc);day=now.astimezone(BJ).date().isoformat()
        state=self.profiles.scalars.get(account,person,'opportunity_round.'+day)['effective_value']
        result=self.profiles.scalars.set(account,person,'opportunity_time',time,expected_revision=expected_revision,actor=actor)
        return {**result,'time_change_effect':'tomorrow' if state in ('canceled','displayed') else 'today','past_time':time<=now.astimezone(BJ).strftime('%H:%M')}

    def register_handlers(self,importer):
        self.relations.register_handlers(importer);importer.register_handler('spark.observation',self.apply_spark)
