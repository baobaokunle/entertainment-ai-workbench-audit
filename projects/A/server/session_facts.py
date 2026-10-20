"""Session catalogs and direct per-viewer revisions; active revisions replace, never accumulate."""
import json
import re
from .common import AppError,canonical_json,utc_now
from .db import digest,reject_floats
from .identity import IdentityService,require_text
from .message_facts import normalize_time
from .rank_facts import metric
from .sync.schema import require_schema

class SessionFacts:
    FIELDS={'revision_id','account_key','session_key','source_id','source_version','source_revision','started_lower_utc','started_upper_utc','ended_lower_utc','ended_upper_utc','time_precision','ended_official','formal','viewers','metric_coverage','pagination_terminal','captured_at_utc'}
    METRICS={'contribution','watch_seconds','comment_count','attendance','pk_contribution'}
    OPTIONAL_METRICS={'like_count'}
    def __init__(self,db):require_schema(db);self.db=db;self.ids=IdentityService(db)

    def _validate(self,conn,p):
        if not isinstance(p,dict) or set(p)!=self.FIELDS:raise AppError(422,'SESSION_FIELDS_INVALID','Complete direct session fields required.')
        reject_floats(p)
        for key in ('revision_id','account_key','session_key','source_id','source_version'):require_text(p[key],key,200)
        if type(p['source_revision']) is not int or p['source_revision']<1 or type(p['ended_official']) is not bool or type(p['formal']) is not bool or type(p['pagination_terminal']) is not bool or p['time_precision'] not in ('second','minute','unknown'):raise AppError(422,'SESSION_INVALID','Session version, boundaries or precision invalid.')
        times={key:normalize_time(p[key]) for key in ('started_lower_utc','started_upper_utc','ended_lower_utc','ended_upper_utc')};normalize_time(p['captured_at_utc'])
        if any(v is None for v in times.values()):
            if not all(v is None for v in times.values()) or p['time_precision']!='unknown':raise AppError(422,'SESSION_TIME_INVALID','Missing session intervals remain explicitly unknown.')
        elif p['time_precision']=='unknown' or times['started_lower_utc']>times['started_upper_utc'] or times['ended_lower_utc']>times['ended_upper_utc'] or times['started_upper_utc']>times['ended_lower_utc']:raise AppError(422,'SESSION_TIME_INVALID','Official session intervals are inconsistent.')
        coverage=p['metric_coverage']
        if not isinstance(coverage,dict) or not self.METRICS<=set(coverage)<=self.METRICS|self.OPTIONAL_METRICS or any(v not in ('complete','partial','unknown') for v in coverage.values()):raise AppError(422,'SESSION_COVERAGE_INVALID','Each supported metric needs independent coverage.')
        metric_names=set(coverage)
        if not isinstance(p['viewers'],list) or len(p['viewers'])>10000 or len(canonical_json(p))>6*1024*1024:raise AppError(413,'SESSION_LIMIT','Viewer revision exceeds finite limits.')
        reasons=[];seen=set()
        if p['captured_at_utc'] is None:reasons.append('capture_time_unknown')
        account=conn.execute('SELECT verified FROM own_account WHERE account_key=?',(p['account_key'],)).fetchone()
        if not account or not account[0] or not all(self.ids.trusted(p['source_id'],p['source_version'],cap,p['account_key']) for cap in ('session_identity','session_viewers','session_pagination')):reasons.append('source_unverified')
        if not p['pagination_terminal']:reasons.append('pagination_incomplete')
        if not self.ids.trusted(p['source_id'],p['source_version'],'session_formal',p['account_key']):reasons.append('formal_classification_unknown')
        if not all(times.values()) or not p['ended_official'] or not self.ids.trusted(p['source_id'],p['source_version'],'session_boundaries',p['account_key']):reasons.append('official_boundary_unknown')
        for viewer in p['viewers']:
            if not isinstance(viewer,dict) or set(viewer)!={'person_key','metrics','entry_times_utc'} or not isinstance(viewer['metrics'],dict) or set(viewer['metrics'])!=metric_names:raise AppError(422,'VIEWER_INVALID','Each viewer needs the same explicitly covered metrics and direct entry timestamps.')
            require_text(viewer['person_key'],'person_key',200)
            if viewer['person_key'] in seen:raise AppError(422,'VIEWER_DUPLICATE','Viewer may occur only once in a session revision.')
            seen.add(viewer['person_key']);person=conn.execute('SELECT verified,object_kind FROM contact WHERE account_key=? AND person_key=?',(p['account_key'],viewer['person_key'])).fetchone()
            if not person or not person['verified'] or person['object_kind']!='person':reasons.append('person_unverified')
            for name,m in viewer['metrics'].items():
                metric(m)
                if name=='like_count' and (m['unit']!='count' or m['state']=='known' and not re.fullmatch(r'0|[1-9][0-9]{0,30}',m['value'])):raise AppError(422,'VIEWER_LIKE_COUNT_INVALID','Direct likes require a nonnegative integer count; retain source display separately.')
                if name=='watch_seconds' and m['unit']!='seconds' or name=='comment_count' and m['unit']!='count' or name=='attendance' and (m['unit']!='boolean' or m['state']=='known' and m['value'] not in ('0','1')):raise AppError(422,'VIEWER_UNIT_INVALID','Viewer metric uses its direct source unit.')
                if m['state']=='known' and not self.ids.trusted(p['source_id'],p['source_version'],'session_'+name,p['account_key']):raise AppError(422,'DIRECT_METRIC_UNSUPPORTED','Known viewer metric lacks independent source capability.')
            entries=viewer['entry_times_utc']
            if not isinstance(entries,list) or len(entries)>1000:raise AppError(422,'ENTRY_TIMES_INVALID','Direct entry timestamps must be finite.')
            if entries and not self.ids.trusted(p['source_id'],p['source_version'],'session_entry_times',p['account_key']):raise AppError(422,'DIRECT_ENTRY_UNSUPPORTED','Only directly supported entry timestamps may be stored.')
            for entry in entries:
                value=normalize_time(entry)
                if not value:raise AppError(422,'ENTRY_TIMES_INVALID','Unknown entry cannot be replaced by collection time.')
        return reasons

    def _store(self,conn,p):
        reasons=self._validate(conn,p);hashed=digest(p);key=p['revision_id']
        old=conn.execute('SELECT digest,state FROM session_revision WHERE revision_key=?',(key,)).fetchone()
        if old:
            if old[0]!=hashed:raise AppError(409,'SESSION_REVISION_REUSED','Session revision ID has different complete payload.')
            return {'status':'duplicate','state':old['state'],'revision_id':key}
        state='partial' if reasons else 'complete'
        prior=conn.execute('SELECT r.payload_json FROM session_active a JOIN session_revision r USING(revision_key) WHERE a.account_key=? AND a.session_key=?',(p['account_key'],p['session_key'])).fetchone()
        if prior and not reasons:
            prior=json.loads(prior[0])
            if (prior['source_id'],prior['source_version'])!=(p['source_id'],p['source_version']):state='partial';reasons.append('source_version_conflict')
            elif p['source_revision']<=prior['source_revision']:state='superseded'
        conn.execute('INSERT INTO session_revision VALUES(?,?,?,?,?,?,?,?,?)',(key,p['account_key'],p['session_key'],p['source_id'],p['source_version'],state,canonical_json(p).decode(),hashed,utc_now()))
        if state=='complete':conn.execute('INSERT INTO session_active VALUES(?,?,?) ON CONFLICT(account_key,session_key) DO UPDATE SET revision_key=excluded.revision_key',(p['account_key'],p['session_key'],key))
        return {'status':'saved','state':state,'revision_id':key,'active':state=='complete','reasons':reasons}

    def stage(self,payload,principal=None):
        if principal:principal.require('collection:run',payload.get('account_key'))
        with self.db.transaction() as conn:
            result=self._store(conn,payload)
            if result['status']!='duplicate':self.db.append_event(conn,account_key=payload['account_key'],target_key=payload['session_key'],event_type='session.revision',payload=payload,channel='collection')
            return result

    def apply_remote(self,conn,event):
        p=event['payload']
        if p.get('account_key')!=event['account_key'] or p.get('session_key')!=event['target_key']:raise AppError(422,'SESSION_SCOPE_INVALID','Session must bind account and stable session identity.')
        return self._store(conn,p)

    def get_active(self,account,session):
        with self.db.lock:row=self.db.conn.execute('SELECT r.payload_json FROM session_active a JOIN session_revision r USING(revision_key) WHERE a.account_key=? AND a.session_key=?',(account,session)).fetchone()
        if not row:return None
        p=json.loads(row[0])
        try:
            with self.db.lock:reasons=self._validate(self.db.conn,p)
        except AppError:return None
        return p if not reasons else None

    def list_active(self,account):
        with self.db.lock:
            keys=[r[0] for r in self.db.conn.execute('SELECT session_key FROM session_active WHERE account_key=?',(account,))]
            return [p for p in (self.get_active(account,key) for key in keys) if p is not None]

    def stage_catalog(self,payload,principal=None,*,emit_event=True):
        if principal:principal.require('collection:run',payload.get('account_key'))
        with self.db.transaction() as conn:
            result=self._catalog(conn,payload)
            if emit_event and result['status']!='duplicate':self.db.append_event(conn,account_key=payload['account_key'],target_key=payload['account_key'],event_type='session.catalog',payload=payload,channel='collection')
            return result

    def _catalog(self,conn,p):
        fields={'revision_id','account_key','source_id','source_version','source_revision','window_start_utc','window_end_utc','session_keys','pagination_terminal'}
        if not isinstance(p,dict) or set(p)!=fields:raise AppError(422,'CATALOG_INVALID','Complete official catalog fields required.')
        reject_floats(p)
        for key in ('revision_id','account_key','source_id','source_version'):require_text(p[key],key,200)
        start=normalize_time(p['window_start_utc']);end=normalize_time(p['window_end_utc'])
        if not start or not end or start>=end or type(p['source_revision']) is not int or p['source_revision']<1 or type(p['pagination_terminal']) is not bool or not isinstance(p['session_keys'],list) or len(p['session_keys'])>10000 or any(not isinstance(k,str) or not k or len(k)>200 for k in p['session_keys']) or len(set(p['session_keys']))!=len(p['session_keys']):raise AppError(422,'CATALOG_INVALID','Official catalog boundaries, keys and version required.')
        hashed=digest(p);old=conn.execute('SELECT digest,state FROM session_catalog_revision WHERE revision_key=?',(p['revision_id'],)).fetchone()
        if old:
            if old[0]!=hashed:raise AppError(409,'CATALOG_REVISION_REUSED','Catalog revision has different payload.')
            return {'status':'duplicate','state':old['state']}
        state='complete' if p['pagination_terminal'] and self.ids.trusted(p['source_id'],p['source_version'],'session_catalog',p['account_key']) else 'partial'
        prior=conn.execute('SELECT r.payload_json FROM session_catalog_active a JOIN session_catalog_revision r USING(revision_key) WHERE a.account_key=?',(p['account_key'],)).fetchone()
        if prior and state=='complete':
            prior=json.loads(prior[0])
            if (prior['source_id'],prior['source_version'])!=(p['source_id'],p['source_version']):state='partial'
            elif prior['source_revision']>=p['source_revision']:state='superseded'
        conn.execute('INSERT INTO session_catalog_revision VALUES(?,?,?,?,?)',(p['revision_id'],p['account_key'],state,canonical_json(p).decode(),hashed))
        if state=='complete':conn.execute('INSERT INTO session_catalog_active VALUES(?,?) ON CONFLICT(account_key) DO UPDATE SET revision_key=excluded.revision_key',(p['account_key'],p['revision_id']))
        return {'status':'saved','state':state,'revision_id':p['revision_id']}

    def catalog(self,account):
        with self.db.lock:row=self.db.conn.execute('SELECT r.payload_json FROM session_catalog_active a JOIN session_catalog_revision r USING(revision_key) WHERE a.account_key=?',(account,)).fetchone()
        if not row:return {'state':'unknown','sessions':[],'missing_details':[]}
        p=json.loads(row[0])
        if not self.ids.trusted(p['source_id'],p['source_version'],'session_catalog',account):return {'state':'unknown','sessions':[],'missing_details':[],'coverage':'provenance_suspended'}
        p['state']='complete';p['missing_details']=[key for key in p['session_keys'] if self.get_active(account,key) is None];return p

    def apply_catalog(self,conn,event):
        if event['account_key']!=event['payload'].get('account_key') or event['target_key']!=event['account_key']:raise AppError(422,'CATALOG_SCOPE_INVALID','Catalog event must bind account.')
        self._catalog(conn,event['payload'])

    def register_handlers(self,importer):
        importer.register_handler('session.revision',self.apply_remote);importer.register_handler('session.catalog',self.apply_catalog)
