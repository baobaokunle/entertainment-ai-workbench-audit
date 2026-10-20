"""Persist observations separately from exact platform facts and ambiguous candidates."""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json

from .common import AppError, canonical_json, utc_now
from .db import stable_key, digest, reject_floats
from .identity import IdentityService, require_text
from .local_deletion import guarded, sql_visible, reject_hidden, observation_selectors
from .rules.daily import BJ


def normalize_time(value):
    if value is None:
        return None
    if not isinstance(value,str) or len(value)>80:
        raise AppError(400,'INVALID_TIME','Occurrence timestamps must be explicit UTC timestamps.')
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds()!=0:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
    except (ValueError,OverflowError):
        raise AppError(400,'INVALID_TIME','Occurrence timestamps must be valid UTC timestamps.')


class MessageFacts:
    def __init__(self, db):
        self.db=db
        self.identities=IdentityService(db)

    @guarded
    def ingest(self, observation, principal=None, *, emit_event=True):
        if not isinstance(observation,dict):
            raise AppError(400,'INVALID_OBSERVATION','Observation must be an object.')
        forbidden={'actor','actor_key','operator_key','role','completed','completion','manual','actor_session','session_id','device_key'}
        if forbidden.intersection(observation):
            raise AppError(400,'CLIENT_AUTHORITY_REJECTED','Observation cannot nominate actor or completion authority.')
        reject_floats(observation)
        for field in ('observation_id','source_id','account_key'):
            require_text(observation.get(field),field,200)
        source_id=observation['source_id']; version=observation.get('source_version','1')
        require_text(version,'source_version',80)
        source=self.identities.get_source(source_id,version)
        if not source:
            raise AppError(409,'SOURCE_NOT_REGISTERED','Observation source must be registered.')
        account=observation['account_key']
        if principal is not None:
            principal.require('observations:write',account)
        body=observation.get('body','')
        if not isinstance(body,str) or len(body.encode('utf-8'))>262144 or '\x00' in body:
            raise AppError(400,'INVALID_BODY','Observation body is invalid or exceeds the limit.')
        # Exported content may already have been transformed by its generator.
        # Its opaque original is preserved, but ordinary collector capabilities
        # never promote this evidence to a verified platform fact. The same
        # authority reduction runs when a paired peer replays the observation.
        exported = 'export_candidate_json' in observation
        if exported:
            original = observation['export_candidate_json']
            if not isinstance(original,str) or len(original.encode('utf-8'))>1048576 or '\x00' in original:
                raise AppError(400,'INVALID_EXPORT_CANDIDATE','Export candidate evidence exceeds the limit or is invalid.')
        direction=observation.get('direction','unknown'); success=observation.get('platform_success','unknown')
        precision=observation.get('time_precision','unknown')
        if direction not in ('in','out','system','unknown') or success not in ('confirmed','failed','pending','unknown') or precision not in ('second','minute','date','unknown'):
            raise AppError(400,'INVALID_OBSERVATION_STATE','Observation state is unsupported.')
        lower=normalize_time(observation.get('occurred_lower_utc')); upper=normalize_time(observation.get('occurred_upper_utc'))
        if (lower is None)!=(upper is None) or (lower is not None and lower>upper) or (lower is None and precision!='unknown') or (lower is not None and precision=='unknown'):
            raise AppError(400,'INVALID_TIME_INTERVAL','Occurrence interval and precision must be consistent.')
        kind=observation.get('kind','text'); require_text(kind,'kind',40)
        platform_key=observation.get('platform_message_key')
        if platform_key is not None:
            require_text(platform_key,'platform_message_key')
        conversation_platform=observation.get('platform_conversation_key')
        if conversation_platform is not None:
            require_text(conversation_platform,'platform_conversation_key')
        correlation=observation.get('platform_correlation_key')
        if correlation is not None:
            require_text(correlation,'platform_correlation_key')
        # Hash every supplied semantic field, including coverage and optional evidence.
        semantic=digest(observation)
        observation_key=stable_key('observation',source_id,version,observation['observation_id'])
        device=principal.device_key if principal is not None else dict(self.db.conn.execute('SELECT key,value FROM app_meta'))['device_key']
        if self.identities.origin_withdrawn(device,account,source_id,version):
            raise AppError(422,'SOURCE_WITHDRAWN','This origin withdrew the source version; delayed evidence cannot restore authority.')
        now=utc_now()
        with self.db.transaction() as conn:
            if self.identities.origin_withdrawn(device,account,source_id,version):
                raise AppError(422,'SOURCE_WITHDRAWN','This origin withdrew the source version.')
            from .derived_import import FIELD, FORMAT, is_derived_source, validate_observation, check_message, fail
            derived = FIELD in observation or is_derived_source(self.db,source_id,version)
            if derived:validate_observation(self.db,observation,device)
            old=conn.execute('SELECT * FROM message_observation WHERE observation_key=?',(observation_key,)).fetchone()
            if old and old['semantic_digest'] != semantic:
                raise AppError(409,'OBSERVATION_ID_REUSED','Observation identifier has a different semantic payload.')
            reject_hidden(self.db,account,observation_key=observation_key,**observation_selectors(observation))
            if old:
                if old['semantic_digest'] != semantic:
                    raise AppError(409,'OBSERVATION_ID_REUSED','Observation identifier has a different semantic payload.')
                if derived:
                    current=conn.execute('SELECT * FROM message WHERE message_key=?',(old['message_key'],)).fetchone()
                    if current is None:fail()
                    check_message(self.db,current)
                return {'status':'duplicate','message_key':old['message_key'],'observation_key':observation_key,'quality':json.loads(old['quality_json']),'committed':True}
            account_row=conn.execute('SELECT verified FROM own_account WHERE account_key=?',(account,)).fetchone()
            if not account_row:
                raise AppError(409,'ACCOUNT_UNKNOWN','Observation account must be registered.')
            identity=self.identities.resolve(account,observation.get('object_kind'),observation.get('identity_kind'),observation.get('identity_value'))
            cap=lambda name:self.identities.trusted(source_id,version,name,account)
            quality={'identity':'verified' if account_row[0] and identity and identity['verified'] else 'pending',
                     'body':'verified' if cap('message_body') else 'pending',
                     'conversation':'verified' if conversation_platform and cap('conversation_identity') else 'pending',
                     'message_identity':'exact' if platform_key and cap('message_identity') else 'ambiguous',
                     'direction':'verified' if direction!='unknown' and cap('message_direction') else 'unknown',
                     'time':'verified' if lower and cap('message_time') else 'pending_time',
                     'success':'verified' if success!='unknown' and cap('message_success') else 'unknown',
                     'actor':'actor_unknown','manual':'manual_unknown','source_kind':source['source_kind']}
            if exported:
                quality.update(identity='pending',body='pending',conversation='pending',
                               message_identity='ambiguous',direction='unknown',
                               time='pending_time',success='unknown',evidence_class='export_file')
            if derived:quality['evidence_class']=FORMAT
            ready=all(quality[key]=='verified' for key in ('identity','body','conversation'))
            message_key=None; status='pending'
            if ready:
                object_kind=identity['object_kind']; person=identity['person_key']
                conversation=stable_key('conversation',account,object_kind,conversation_platform)
                existing=conn.execute('SELECT person_key FROM conversation WHERE conversation_key=?',(conversation,)).fetchone()
                if existing and existing[0]!=person:
                    raise AppError(409,'CONVERSATION_IDENTITY_CONFLICT','Stable conversation has conflicting target identity.')
                conn.execute('INSERT OR IGNORE INTO conversation VALUES (?,?,?,?,?)',(conversation,account,person,object_kind,conversation_platform))
                exact=quality['message_identity']=='exact'
                message_key=stable_key('message',account,conversation,platform_key) if exact else stable_key('candidate',observation_key)
                reject_hidden(self.db,account,message_key=message_key)
                effective_platform=platform_key if exact else None
                effective_direction=direction if quality['direction']=='verified' else 'unknown'
                effective_success=success if quality['success']=='verified' else 'unknown'
                effective_lower,effective_upper=(lower,upper) if quality['time']=='verified' else (None,None)
                effective_precision=precision if effective_lower else 'unknown'
                effective_correlation=correlation if cap('platform_correlation') else None
                prior=conn.execute('SELECT * FROM message WHERE message_key=?',(message_key,)).fetchone()
                if prior and (check_message(self.db,prior) is not None or derived):fail()
                body_hash=hashlib.sha256(body.encode('utf-8')).hexdigest()
                revision=1;active_revision=1;write_revision=True
                if prior:
                    prior_quality=json.loads(prior['quality_json'])
                    active_revision=prior['active_body_revision']
                    prior_body=conn.execute('SELECT body_sha256 FROM message_revision WHERE message_key=? AND revision=?',(message_key,active_revision)).fetchone()[0]
                    existing_revision=conn.execute('SELECT revision FROM message_revision WHERE message_key=? AND body_sha256=?',(message_key,body_hash)).fetchone()
                    write_revision=not existing_revision
                    revision=conn.execute('SELECT MAX(revision) FROM message_revision WHERE message_key=?',(message_key,)).fetchone()[0]+1 if write_revision else existing_revision[0]
                    if body_hash != prior_body:
                        quality['body']='conflict'
                    if prior['kind']!=kind:quality['kind']='conflict'
                    for dimension in ('body','direction','time','success','correlation','kind'):
                        if prior_quality.get(dimension)=='conflict':quality[dimension]='conflict'
                    for dimension in ('direction','time','success'):
                        if quality[dimension] in ('unknown','pending_time') and prior_quality.get(dimension)=='verified':
                            quality[dimension]='verified'
                    # Less capable or delayed observations cannot erase verified dimensions.
                    if effective_direction=='unknown': effective_direction=prior['direction']
                    elif prior['direction'] not in ('unknown',effective_direction):
                        effective_direction='unknown'; quality['direction']='conflict'
                    if effective_success=='unknown' or (prior['success_state']=='confirmed' and effective_success=='pending'):
                        effective_success=prior['success_state']
                    elif prior['success_state'] in ('confirmed','failed') and effective_success in ('confirmed','failed') and prior['success_state']!=effective_success:
                        effective_success='unknown'; quality['success']='conflict'
                    if effective_lower is None:
                        effective_lower,effective_upper,effective_precision=prior['occurred_lower_utc'],prior['occurred_upper_utc'],prior['time_precision']
                    elif prior['occurred_lower_utc'] and (effective_lower,effective_upper)!=(prior['occurred_lower_utc'],prior['occurred_upper_utc']):
                        effective_lower=effective_upper=None; effective_precision='unknown'; quality['time']='conflict'
                    if effective_correlation is None: effective_correlation=prior['platform_correlation_key']
                    elif prior['platform_correlation_key'] and effective_correlation!=prior['platform_correlation_key']:
                        effective_correlation=None; quality['correlation']='conflict'
                    if quality.get('direction')=='conflict':effective_direction='unknown'
                    if quality.get('success')=='conflict':effective_success='unknown'
                    if quality.get('time')=='conflict':effective_lower=effective_upper=None;effective_precision='unknown'
                    if quality.get('correlation')=='conflict':effective_correlation=None
                    conn.execute('UPDATE message SET direction=?,occurred_lower_utc=?,occurred_upper_utc=?,time_precision=?,success_state=?,active_body_revision=?,quality_json=?,platform_correlation_key=? WHERE message_key=?',(effective_direction,effective_lower,effective_upper,effective_precision,effective_success,active_revision,canonical_json(quality).decode(),effective_correlation,message_key))
                else:
                    conn.execute('INSERT INTO message(message_key,account_key,conversation_key,person_key,platform_message_key,direction,kind,occurred_lower_utc,occurred_upper_utc,time_precision,success_state,identity_state,active_body_revision,quality_json,platform_correlation_key,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(message_key,account,conversation,person,effective_platform,effective_direction,kind,effective_lower,effective_upper,effective_precision,effective_success,'exact' if exact else 'ambiguous',revision,canonical_json(quality).decode(),effective_correlation,now))
                if write_revision:
                    conn.execute('INSERT INTO message_revision VALUES (?,?,?,?,?,?,?)',(message_key,revision,body,body_hash,semantic,observation_key,now))
                status='accepted' if exact else 'pending'
                coverage_id=stable_key('coverage',observation_key)
                conn.execute('INSERT INTO coverage_segment VALUES (?,?,?,?,?,?,?)',(coverage_id,source_id,version,account,conversation,canonical_json(observation.get('coverage',{})).decode(),now))
            conn.execute('INSERT INTO message_observation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(observation_key,source_id,version,observation['observation_id'],account,message_key,device,now,semantic,canonical_json(observation).decode(),canonical_json(quality).decode(),status))
            if emit_event:
                self.db.append_event(conn,account_key=account,target_key=message_key or observation_key,event_type='message.observed' if message_key else 'observation.pending',payload={'observation_key':observation_key,'observation':observation,'message_key':message_key,'quality':quality})
            if message_key and prior and prior['actor_state']=='attributed':
                from .send_evidence import SendEvidence
                SendEvidence(self.db).link_success(message_key,emit_event=emit_event)
            return {'status':status,'message_key':message_key,'observation_key':observation_key,'quality':quality,'committed':True}

    @guarded
    def get_message(self,message_key):
        with self.db.lock:
            row=self.db.conn.execute('SELECT m.*,r.body,r.body_sha256 FROM message m JOIN message_revision r ON r.message_key=m.message_key AND r.revision=m.active_body_revision WHERE m.message_key=? AND '+sql_visible(self.db),(message_key,)).fetchone()
            if row:
                from .derived_import import check_message
                check_message(self.db,row)
        if not row: return None
        result=dict(row); result['quality']=json.loads(result.pop('quality_json'))
        result['manual_state']='verified_manual' if result['actor_state']=='attributed' else result['quality'].get('manual','manual_unknown')
        return result

    @guarded
    def list_messages(self,account_key,conversation_key=None,person_key=None,*,keyword=None,day=None,before=None,limit=50):
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=200:
            raise AppError(400,'INVALID_LIMIT','History limit must be between 1 and 200.')
        if keyword is not None and (not isinstance(keyword,str) or len(keyword)>200):
            raise AppError(400,'INVALID_KEYWORD','History keyword exceeds the limit.')
        if day is not None:
            try:
                day=date.fromisoformat(day) if isinstance(day,str) else day
                if type(day) is not date:raise ValueError()
                begin=datetime.combine(day,time.min,BJ).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
                finish=datetime.combine(day+timedelta(days=1),time.min,BJ).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
            except (ValueError,OverflowError):
                raise AppError(422,'INVALID_HISTORY_DATE','ISO Beijing date required.') from None
        scope=digest(['history-facts-1',account_key,person_key,conversation_key,keyword,day.isoformat() if day else None])
        params=[account_key]; clauses=['m.account_key=?',sql_visible(self.db)]
        for name,value in (('conversation_key',conversation_key),('person_key',person_key)):
            if value is not None:
                clauses.append('m.'+name+'=?'); params.append(value)
        if before is not None:
            if not isinstance(before,dict) or set(before)!= {'message_key','account_key','scope'} or before['account_key']!=account_key or before['scope']!=scope:
                raise AppError(400,'INVALID_CURSOR','History cursor must match the complete query.')
            require_text(before['message_key'],'message_key',200)
            clauses.append('m.message_key<?');params.append(before['message_key'])
        if day is not None:
            # Match the indexed path: only intervals wholly inside the Beijing
            # day qualify; unknown timestamps cannot acquire an inferred date.
            clauses.append('(m.occurred_lower_utc>=? AND m.occurred_lower_utc<? AND m.occurred_upper_utc<=? AND (m.occurred_upper_utc<? OR m.occurred_lower_utc<m.occurred_upper_utc))')
            params.extend([begin,finish,finish,finish])
        if keyword is not None:
            clauses.append('instr(r.body,?)>0');params.append(keyword)
        params.append(limit+1)
        with self.db.lock:
            rows=self.db.conn.execute('SELECT m.*,r.body,r.body_sha256 FROM message m JOIN message_revision r ON r.message_key=m.message_key AND r.revision=m.active_body_revision WHERE '+' AND '.join(clauses)+' ORDER BY m.message_key DESC LIMIT ?',params).fetchall()
            from .derived_import import check_message
            for row in rows[:limit]:check_message(self.db,row)
        items=[]
        for row in rows[:limit]:
            item=dict(row);item['quality']=json.loads(item.pop('quality_json'))
            item['manual_state']='verified_manual' if item['actor_state']=='attributed' else item['quality'].get('manual','manual_unknown');items.append(item)
        return {'items':items,'next_cursor':{'account_key':account_key,'message_key':items[-1]['message_key'],'scope':scope} if len(rows)>limit else None,'search_mode':'literal_substring','coverage':'observed_only'}
