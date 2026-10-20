"""New fictional schema-3 payload catalog. Never a production DB migration.

Purged metadata is not a schema-2 event and confers no signature authority.
The original business DB and all external copies remain copies_pending.
"""
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
from contextlib import contextmanager

from server.common import AppError,canonical_json
from server.db import digest,stable_key
from server.deletion_manifest import DeletionManifestService
from server.local_deletion import LogicalDeletionCoordinator,require_ready,observation_selectors
from server.sync.events import from_row,parse_json,validate_event,FIELDS
from server.ai_annotations import validate_result

FORMAT='dfa-fictional-payload-catalog-3'
SCHEMA_VERSION=3
MAX_ROWS=50000
MAX_BYTES=64*1048576
COPIES=['original_business_db','wire','journal','backup','WAL_free_pages','peer_external_provider_copies']
SUPPORTED={'domain_event','sync_received','message_observation','message_revision','ai_job','ai_control',
           'event_sequence','sync_watermark','outbox'}
DDL='''
PRAGMA user_version=3;
CREATE TABLE catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE object_meta(object_kind TEXT NOT NULL,object_key TEXT NOT NULL,account_key TEXT,
 metadata_json TEXT NOT NULL,source_row_digest TEXT NOT NULL,payload_state TEXT NOT NULL CHECK(payload_state IN ('retained','purged')),
 purge_basis_json TEXT NOT NULL,PRIMARY KEY(object_kind,object_key));
CREATE TABLE object_body(object_kind TEXT NOT NULL,object_key TEXT NOT NULL,field TEXT NOT NULL,
 raw BLOB NOT NULL,raw_sha256 TEXT NOT NULL,PRIMARY KEY(object_kind,object_key,field),
 FOREIGN KEY(object_kind,object_key) REFERENCES object_meta(object_kind,object_key));
CREATE TABLE unsupported(source_table TEXT PRIMARY KEY,row_count INTEGER NOT NULL,rows_digest TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE stream_state(source_table TEXT NOT NULL,row_key TEXT NOT NULL,metadata_json TEXT NOT NULL,source_row_digest TEXT NOT NULL,
 PRIMARY KEY(source_table,row_key));
'''


def _error(code,message,status=409):raise AppError(status,code,message)


def _bytes(value):
    if not isinstance(value,str):_error('CATALOG_SOURCE_INVALID','Original payload must be SQLite TEXT.')
    return value.encode('utf-8')


def _sha(value):return hashlib.sha256(value).hexdigest()


def _path(path, *, directory=False):
    raw=Path(path)
    if not raw.is_absolute():_error('CATALOG_PATH','Explicit absolute owned workspace paths are required.',422)
    for p in (raw,*raw.parents):
        if p.is_symlink() or getattr(p,'is_junction',lambda:False)():
            _error('CATALOG_PATH','Catalog paths cannot traverse links or junctions.',422)
    if raw.exists():
        if directory:
            if not raw.is_dir():_error('CATALOG_PATH','Workspace root must be an existing directory.',422)
        elif not raw.is_file() or raw.stat().st_nlink!=1:
            _error('CATALOG_PATH','Catalog must be an owned regular file without hard links.',422)
    elif directory:_error('CATALOG_PATH','Workspace parent must already exist.',422)
    return raw.resolve()


def _new_output(path,root):
    root=_path(root,directory=True);output=_path(path)
    if output==root or not output.is_relative_to(root):
        _error('CATALOG_PATH','New catalog must be inside the explicit workspace root.',422)
    _path(output.parent,directory=True)
    if output.exists():_error('CATALOG_EXISTS','A catalog cannot overwrite any existing path.')
    return output,root


def _scope(records):
    result=dict(targets=set(),jobs=set(),events={},aliases=set(),actions=set())
    for record in records:
        intent=record['intent'];account=intent['account_key']
        result['targets'].update((account,t['target_kind'],t['target_key']) for t in intent['targets'])
        result['jobs'].update((account,k) for k in intent['ai_job_keys'])
        result['aliases'].update(canonical_json(a).decode() for a in intent['alias_selectors'])
        for ref in intent['event_refs']:
            identity=(ref['event_digest'],account)
            if result['events'].setdefault(ref['event_id'],identity)!=identity:
                _error('CATALOG_EVENT_CONFLICT','Authenticated intent has inconsistent immutable event identities.')
        if sum(len(result[k]) for k in ('targets','jobs','events','aliases','actions'))>MAX_ROWS:
            _error('CATALOG_LIMIT','Complete authenticated scope exceeds its finite identifier budget.',413)
    return result


def _observation_hidden(account,raw,scope, *, observation_key=None,message_key=None):
    if (account,'observation',observation_key) in scope['targets'] or (account,'message',message_key) in scope['targets']:return True
    selector=observation_selectors(raw)
    file_fields=('source_id','source_version','account_namespace','source_key')
    platform_fields=('platform_conversation_key','platform_message_key')
    candidates=[]
    if all(isinstance(selector[k],str) and selector[k] for k in file_fields):
        candidates.append(dict(kind='file_record',account_key=account,**{k:selector[k] for k in file_fields}))
    if all(isinstance(selector[k],str) and selector[k] for k in platform_fields):
        candidates.append(dict(kind='platform_message',account_key=account,**{k:selector[k] for k in platform_fields}))
    return any(canonical_json(c).decode() in scope['aliases'] for c in candidates)


def _refs_hidden(account,payload,scope):
    for kind,key in DeletionManifestService._references(payload):
        if kind in ('message_key','observation_key') and (account,kind[:-4],key) in scope['targets']:return True
        if kind=='job_key' and (account,key) in scope['jobs']:return True
        if kind=='action_key' and (account,key) in scope['actions']:return True
    return False


def _event_hidden(event,scope):
    ref=scope['events'].get(event['event_id'])
    if ref:
        if ref!=(event['event_digest'],event['account_key']):_error('CATALOG_EVENT_CONFLICT','Event ID does not match authenticated deletion digest/account.')
        return True
    payload=event['payload'];account=event['account_key']
    if _refs_hidden(account,payload,scope):return True
    if event['event_type'] in ('message.observed','observation.pending') and isinstance(payload.get('observation'),dict):
        return _observation_hidden(account,payload['observation'],scope,observation_key=payload.get('observation_key'),message_key=payload.get('message_key'))
    return False


class PayloadCatalogBuilder:
    def __init__(self,db, *, fixture_mode=False,max_rows=MAX_ROWS,max_bytes=MAX_BYTES):
        if fixture_mode is not True:_error('CATALOG_FIXTURE_ONLY','New payload catalogs are limited to explicit fictional fixtures.',403)
        if type(max_rows)is not int or type(max_bytes)is not int or not 1<=max_rows<=MAX_ROWS or not 1<=max_bytes<=MAX_BYTES:
            _error('CATALOG_LIMIT','Finite limits cannot exceed the supported complete snapshot budget.',422)
        self.db,self.max_rows,self.max_bytes=db,max_rows,max_bytes

    def _snapshot(self):
        """Caller owns lock and an SQLite transaction; every table shares one snapshot."""
        require_ready(self.db)
        gate=getattr(self.db,'_deletion_gate',None)
        if not isinstance(gate,LogicalDeletionCoordinator) or not gate.ready:
            _error('CATALOG_GATE_REQUIRED','A ready explicitly bound fictional deletion coordinator is required.',503)
        conn=self.db.conn
        version=conn.execute('PRAGMA user_version').fetchone()[0]
        if version!=2:_error('CATALOG_SOURCE_SCHEMA','Only explicit schema-2 source fixtures are supported.')
        schema=[];schema_bytes=0
        for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"):
            record=dict(row);schema.append(record)
            schema_bytes+=len(canonical_json(record))
            if len(schema)>self.max_rows or schema_bytes>self.max_bytes:_error('CATALOG_LIMIT','Complete source schema exceeds its finite snapshot budget.',413)
        rows={};hashes={};count=len(schema);charged=schema_bytes
        for name in sorted(r['name'] for r in schema if r['type']=='table'):
            table_rows=[];table_hashes=[]
            sql='SELECT * FROM "'+name.replace('"','""')+'"'
            for record in conn.execute(sql):
                row=dict(record);count+=1
                charged+=sum(len(v) if isinstance(v,bytes) else len(v.encode('utf-8')) if isinstance(v,str) else 16 for v in row.values())
                if count>self.max_rows or charged>self.max_bytes:
                    _error('CATALOG_LIMIT','Complete source snapshot exceeds its finite budget; nothing is truncated.',413)
                table_hashes.append(DeletionManifestService._row_digest(row))
                if name in SUPPORTED:table_rows.append(row)
            hashes[name]=dict(count=len(table_hashes),digest=digest(sorted(table_hashes)))
            if name in SUPPORTED:rows[name]=table_rows
        meta=dict(conn.execute('SELECT key,value FROM app_meta'))
        if not isinstance(meta.get('data_version'),str) or not meta['data_version'].isdigit():
            _error('CATALOG_SOURCE_INVALID','Persisted monotonic source data_version is required.')
        head=gate.expected_head;records=gate.log.read(expected_head=head)
        charged+=len(canonical_json(records))
        if charged>self.max_bytes:_error('CATALOG_LIMIT','Authenticated scope and complete source exceed the finite snapshot byte budget.',413)
        token=dict(source_schema=version,source_schema_digest=digest(schema),data_version=meta['data_version'],log_head=head,
                   source_snapshot_digest=digest(hashes),object_rows=count,charged_bytes=charged,
                   device_key=meta.get('device_key'),epoch=meta.get('epoch'),intent_digest=digest(records))
        token['plan_hash']=digest(dict(format=FORMAT,source=token))
        return dict(rows=rows,hashes=hashes,scope=_scope(records),token=token)

    def _capture(self):
        with self.db.lock:
            if self.db.conn.in_transaction:_error('CATALOG_TRANSACTION','Use an independent completed source transaction for catalog planning.')
            self.db.conn.execute('BEGIN')
            try:return self._snapshot()
            finally:self.db.conn.execute('ROLLBACK')

    def plan(self):
        snapshot=self._capture()
        return {**snapshot['token'],'format':FORMAT,'catalog_schema':3,'mode':'fictional_new_catalog_only',
                'copies_state':'copies_pending','physical_purged':False,'product_entry_enabled':False,
                'unsupported':[{ 'source_table_digest':digest(t),**h } for t,h in snapshot['hashes'].items() if t not in SUPPORTED]}

    def _populate(self,conn,snapshot):
        rows,scope=snapshot['rows'],snapshot['scope']
        events={};event_sources=[]
        for table in ('domain_event','sync_received'):
            for row in rows.get(table,[]):
                event=from_row(row)
                for field in ('event_id','event_digest','device_key','epoch','channel','seq','account_key','target_key','event_type','payload_hash','created_at_utc'):
                    if field in row and row[field]!=event[field]:_error('CATALOG_EVENT_CONFLICT','Event storage identity does not equal its immutable original semantics.')
                if 'parents_json' in row and parse_json(row['parents_json'])!=event['parents']:
                    _error('CATALOG_EVENT_CONFLICT','Stored parent metadata does not equal original event semantics.')
                if events.setdefault(event['event_id'],event['event_digest'])!=event['event_digest']:
                    _error('CATALOG_EVENT_CONFLICT','Snapshot contains reused immutable event identities.')
                event_sources.append((table,row,event))
                if event['event_id'] in scope['events'] and event['event_type']=='action.candidate':
                    scope['actions'].add((event['account_key'],event['target_key']))
        # Derivation uses typed exact IDs only; body/quote/person comparisons never confer association.
        contexts={}
        for job in rows.get('ai_job',[]):
            context=parse_json(job['context_json'])
            if not isinstance(context,dict) or digest(context)!=job['context_digest'] or context.get('account_key')!=job['account_key'] or context.get('person_key')!=job['person_key']:
                _error('CATALOG_SOURCE_INVALID','AI context does not bind its recorded digest and exact same-account person.')
            contexts[job['job_key']]=context
            if _refs_hidden(job['account_key'],context,scope):scope['jobs'].add((job['account_key'],job['job_key']))

        def add(kind,key,account,metadata,row,purged,bodies):
            state='purged' if purged else 'retained'
            conn.execute('INSERT INTO object_meta VALUES(?,?,?,?,?,?,?)',(kind,key,account,canonical_json(metadata).decode(),
                         DeletionManifestService._row_digest(row),state,canonical_json(['authenticated_exact_scope'] if purged else []).decode()))
            if not purged:
                for field,raw in bodies.items():
                    if raw is not None:
                        raw=_bytes(raw)
                        conn.execute('INSERT INTO object_body VALUES(?,?,?,?,?)',(kind,key,field,raw,_sha(raw)))

        outbox={r['event_id']:r for r in rows.get('outbox',[])}
        for table,row,event in event_sources:
            metadata={k:v for k,v in event.items() if k!='payload'}
            metadata.update(original_event_json_sha256=_sha(_bytes(row['event_json'])),original_storage=table,
                            original_state=row.get('state'),outbox=outbox.get(event['event_id']) if table=='domain_event' else None)
            add('event' if table=='domain_event' else 'received_event',event['event_id'],event['account_key'],metadata,row,
                _event_hidden(event,scope),{'event_json':row['event_json']})
        for row in rows.get('message_observation',[]):
            raw=parse_json(row['payload_json'])
            if not isinstance(raw,dict) or digest(raw)!=row['semantic_digest']:_error('CATALOG_SOURCE_INVALID','Observation semantic digest does not bind its original payload.')
            if any(raw.get(k)!=row[k] for k in ('observation_id','source_id','account_key')) or raw.get('source_version','1')!=row['source_version'] or row['observation_key']!=stable_key('observation',row['source_id'],row['source_version'],row['observation_id']):
                _error('CATALOG_SOURCE_INVALID','Observation source/account/version identity is inconsistent.')
            metadata={k:v for k,v in row.items() if k not in ('payload_json','quality_json')}
            metadata.update(original_payload_sha256=_sha(_bytes(row['payload_json'])),quality_sha256=_sha(_bytes(row['quality_json'])))
            hidden=_observation_hidden(row['account_key'],raw,scope,observation_key=row['observation_key'],message_key=row['message_key'])
            add('observation',row['observation_key'],row['account_key'],metadata,row,hidden,{'payload_json':row['payload_json'],'quality_json':row['quality_json']})
        # Message identity metadata is known from observations; account lookup is snapshot-only.
        account_by_message={r['message_key']:r['account_key'] for r in rows.get('message_observation',[]) if r['message_key']}
        observation_by_key={r['observation_key']:r for r in rows.get('message_observation',[])}
        for row in rows.get('message_revision',[]):
            account=account_by_message.get(row['message_key'])
            if not account:_error('CATALOG_SOURCE_INVALID','Revision requires an exact observed same-account message identity.')
            if _sha(_bytes(row['body']))!=row['body_sha256']:_error('CATALOG_SOURCE_INVALID','Original revision body does not match its recorded digest.')
            observation=observation_by_key.get(row['observation_key'])
            if not observation or observation['message_key']!=row['message_key'] or observation['semantic_digest']!=row['semantic_digest'] or parse_json(observation['payload_json']).get('body','')!=row['body']:
                _error('CATALOG_SOURCE_INVALID','Revision semantic digest requires its exact original observation/body.')
            metadata={k:v for k,v in row.items() if k!='body'}
            hidden=(account,'message',row['message_key']) in scope['targets'] or (account,'observation',row['observation_key']) in scope['targets']
            add('revision',canonical_json([row['message_key'],row['revision']]).decode(),account,metadata,row,hidden,{'body':row['body']})
        for row in rows.get('ai_job',[]):
            context=contexts[row['job_key']]
            if row['result_json'] is not None:validate_result(row['result_json'],context,row['purpose'])
            metadata={k:v for k,v in row.items() if k not in ('context_json','result_json','error_code')}
            metadata.update(context_sha256=_sha(_bytes(row['context_json'])),result_sha256=_sha(_bytes(row['result_json'])) if row['result_json'] is not None else None,
                            error_code_digest=digest(row['error_code']))
            add('ai_job',row['job_key'],row['account_key'],metadata,row,(row['account_key'],row['job_key']) in scope['jobs'],
                {'context_json':row['context_json'],'result_json':row['result_json']})
        jobs={r['job_key']:r for r in rows.get('ai_job',[])}
        for row in rows.get('ai_control',[]):
            job=jobs.get(row['job_key'])
            if not job:_error('CATALOG_SOURCE_INVALID','Control cannot be assigned by guessing a missing job identity.')
            value=parse_json(row['value_json'])
            if row['operation']=='correct':validate_result(value,contexts[row['job_key']],job['purpose'])
            elif row['operation'] not in ('cancel','hide') or value is not None:_error('CATALOG_SOURCE_INVALID','Finite AI control metadata required.')
            metadata={k:v for k,v in row.items() if k!='value_json'}
            metadata['value_sha256']=_sha(_bytes(row['value_json']))
            add('ai_control',row['event_id'],job['account_key'],metadata,row,(job['account_key'],row['job_key']) in scope['jobs'],{'value_json':row['value_json']})
        for table in ('event_sequence','sync_watermark'):
            for row in rows.get(table,[]):
                key=canonical_json([row[k] for k in ('device_key','epoch','channel')]).decode()
                conn.execute('INSERT INTO stream_state VALUES(?,?,?,?)',(table,key,canonical_json(row).decode(),DeletionManifestService._row_digest(row)))
        for table,h in snapshot['hashes'].items():
            if table not in SUPPORTED:
                conn.execute('INSERT INTO unsupported VALUES(?,?,?,?)',(table,h['count'],h['digest'],'unsupported_original_source_retained_copies_pending'))

    def build(self,output_path,workspace_root, *, expected_plan_hash):
        output,root=_new_output(output_path,workspace_root)
        if os.name!='nt':_error('CATALOG_PLATFORM','This fixture publisher requires Windows non-overwriting atomic rename.',422)
        snapshot=self._capture()
        if not isinstance(expected_plan_hash,str) or not hmac.compare_digest(snapshot['token']['plan_hash'],expected_plan_hash):
            _error('CATALOG_SOURCE_CHANGED','Source snapshot or authenticated head changed; review a new complete plan.')
        temporary=output.parent/('.catalog_build_'+secrets.token_hex(16)+'.sqlite3')
        published=False
        try:
            with temporary.open('xb'):pass
            conn=sqlite3.connect(temporary,isolation_level=None)
            try:
                conn.execute('PRAGMA foreign_keys=ON');conn.execute('PRAGMA synchronous=FULL')
                conn.executescript('BEGIN IMMEDIATE;'+DDL)
                self._populate(conn,snapshot)
                base=dict(format=FORMAT,catalog_schema=3,source=snapshot['token'],copies_state='copies_pending',physical_purged=False,
                          source_database_body_removed=False,original_signature_authority=False,product_entry_enabled=False,
                          uninspected_copies=COPIES)
                conn.execute('INSERT INTO catalog_meta VALUES(?,?)',('manifest',canonical_json(base).decode()))
                conn.execute('INSERT INTO catalog_meta VALUES(?,?)',('catalog_digest',_catalog_digest(conn)))
                conn.execute('COMMIT')
            finally:conn.close()
            with temporary.open('r+b') as handle:os.fsync(handle.fileno())
            verified=verify_catalog_metadata(temporary)
            # Writer serialization makes final digest/head recheck and publish one bounded critical section.
            with self.db.transaction():
                with _log_checkpoint(self.db._deletion_gate.log):
                    current=self._snapshot()
                    if current['token']!=snapshot['token']:_error('CATALOG_SOURCE_CHANGED','Complete source changed while the candidate was being constructed.')
                    _new_output(output,root);_path(temporary)
                    os.rename(temporary,output) # Windows rename fails atomically if destination exists.
                    published=True
            return verified
        finally:
            if not published and temporary.exists():temporary.unlink()


@contextmanager
def _log_checkpoint(log):
    """Reserve the separate log against another connection's append at publication."""
    with log.lock:
        checkpoint=sqlite3.connect(log.path.as_uri()+'?mode=rw',uri=True,isolation_level=None)
        try:
            checkpoint.execute('PRAGMA busy_timeout=1000')
            checkpoint.execute('BEGIN IMMEDIATE')
            yield
        finally:
            if checkpoint.in_transaction:checkpoint.execute('ROLLBACK')
            checkpoint.close()


CATALOG_COLUMNS={
 'catalog_meta':('key','value'),
 'object_meta':('object_kind','object_key','account_key','metadata_json','source_row_digest','payload_state','purge_basis_json'),
 'object_body':('object_kind','object_key','field','raw','raw_sha256'),
 'unsupported':('source_table','row_count','rows_digest','reason'),
 'stream_state':('source_table','row_key','metadata_json','source_row_digest')}


def _catalog_bounds(conn):
    total=0;charged=0
    for table,columns in CATALOG_COLUMNS.items():
        actual=tuple(r[1] for r in conn.execute('PRAGMA table_info('+table+')'))
        if actual!=columns:_error('CATALOG_SCHEMA','Catalog requires exact finite column shapes.')
        count,bytes_count=conn.execute('SELECT count(*),coalesce(sum('+ '+'.join('coalesce(length('+c+'),0)' for c in columns)+'),0) FROM '+table).fetchone()
        total+=count;charged+=bytes_count
        if count>(2*MAX_ROWS if table=='object_body' else MAX_ROWS):_error('CATALOG_LIMIT','Catalog table exceeds its finite row budget before hashing.',413)
        if total>4*MAX_ROWS or charged>2*MAX_BYTES:_error('CATALOG_LIMIT','Complete catalog rows/bytes exceed the verification budget.',413)
    body_bytes=conn.execute('SELECT coalesce(sum(length(raw)),0) FROM object_body').fetchone()[0]
    if body_bytes>MAX_BYTES:_error('CATALOG_LIMIT','Catalog bodies exceed the finite byte budget before hashing.',413)


def _catalog_digest(conn):
    _catalog_bounds(conn)
    tables={}
    for table in ('catalog_meta','object_meta','object_body','unsupported','stream_state'):
        rows=[]
        for row in conn.execute('SELECT * FROM '+table):
            values=list(row)
            if table=='catalog_meta' and values[0]=='catalog_digest':continue
            rows.append(digest([{'sha256':_sha(v),'bytes':len(v)} if isinstance(v,bytes) else v for v in values]))
        tables[table]=sorted(rows)
    return digest(tables)


def _hash(value):return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def _text(value,limit=200):return isinstance(value,str) and 0<len(value)<=limit and '\x00' not in value


def _manifest_source(manifest):
    fields={'format','catalog_schema','source','copies_state','physical_purged','source_database_body_removed','original_signature_authority','product_entry_enabled','uninspected_copies'}
    if not isinstance(manifest,dict) or set(manifest)!=fields or manifest['format']!=FORMAT or manifest['catalog_schema']!=3 or manifest['copies_state']!='copies_pending' or any(manifest[k] is not False for k in ('physical_purged','source_database_body_removed','original_signature_authority','product_entry_enabled')) or manifest['uninspected_copies']!=COPIES:
        _error('CATALOG_SCHEMA','Finite catalog manifest fields and copy boundaries are required.')
    source=manifest['source']
    hashes={'source_schema_digest','log_head','source_snapshot_digest','intent_digest','plan_hash'}
    fields=hashes|{'source_schema','data_version','object_rows','charged_bytes','device_key','epoch'}
    if not isinstance(source,dict) or set(source)!=fields or any(not _hash(source[k]) for k in hashes) or source['source_schema']!=2 or not _text(source['data_version'],20) or not source['data_version'].isdigit() or any(not _text(source[k]) for k in ('device_key','epoch')) or type(source['object_rows'])is not int or not 0<=source['object_rows']<=MAX_ROWS or type(source['charged_bytes'])is not int or not 0<=source['charged_bytes']<=MAX_BYTES:
        _error('CATALOG_SCHEMA','Exact bounded source checkpoint fields are required.')
    if source['plan_hash']!=digest(dict(format=FORMAT,source={k:v for k,v in source.items() if k!='plan_hash'})):
        _error('CATALOG_DIGEST','Source plan digest does not bind its complete checkpoint.')
    # Do not display raw IDs or attacker-controlled strings. This checksum is not external authentication.
    visible={k:v for k,v in source.items() if k not in ('device_key','epoch')}
    visible.update(device_key_digest=digest(source['device_key']),epoch_digest=digest(source['epoch']))
    return visible


META_FIELDS={
 'event':(FIELDS-{'payload'})|{'event_digest','original_event_json_sha256','original_storage','original_state','outbox'},
 'received_event':(FIELDS-{'payload'})|{'event_digest','original_event_json_sha256','original_storage','original_state','outbox'},
 'observation':{'observation_key','source_id','source_version','observation_id','account_key','message_key','device_key','observed_at_utc','semantic_digest','status','original_payload_sha256','quality_sha256'},
 'revision':{'message_key','revision','body_sha256','semantic_digest','observation_key','created_at_utc'},
 'ai_job':{'job_key','account_key','person_key','purpose','state','context_digest','model_digest','prompt_version','input_tokens','request_event_id','created_at_utc','updated_at_utc','context_sha256','result_sha256','error_code_digest'},
 'ai_control':{'event_id','job_key','operation','parents_json','value_sha256'}}


def _object_shape(row):
    kind=row['object_kind']
    if kind not in META_FIELDS or not _text(row['object_key'],500) or row['account_key'] is not None and not _text(row['account_key']) or not _hash(row['source_row_digest']) or row['payload_state'] not in ('retained','purged'):
        _error('CATALOG_SCHEMA','Finite object identities and states are required.')
    raw=row['metadata_json']
    if not isinstance(raw,str) or len(raw.encode())>65536:_error('CATALOG_LIMIT','Metadata object exceeds its finite byte budget.',413)
    m=parse_json(raw)
    if not isinstance(m,dict) or set(m)!=META_FIELDS[kind]:_error('CATALOG_SCHEMA','Metadata cannot contain undeclared payload fields.')
    if parse_json(row['purge_basis_json'])!=(['authenticated_exact_scope'] if row['payload_state']=='purged' else []):_error('CATALOG_SCHEMA','Finite purge basis required.')
    for key,value in m.items():
        if key.endswith(('sha256','digest')) and value is not None and key!='model_digest' and not _hash(value):_error('CATALOG_SCHEMA','Original digest fields must remain exact hashes.')
        if key not in ('parents','actor','outbox') and (not isinstance(value,(str,int)) and value is not None or isinstance(value,str) and (len(value)>22000 if key=='parents_json' else len(value)>2000)):
            _error('CATALOG_SCHEMA','Metadata values must have finite declared scalar shapes.')
    expected={'event':{'event_json'},'received_event':{'event_json'},'observation':{'payload_json','quality_json'},'revision':{'body'},'ai_control':{'value_json'},'ai_job':{'context_json'}|({'result_json'} if m.get('result_sha256') is not None else set())}[kind]
    if kind in ('event','received_event'):
        if m['original_storage']!=('domain_event' if kind=='event' else 'sync_received') or m['schema_version']!=2 or type(m['seq'])is not int or not 1<=m['seq']<=2**63-1 or m['event_id']!=row['object_key'] or m['account_key']!=row['account_key']:
            _error('CATALOG_EVENT_CONFLICT','Event metadata must preserve original storage/identity/schema.')
        if any(not _text(m[k]) for k in ('event_id','device_key','epoch','channel','account_key','target_key','event_type')) or m['event_id']!=stable_key('event',m['device_key'],m['epoch'],m['channel'],m['seq']):_error('CATALOG_EVENT_CONFLICT','Event identity must bind its original stream sequence.')
        if not isinstance(m['parents'],list) or len(m['parents'])>100 or any(not _text(k) for k in m['parents']) or m['parents']!=sorted(set(m['parents'])):_error('CATALOG_SCHEMA','Finite canonical event parents required.')
        if m['actor'] is not None and (not isinstance(m['actor'],dict) or set(m['actor'])!={'operator_key','device_key','session_id'} or any(not _text(v) for v in m['actor'].values())):_error('CATALOG_SCHEMA','Actor metadata cannot contain payload fields.')
        if m['outbox'] is not None and (not isinstance(m['outbox'],dict) or set(m['outbox'])!={'event_id','state','attempts','next_attempt_at','created_at_utc'} or m['outbox']['event_id']!=m['event_id'] or any(not isinstance(v,(str,int)) and v is not None for v in m['outbox'].values())):_error('CATALOG_SCHEMA','Finite original outbox metadata required.')
    return m,expected


def verify_catalog_metadata(path):
    """Read-only verifier; returns only identity hashes/counts/states, never payloads."""
    path=_path(path)
    if not path.is_file():_error('CATALOG_MISSING','Catalog must already exist.',404)
    if path.stat().st_size>2*MAX_BYTES:_error('CATALOG_LIMIT','Catalog file exceeds finite verification budget.',413)
    conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    try:
        if conn.execute('PRAGMA user_version').fetchone()[0]!=3:_error('CATALOG_SCHEMA','This is not the explicit new schema-3 catalog.')
        names={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names!={'catalog_meta','object_meta','object_body','unsupported','stream_state'}:_error('CATALOG_SCHEMA','Catalog structure is incomplete or contains unsupported additions.')
        _catalog_bounds(conn)
        # An untrusted catalog may remove SQLite constraints while keeping identical column names.
        identities={'catalog_meta':('key',),'object_meta':('object_kind','object_key'),
                    'object_body':('object_kind','object_key','field'),'unsupported':('source_table',),
                    'stream_state':('source_table','row_key')}
        for table,keys in identities.items():
            if conn.execute('SELECT 1 FROM '+table+' GROUP BY '+','.join(keys)+' HAVING count(*)>1 LIMIT 1').fetchone():
                _error('CATALOG_IDENTITY_REUSED','Catalog identities must be unique independently of declared constraints.')
        if conn.execute('SELECT 1 FROM object_body b LEFT JOIN object_meta m ON m.object_kind=b.object_kind AND m.object_key=b.object_key WHERE m.object_key IS NULL LIMIT 1').fetchone():
            _error('CATALOG_BODY_ORPHAN','Catalog contains an unregistered body, regardless of its declared foreign keys.')
        metadata=dict(conn.execute('SELECT key,value FROM catalog_meta'))
        if set(metadata)!={'manifest','catalog_digest'} or _catalog_digest(conn)!=metadata['catalog_digest']:_error('CATALOG_DIGEST','Complete catalog digest mismatch.')
        manifest=parse_json(metadata['manifest'])
        source=_manifest_source(manifest)
        counts={};charged=0
        for row in conn.execute('SELECT * FROM object_meta'):
            key=(row['object_kind'],row['object_key']);state=row['payload_state'];counts[state]=counts.get(state,0)+1
            if sum(counts.values())>MAX_ROWS:_error('CATALOG_LIMIT','Catalog object budget exceeded.',413)
            bodies=conn.execute('SELECT field,raw,raw_sha256 FROM object_body WHERE object_kind=? AND object_key=?',key).fetchall()
            if state=='purged' and bodies:_error('CATALOG_PURGED_BODY','Purged object must have no body row.')
            m,expected=_object_shape(row)
            if state=='retained' and {body['field'] for body in bodies}!=expected:_error('CATALOG_BODY_MISSING','Every retained object requires its exact complete original body fields.')
            raw_fields={}
            for body in bodies:
                if not isinstance(body['raw'],bytes):_error('CATALOG_SCHEMA','Original body fields must be BLOB bytes.')
                raw=bytes(body['raw']);charged+=len(raw)
                if _sha(raw)!=body['raw_sha256'] or charged>MAX_BYTES:_error('CATALOG_BODY_DIGEST','Retained body digest/budget is invalid.')
                raw_fields[body['field']]=raw
            if state=='retained' and row['object_kind'] in ('event','received_event'):
                if {b['field'] for b in bodies}!={'event_json'}:_error('CATALOG_BODY_MISSING','Retained event requires its complete original body.')
                raw=bytes(bodies[0]['raw'])
                if _sha(raw)!=m['original_event_json_sha256']:_error('CATALOG_BODY_DIGEST','Original event raw bytes changed.')
                event=parse_json(raw);event['event_digest']=m['event_digest'];validate_event(event)
                if {k:v for k,v in event.items() if k!='payload'}!={k:m[k] for k in event if k!='payload'}:
                    _error('CATALOG_EVENT_CONFLICT','Retained event semantics do not equal its metadata.')
            if state=='retained' and row['object_kind']=='revision' and _sha(raw_fields['body'])!=m['body_sha256']:_error('CATALOG_BODY_DIGEST','Original revision body hash mismatch.')
            if state=='retained' and row['object_kind']=='observation':
                observation=parse_json(raw_fields['payload_json'])
                if _sha(raw_fields['payload_json'])!=m['original_payload_sha256'] or _sha(raw_fields['quality_json'])!=m['quality_sha256'] or digest(observation)!=m['semantic_digest']:
                    _error('CATALOG_BODY_DIGEST','Original observation fields/digests changed.')
            if state=='retained' and row['object_kind']=='ai_job':
                context=parse_json(raw_fields['context_json'])
                if _sha(raw_fields['context_json'])!=m['context_sha256'] or digest(context)!=m['context_digest']:_error('CATALOG_BODY_DIGEST','Original AI context digest mismatch.')
                if 'result_json' in raw_fields:
                    if _sha(raw_fields['result_json'])!=m['result_sha256']:_error('CATALOG_BODY_DIGEST','Original AI result digest mismatch.')
                    validate_result(raw_fields['result_json'].decode(),context,m['purpose'])
            if state=='retained' and row['object_kind']=='ai_control':
                if _sha(raw_fields['value_json'])!=m['value_sha256']:_error('CATALOG_BODY_DIGEST','Original AI control value hash mismatch.')
        if conn.execute('PRAGMA foreign_key_check').fetchone():_error('CATALOG_BODY_ORPHAN','Catalog contains orphan body rows.')
        for row in conn.execute('SELECT * FROM stream_state'):
            table=row['source_table'];raw=row['metadata_json']
            if table not in ('event_sequence','sync_watermark') or not isinstance(raw,str) or len(raw.encode())>4000 or not _hash(row['source_row_digest']):_error('CATALOG_SCHEMA','Finite stream-state metadata is required.')
            m=parse_json(raw);identities=('device_key','epoch','channel')
            counters=('seq',) if table=='event_sequence' else ('received','processed','applied')
            if not isinstance(m,dict) or set(m)!=set(identities+counters) or any(not _text(m[k]) for k in identities) or any(type(m[k])is not int or not 0<=m[k]<=2**63-1 for k in counters) or row['row_key']!=canonical_json([m[k] for k in identities]).decode():
                _error('CATALOG_SCHEMA','Stream metadata cannot contain payload fields or guessed identities.')
        unsupported=[]
        for row in conn.execute('SELECT * FROM unsupported ORDER BY source_table'):
            if not _text(row['source_table']) or type(row['row_count'])is not int or not 0<=row['row_count']<=MAX_ROWS or not _hash(row['rows_digest']) or row['reason']!='unsupported_original_source_retained_copies_pending':_error('CATALOG_SCHEMA','Unsupported evidence must use finite counts/digests and a fixed copy-state code.')
            unsupported.append(dict(source_table_digest=digest(row['source_table']),row_count=row['row_count'],rows_digest=row['rows_digest'],reason='unsupported_original_source_retained_copies_pending'))
        return dict(format=FORMAT,catalog_schema=3,catalog_digest=metadata['catalog_digest'],source=source,
                    object_counts=counts,unsupported=unsupported,
                    copies_state='copies_pending',physical_purged=False,original_signature_authority=False,product_entry_enabled=False,
                    checksum_only=True,catalog_authenticated=False)
    except sqlite3.Error:_error('CATALOG_SCHEMA','Catalog could not be fully verified.')
    finally:conn.close()
