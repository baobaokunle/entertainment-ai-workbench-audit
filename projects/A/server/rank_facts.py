"""Immutable original rank revisions and independently effective actual periods."""
from decimal import Decimal,InvalidOperation
from datetime import date,datetime,timedelta,timezone
import json
from .common import AppError,canonical_json,utc_now
from .db import digest,reject_floats
from .identity import IdentityService,require_text
from .message_facts import normalize_time
from .sync.schema import require_schema

ADOPTION_FIELDS={'format','time_basis','statistics_through_day','package_sha256','expires_at'}

def thirty_day_period(payload):
    """A business-day convention is explicit, never a fabricated source window."""
    if payload.get('adoption',{}).get('time_basis')=='user_confirmed_previous_broadcast':
        return payload.get('period_basis')=='rolling_30d'
    start,end=payload.get('period_start_utc'),payload.get('period_end_utc')
    return bool(start and end and datetime.fromisoformat(end.replace('Z','+00:00'))-datetime.fromisoformat(start.replace('Z','+00:00'))==timedelta(days=30))

def validate_adoption(payload,now):
    import re
    a=payload['adoption']
    try:
        day=date.fromisoformat(payload['rank_as_of_day'])
        if (type(a) is not dict or set(a)!=ADOPTION_FIELDS or a['format']!='rank-observed-adoption-1'
                or a['time_basis']!='user_confirmed_previous_broadcast'
                or a['statistics_through_day']!=(day-timedelta(days=1)).isoformat()
                or re.fullmatch('[0-9a-f]{64}',a['package_sha256']) is None
                or payload['period_start_utc'] is not None or payload['period_end_utc'] is not None
                or payload['pagination_terminal'] is not False):raise ValueError()
        captured=datetime.fromisoformat(normalize_time(payload['captured_at_utc']).replace('Z','+00:00'))
        expires=datetime.fromisoformat(normalize_time(a['expires_at']).replace('Z','+00:00'))
        if captured.astimezone(timezone(timedelta(hours=8))).date()!=day or expires<=captured:raise ValueError()
    except (KeyError,ValueError,TypeError,AttributeError):
        raise AppError(422,'RANK_ADOPTION_INVALID','Observed ranks need a reviewed business day, original capture and finite source lifetime; source window stays unknown.')
    return expires<=now

def metric(value,*,allowed_units=None):
    if not isinstance(value,dict) or set(value)!={'state','value','unit','raw_display','precision'} or value['state'] not in ('known','unknown') or value['precision'] not in ('exact','display','unknown'):
        raise AppError(422,'METRIC_INVALID','Metric must retain explicit state, unit, raw display and precision.')
    require_text(value['unit'],'unit',80)
    if allowed_units and value['unit'] not in allowed_units:raise AppError(422,'METRIC_UNIT_INVALID','Direct source unit is unsupported.')
    if not isinstance(value['raw_display'],str) or len(value['raw_display'])>200:raise AppError(422,'METRIC_INVALID','Raw display is invalid.')
    if value['state']=='unknown':
        if value['value'] is not None or value['precision']!='unknown':raise AppError(422,'UNKNOWN_IS_NOT_ZERO','Unknown metric cannot contain a zero or a known precision.')
    else:
        if not isinstance(value['value'],str) or len(value['value'])>100 or value['precision']=='unknown':raise AppError(422,'METRIC_INVALID','Known metric requires a decimal text value and precision.')
        try:
            parsed=Decimal(value['value'])
            if not parsed.is_finite() or parsed<0 or parsed>Decimal('1e30'):raise ValueError()
        except (InvalidOperation,ValueError):raise AppError(422,'METRIC_INVALID','Metric decimal must be finite and nonnegative.')
    return value

class RankFacts:
    FIELDS={'revision_id','account_key','rank_kind','period_key','period_start_utc','period_end_utc','period_label','observed_period_key','source_id','source_version','source_revision','captured_at_utc','pages','pagination_terminal'}
    ROLLING_FIELDS={'period_basis','rank_as_of_day'}
    def __init__(self,db,*,max_pages=10):
        require_schema(db)
        if type(max_pages) is not int or not 1<=max_pages<=100:raise AppError(422,'PAGE_LIMIT_INVALID','Finite safe page limit required.')
        self.db=db;self.ids=IdentityService(db);self.max_pages=max_pages

    def _validate(self,conn,payload,*,now=None):
        now=now or datetime.now(timezone.utc)
        if not isinstance(payload,dict) or set(payload) not in (self.FIELDS,self.FIELDS|self.ROLLING_FIELDS,self.FIELDS|self.ROLLING_FIELDS|{'adoption'}):raise AppError(422,'RANK_FIELDS_INVALID','Complete registered rank fields required.')
        reject_floats(payload)
        for key in ('revision_id','account_key','period_key','period_label','observed_period_key','source_id','source_version'):require_text(payload[key],key,200)
        if payload['rank_kind'] not in ('contribution','watch','comment') or type(payload['source_revision']) is not int or payload['source_revision']<1 or type(payload['pagination_terminal']) is not bool:raise AppError(422,'RANK_INVALID','Rank kind, source revision or terminal evidence invalid.')
        start=normalize_time(payload['period_start_utc']);end=normalize_time(payload['period_end_utc']);normalize_time(payload['captured_at_utc'])
        observed='adoption' in payload
        if not observed and (not start or not end or start>=end):raise AppError(422,'RANK_PERIOD_INVALID','Actual rank period requires a nonempty source window.')
        pages=payload['pages']
        if not isinstance(pages,list) or len(pages)>100 or sum(len(p.get('rows',[])) for p in pages if isinstance(p,dict))>10000 or len(canonical_json(payload))>6*1024*1024:raise AppError(413,'RANK_LIMIT','Rank snapshot exceeds finite limits.')
        reasons=[]
        if observed:
            if validate_adoption(payload,now):reasons.append('source_expired')
            source=self.ids.get_source(payload['source_id'],payload['source_version'])
            evidence=source['evidence'] if source and isinstance(source['evidence'],dict) else {}
            if (evidence.get('rank_adoption')!=payload['adoption']
                    or digest(payload) not in evidence.get('rank_payload_digests',[])):
                reasons.append('adoption_not_approved')
        if 'period_basis' in payload:
            try:
                day=payload['rank_as_of_day']
                if payload['period_basis']!='rolling_30d' or not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day:raise ValueError()
                if not thirty_day_period(payload):raise ValueError()
            except (ValueError,TypeError):raise AppError(422,'RANK_ROLLING_DAY_INVALID','Rolling rank requires an explicit source business day and exact 30-day window.')
            if not self.ids.trusted(payload['source_id'],payload['source_version'],'rank_rolling_day',payload['account_key']):reasons.append('rolling_day_unverified')
        if payload['captured_at_utc'] is None:reasons.append('capture_time_unknown')
        caps=('rank_identity','rank_rows','rank_business_day','rank_observed_rows') if observed else ('rank_identity','rank_period','rank_rows','rank_pagination')
        account=conn.execute('SELECT verified FROM own_account WHERE account_key=?',(payload['account_key'],)).fetchone()
        if not account or not account[0] or not all(self.ids.trusted(payload['source_id'],payload['source_version'],c,payload['account_key']) for c in caps):reasons.append('source_unverified')
        if payload['observed_period_key']!=payload['period_key']:reasons.append('period_tab_mismatch')
        if (not observed and not payload['pagination_terminal']) or not pages:reasons.append('pagination_incomplete')
        if len(pages)>self.max_pages:reasons.append('safe_page_limit')
        seen=set();positions=set()
        for number,page in enumerate(pages,1):
            if not isinstance(page,dict) or set(page)!={'page_number','rows'} or type(page['page_number']) is not int or page['page_number']!=number or not isinstance(page['rows'],list):raise AppError(422,'RANK_PAGE_INVALID','Pages must be sequential source pages.')
            for row in page['rows']:
                if not isinstance(row,dict) or set(row)!={'person_key','position','metric'} or type(row['position']) is not int or row['position']<1 or row['position']>1000000:raise AppError(422,'RANK_ROW_INVALID','Original rank position and stable person required.')
                require_text(row['person_key'],'person_key',200);metric(row['metric'])
                if row['person_key'] in seen or row['position'] in positions:raise AppError(422,'RANK_ROW_DUPLICATE','Rank person and original position must be unique.')
                seen.add(row['person_key']);positions.add(row['position'])
                person=conn.execute('SELECT verified,object_kind FROM contact WHERE account_key=? AND person_key=?',(payload['account_key'],row['person_key'])).fetchone()
                if not person or not person['verified'] or person['object_kind']!='person':reasons.append('person_unverified')
        return sorted(set(reasons))

    def _store(self,conn,payload,*,now=None):
        reasons=self._validate(conn,payload,now=now);hashed=digest(payload);key=payload['revision_id']
        old=conn.execute('SELECT digest,state FROM rank_revision WHERE revision_key=?',(key,)).fetchone()
        if old:
            if old[0]!=hashed:raise AppError(409,'RANK_REVISION_REUSED','Rank revision ID has different complete payload.')
            return {'status':'duplicate','state':old['state'],'revision_id':key}
        scope=(payload['account_key'],payload['rank_kind'],payload['period_key'])
        prior=conn.execute('SELECT r.payload_json FROM rank_active a JOIN rank_revision r USING(revision_key) WHERE a.account_key=? AND a.rank_kind=? AND a.period_key=?',scope).fetchone()
        state='partial' if reasons else 'observed' if 'adoption' in payload else 'complete'
        if prior and not reasons:
            prior=json.loads(prior[0])
            if (prior['source_id'],prior['source_version'])!=(payload['source_id'],payload['source_version']):state='partial';reasons.append('source_version_conflict')
            elif payload['source_revision']<=prior['source_revision']:state='superseded'
        conn.execute('INSERT INTO rank_revision VALUES(?,?,?,?,?,?,?,?,?,?)',(key,*scope,payload['source_id'],payload['source_version'],state,canonical_json(payload).decode(),hashed,utc_now()))
        if state in ('complete','observed'):conn.execute('INSERT INTO rank_active VALUES(?,?,?,?) ON CONFLICT(account_key,rank_kind,period_key) DO UPDATE SET revision_key=excluded.revision_key',(*scope,key))
        return {'status':'saved','state':state,'revision_id':key,'reasons':reasons,'active':state in ('complete','observed')}

    def stage(self,payload,principal=None,*,now=None):
        if principal:principal.require('collection:run',payload.get('account_key'))
        with self.db.transaction() as conn:
            result=self._store(conn,payload,now=now)
            if result['status']!='duplicate':self.db.append_event(conn,account_key=payload['account_key'],target_key=payload['revision_id'],event_type='rank.revision',payload=payload,channel='collection')
            return result

    def apply_remote(self,conn,event):
        if event['account_key']!=event['payload'].get('account_key') or event['target_key']!=event['payload'].get('revision_id'):raise AppError(422,'RANK_SCOPE_INVALID','Rank event binds actual account and revision.')
        self._store(conn,event['payload'])

    def get_active(self,account,kind,period,*,now=None):
        with self.db.lock:
            row=self.db.conn.execute('SELECT r.* FROM rank_active a JOIN rank_revision r USING(revision_key) WHERE a.account_key=? AND a.rank_kind=? AND a.period_key=?',(account,kind,period)).fetchone()
            if not row:return {'state':'unknown','coverage':'no_complete_revision','items':[]}
            payload=json.loads(row['payload_json'])
            if self._validate(self.db.conn,payload,now=now):return {'state':'unknown','coverage':'provenance_suspended','items':[],'revision_id':row['revision_key']}
            observed='adoption' in payload
            return {'state':'observed' if observed else 'complete','revision_id':row['revision_key'],'period_key':period,'period_start_utc':payload['period_start_utc'],'period_end_utc':payload['period_end_utc'],'period_label':payload['period_label'],'effective_cutoff':payload['captured_at_utc'],'period_basis':payload.get('period_basis'),'rank_as_of_day':payload.get('rank_as_of_day'),'items':[item for page in payload['pages'] for item in page['rows']],'coverage':'observed_rows' if observed else 'source_complete',**({'adoption':payload['adoption']} if observed else {})}

    def for_person(self,account,kind,period,person):
        result=self.get_active(account,kind,period)
        row=next((r for r in result['items'] if r['person_key']==person),None)
        return {'rank':row,'metric_state':row['metric']['state'] if row else 'unknown','period_key':period,'revision_id':result.get('revision_id'),'coverage':result['coverage']}

    def periods(self,account,*,now=None):
        with self.db.lock:
            result=[]
            for row in self.db.conn.execute('SELECT a.rank_kind,a.period_key,r.payload_json FROM rank_active a JOIN rank_revision r USING(revision_key) WHERE a.account_key=? ORDER BY a.period_key,a.rank_kind',(account,)):
                p=json.loads(row['payload_json']);result.append({'rank_kind':row['rank_kind'],'period_key':row['period_key'],'period_start_utc':p['period_start_utc'],'period_end_utc':p['period_end_utc'],'effective_cutoff':p['captured_at_utc'],'revision_id':p['revision_id'],'period_basis':p.get('period_basis'),'rank_as_of_day':p.get('rank_as_of_day'),**({'adoption':p['adoption']} if 'adoption' in p else {})})
            result=[p for p in result if self.get_active(account,p['rank_kind'],p['period_key'],now=now)['state'] in ('complete','observed')]
            return result

    def register_handlers(self,importer):importer.register_handler('rank.revision',self.apply_remote)
