"""Explicit, pending-only export imports. No file IO, collectors or schema-on-read.

Mappings are selections of previously registered identities, never registrations.
The existing observation event and outbox carry the opaque export candidate;
the MessageFacts export gate applies again on authenticated peer replay.
"""
import base64
import hashlib
import json
import math
import re

from .auth import Principal
from .common import AppError, canonical_json, utc_now
from .db import digest, stable_key
from .identity import IdentityService, require_text
from .message_facts import MessageFacts
from .local_deletion import guarded,sql_visible,reject_hidden,observation_selectors
from .file_import_index import candidate,install_import_indexes,indexes_available,VALID,NAMESPACE,BATCH

SCHEMA_VERSION = '1'
PROTOCOL = 'file-import-candidate-v1'
MAX_PREVIEW_BYTES = 32 * 1024 * 1024
MAX_VERSIONS = 2000
MAX_CANDIDATE_BYTES = 1024 * 1024


def initialize_import_schema(db):
    """Only explicit init/migrate callers install this additive schema."""
    with db.transaction() as conn:
        old = conn.execute("SELECT value FROM app_meta WHERE key='file_import_schema_version'").fetchone()
        if old and old[0] != SCHEMA_VERSION:
            raise AppError(409, 'FILE_IMPORT_SCHEMA_INCOMPATIBLE', 'Import schema needs an explicit compatible migration.')
        conn.execute('''CREATE TABLE IF NOT EXISTS file_import_binding(
            account_namespace TEXT PRIMARY KEY, account_key TEXT NOT NULL,
            source_id TEXT NOT NULL, source_version TEXT NOT NULL,
            FOREIGN KEY(account_key) REFERENCES own_account(account_key),
            FOREIGN KEY(source_id,source_version) REFERENCES source(source_id,source_version))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS file_import_batch(
            batch_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, source_id TEXT NOT NULL,
            source_version TEXT NOT NULL, account_namespace TEXT NOT NULL,
            device_key TEXT NOT NULL, input_digest TEXT NOT NULL, mapping_digest TEXT NOT NULL,
            plan_hash TEXT NOT NULL, status TEXT NOT NULL, created_at_utc TEXT NOT NULL,
            FOREIGN KEY(account_key) REFERENCES own_account(account_key))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS file_import_item(
            batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL, source_key TEXT NOT NULL,
            observation_key TEXT NOT NULL UNIQUE,
            PRIMARY KEY(batch_id,ordinal),
            FOREIGN KEY(batch_id) REFERENCES file_import_batch(batch_id),
            FOREIGN KEY(observation_key) REFERENCES message_observation(observation_key))''')
        conn.execute("INSERT INTO app_meta VALUES('file_import_schema_version',?) ON CONFLICT(key) DO NOTHING", (SCHEMA_VERSION,))
        install_import_indexes(db)


def _json_bytes(value, limit=MAX_PREVIEW_BYTES):
    """Bound depth, node count and encoded bytes before retaining normalized JSON."""
    nodes = 0
    size = 0
    def walk(v, depth=0):
        nonlocal nodes, size
        nodes += 1
        if nodes > 200000 or depth > 40:
            raise AppError(400, 'IMPORT_JSON_LIMIT', 'Import JSON exceeds depth or node budget.')
        if type(v) is dict:
            size += 2 + len(v)
            for key, child in v.items():
                if type(key) is not str:
                    raise AppError(400, 'IMPORT_JSON_INVALID', 'JSON object keys must be text.')
                walk(key, depth + 1); size += 1; walk(child, depth + 1)
        elif type(v) is list:
            size += 2 + len(v)
            for child in v: walk(child, depth + 1)
        elif type(v) in (str, int, bool, float) or v is None:
            if type(v) is str and len(v)>limit:
                raise AppError(413, 'IMPORT_PREVIEW_LIMIT', 'A single JSON string exceeds the byte budget.')
            if type(v) is float and not math.isfinite(v):
                raise AppError(400, 'IMPORT_JSON_INVALID', 'Export JSON must contain finite numbers.')
            try: size += len(canonical_json(v))
            except (ValueError, UnicodeError, OverflowError) as exc:
                raise AppError(400, 'IMPORT_JSON_INVALID', 'Invalid export JSON value.') from exc
        else:
            raise AppError(400, 'IMPORT_JSON_INVALID', 'Import accepts ordinary JSON values only.')
        if size > limit:
            raise AppError(413, 'IMPORT_PREVIEW_LIMIT', 'Import JSON exceeds the byte budget.')
    walk(value)
    return canonical_json(value)


class FileImportService:
    def __init__(self, db, history=None):
        self.db, self.history = db, history
        self.identities, self.messages = IdentityService(db), MessageFacts(db)

    def _schema(self):
        row = self.db.conn.execute("SELECT value FROM app_meta WHERE key='file_import_schema_version'").fetchone()
        if not row or row[0] != SCHEMA_VERSION:
            raise AppError(409, 'FILE_IMPORT_NOT_INITIALIZED', 'File import requires explicit schema initialization.')

    def _authorize(self, principal, account, *, write=True):
        if not write:
            if not isinstance(principal,Principal) or principal.role not in ('manager','baobao'):
                raise AppError(403,'IMPORT_HUMAN_SESSION_REQUIRED','Import history requires an enrolled human session.')
            principal.require('history:read',account)
            return
        if not isinstance(principal, Principal) or principal.role != 'manager':
            raise AppError(403, 'IMPORT_MANAGER_REQUIRED', 'File imports require an enrolled manager.')
        principal.require('identities:write', account)
        principal.require('observations:write', account)
        local = self.db.conn.execute("SELECT value FROM app_meta WHERE key='device_key'").fetchone()
        if write and (not local or local[0] != principal.device_key):
            raise AppError(403, 'IMPORT_LOCAL_DEVICE_REQUIRED', 'Import writes must originate on the enrolled local device.')

    @staticmethod
    def _candidate(payload):
        return candidate(payload)

    def _dedicated(self, source, version):
        # Add-on schemas are optional. Inspect existing tables read-only, so a
        # source used for identity, rank, session, relation or activity evidence
        # cannot later be revoked through a deceptively file-only operation.
        for row in self.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            table=row[0]
            if table in ('source','message_observation','file_import_binding','file_import_batch','file_import_item'): continue
            quoted='"'+table.replace('"','""')+'"'
            columns={r[1] for r in self.db.conn.execute('PRAGMA table_info('+quoted+')')}
            if {'source_id','source_version'}.issubset(columns):
                shared=self.db.conn.execute('SELECT 1 FROM '+quoted+' WHERE source_id=? AND source_version=? LIMIT 1',(source,version)).fetchone()
            elif table in ('session_catalog_revision','activity_event') and 'payload_json' in columns:
                shared=self.db.conn.execute('SELECT 1 FROM '+quoted+" WHERE json_extract(payload_json,'$.source_id')=? AND json_extract(payload_json,'$.source_version')=? LIMIT 1",(source,version)).fetchone()
            else: shared=None
            if shared:
                raise AppError(409,'IMPORT_SOURCE_SHARED','Import requires a source version dedicated exclusively to file observations, without other source registrations or facts.')
        if indexes_available(self.db.conn):
            shared=self.db.conn.execute('SELECT 1 FROM message_observation INDEXED BY idx_file_non_candidate_source_v1 WHERE source_id=? AND source_version=? AND '+VALID+'(payload_json)=0 LIMIT 1',(source,version)).fetchone()
            if shared: raise AppError(409,'IMPORT_SOURCE_SHARED','Import source version already contains non-file observations.')
            return
        for row in self.db.conn.execute('SELECT payload_json FROM message_observation WHERE source_id=? AND source_version=?', (source, version)):
            if not self._candidate(json.loads(row[0])):
                raise AppError(409, 'IMPORT_SOURCE_SHARED', 'Import source version already contains non-file observations.')

    def _scope(self, account, device, source, version):
        batch_ids = set()
        # canonical_json's sorted object keys put observations first. Stream
        # its exact array bytes instead of retaining every identity in memory.
        hasher = hashlib.sha256(b'{"observations":[')
        count = 0
        indexed=indexes_available(self.db.conn)
        projection='observation_key,semantic_digest,'+BATCH+'(payload_json) AS batch_id' if indexed else 'observation_key,semantic_digest,payload_json'
        condition=' AND '+VALID+'(payload_json)=1' if indexed else ''
        index=' INDEXED BY idx_file_candidate_origin_v1' if indexed else ''
        for row in self.db.conn.execute('SELECT '+projection+' FROM message_observation'+index+' WHERE account_key=? AND device_key=? AND source_id=? AND source_version=?'+condition+' ORDER BY observation_key', (account, device, source, version)):
            value = {'batch_id':row['batch_id']} if indexed else self._candidate(json.loads(row['payload_json']))
            if value:
                count += 1
                if count > 1: hasher.update(b',')
                hasher.update(canonical_json([row['observation_key'],row['semantic_digest']]))
                if isinstance(value.get('batch_id'), str): batch_ids.add(value['batch_id'])
        result = {'kind': 'origin_source_version', 'account_key': account, 'device_key': device,
                'source_id': source, 'source_version': version, 'batch_ids': sorted(batch_ids),
                'affected_import_observations': count, 'batch_only': False,
                'local_source_verified_flag_disabled': True}
        row = self.db.conn.execute('SELECT * FROM source WHERE source_id=? AND source_version=?',(source,version)).fetchone()
        hasher.update(b'],"scope":'+canonical_json(result)+b',"source":'+canonical_json(dict(row) if row else None)+b',"withdrawn":'+canonical_json(self.identities.origin_withdrawn(device,account,source,version))+b'}')
        result['scope_hash'] = hasher.hexdigest()
        return result

    def _namespace_conflict(self, namespace, target):
        """Exact peer candidate namespace collision; target=(account,source,version)."""
        if indexes_available(self.db.conn):
            return self.db.conn.execute('SELECT 1 FROM message_observation INDEXED BY idx_file_candidate_namespace_v1 WHERE '+VALID+'(payload_json)=1 AND '+NAMESPACE+'(payload_json)=? AND (account_key!=? OR source_id!=? OR source_version!=?) LIMIT 1',(namespace,*target)).fetchone() is not None
        for row in self.db.conn.execute('SELECT account_key,source_id,source_version,payload_json FROM message_observation'):
            value=self._candidate(json.loads(row['payload_json']))
            if value and value.get('account_namespace')==namespace and (row['account_key'],row['source_id'],row['source_version'])!=target:
                return True
        return False

    @guarded
    def _prepare(self, preview, mapping, principal):
        self._schema()
        if type(preview) is not dict or type(mapping) is not dict:
            raise AppError(400, 'INVALID_IMPORT', 'Preview and explicit mapping must be JSON objects.')
        preview_bytes = _json_bytes(preview)
        mapping_bytes = _json_bytes(mapping, 2 * 1024 * 1024)
        required = {'account_key', 'source_id', 'source_version', 'account_namespace', 'records'}
        if set(mapping) != required:
            raise AppError(400, 'IMPORT_MAPPING_INVALID', 'Mapping requires only account/source/version/namespace and explicit records.')
        account, source, version, namespace = (mapping[k] for k in ('account_key', 'source_id', 'source_version', 'account_namespace'))
        for key, value, maximum in [('account_key',account,200),('source_id',source,200),('source_version',version,80),('account_namespace',namespace,256)]:
            require_text(value, key, maximum)
        self._authorize(principal, account)
        if preview.get('account_namespace') != namespace:
            raise AppError(409, 'IMPORT_NAMESPACE_MISMATCH', 'Preview namespace and explicit mapping differ.')
        if preview.get('format') not in ('douyin-chat-export.message_rows.v1','chatlab.export_records.v0.0.2'):
            raise AppError(400, 'IMPORT_FORMAT_UNSUPPORTED', 'Only the supported export preview versions can be imported.')
        if preview.get('persisted') is not False or preview.get('status') != 'offline_preview':
            raise AppError(400, 'IMPORT_PREVIEW_INVALID', 'An explicit offline preview is required.')
        account_row = self.db.conn.execute('SELECT * FROM own_account WHERE account_key=?', (account,)).fetchone()
        source_row = self.db.conn.execute('SELECT * FROM source WHERE source_id=? AND source_version=?', (source,version)).fetchone()
        if not account_row or not source_row:
            raise AppError(409, 'IMPORT_REGISTRATION_REQUIRED', 'Account and source must already be registered.')
        caps = json.loads(source_row['capabilities_json'])
        if source_row['source_kind'] == 'trusted_local_action' or any((name in caps if isinstance(caps,list) else caps.get(name) is True) for name in ('local_action','native_manual_action')):
            raise AppError(409, 'IMPORT_ACTION_SOURCE_REJECTED', 'An action-capable source cannot be borrowed for file imports.')
        if isinstance(caps,dict) and 'account_keys' in caps and account not in caps['account_keys']:
            raise AppError(403, 'IMPORT_SOURCE_ACCOUNT_DENIED', 'Registered source is restricted to other accounts.')
        self._dedicated(source, version)
        binding = self.db.conn.execute('SELECT * FROM file_import_binding WHERE account_namespace=?', (namespace,)).fetchone()
        target = (account,source,version)
        if binding and tuple(binding[k] for k in ('account_key','source_id','source_version')) != target:
            raise AppError(409, 'IMPORT_NAMESPACE_BOUND', 'This namespace is permanently bound to another account/source/version.')
        # Peers replay observations, not local batch bookkeeping. Their explicit
        # namespace/source declarations still prevent later cross-account rebinding.
        if self._namespace_conflict(namespace,target):
            raise AppError(409, 'IMPORT_NAMESPACE_BOUND', 'Received import observations already bind this namespace.')
        rows, selections = preview.get('records'), mapping['records']
        if type(rows) is not list or not 1 <= len(rows) <= MAX_VERSIONS or type(selections) is not dict:
            raise AppError(400, 'IMPORT_RECORDS_INVALID', 'Import requires between one and 2000 explicitly mapped records.')
        input_hash = digest(preview); mapping_hash = digest(mapping)
        batch = stable_key('file-import-batch',principal.device_key,account,source,version,namespace,input_hash,mapping_hash)
        keys, observations, alias_state, conflicts = set(), [], [], 0
        charged_bytes = 0
        for record in rows:
            if type(record) is not dict:
                raise AppError(400, 'IMPORT_RECORD_INVALID', 'Every record must be an object.')
            key = require_text(record.get('source_key'), 'source_key', 2048)
            if key in keys or record.get('account_namespace') != namespace:
                raise AppError(400, 'IMPORT_RECORD_INVALID', 'Record keys must be unique and share the explicit namespace.')
            keys.add(key)
            selection = selections.get(key)
            if type(selection) is not dict or set(selection) - {'object_kind','identity_kind','identity_value','platform_conversation_key'}:
                raise AppError(400, 'IMPORT_MAPPING_INVALID', 'Every record needs an explicit registered identity selection or empty unknown mapping.')
            triple = ('object_kind','identity_kind','identity_value')
            if any(k in selection for k in triple) and not all(k in selection for k in triple):
                raise AppError(400, 'IMPORT_MAPPING_INVALID', 'Identity mappings must supply the complete registered tuple.')
            identity = None
            if all(k in selection for k in triple):
                for field in triple: require_text(selection[field],field)
                if selection['object_kind'] not in ('person','group') or selection['identity_kind'] not in ('uid','sec_uid','group_id'):
                    raise AppError(400,'IMPORT_MAPPING_INVALID','Identity tuple has unsupported object or identity kind.')
                identity = self.identities.resolve(account,*(selection[k] for k in triple))
                if not identity:
                    raise AppError(409, 'IMPORT_ALIAS_NOT_REGISTERED', 'The selected identity must already be registered in this account.')
                alias_state.append(identity)
            conv = selection.get('platform_conversation_key')
            if conv is not None:
                require_text(conv,'platform_conversation_key')
                if not identity:
                    raise AppError(400,'IMPORT_MAPPING_INVALID','Conversation mapping requires a registered identity selection.')
                old = self.db.conn.execute('SELECT * FROM conversation WHERE account_key=? AND kind=? AND platform_conversation_key=?', (account,identity['object_kind'],conv)).fetchone()
                if old and old['person_key'] != identity['person_key']:
                    raise AppError(409,'IMPORT_CONVERSATION_CONFLICT','Registered conversation targets a different identity.')
                alias_state.append(dict(old) if old else {'unregistered_conversation_claim':conv})
            versions = record.get('versions')
            if type(versions) is not list or not versions:
                raise AppError(400,'IMPORT_VERSIONS_INVALID','Every record must preserve at least one original version.')
            if len(versions) > 1: conflicts += 1
            platform = record.get('platform_message_id') if record.get('key_origin') == 'platform_claim' else None
            if platform is not None: require_text(platform,'platform_message_key')
            record_claims = {k:v for k,v in record.items() if k != 'versions'}
            for candidate_version in versions:
                if len(observations) >= MAX_VERSIONS or type(candidate_version) is not dict:
                    raise AppError(400,'IMPORT_VERSIONS_LIMIT','Import version budget exceeded or malformed version.')
                body = candidate_version.get('original_body')
                if not isinstance(body,str) or '\x00' in body or len(body.encode('utf-8')) > 262144:
                    raise AppError(400,'IMPORT_ORIGINAL_BODY_INVALID','Original body must be a bounded string; null is not an empty message.')
                opaque = {'import_protocol':PROTOCOL,'batch_id':batch,'account_namespace':namespace,
                          'source_key':key,'record':record_claims,'version':candidate_version,'mapping':selection,
                          'preview_metadata':{k:v for k,v in preview.items() if k != 'records'}}
                raw = _json_bytes(opaque,MAX_CANDIDATE_BYTES).decode('utf-8')
                if '\x00' in raw:
                    raise AppError(400,'IMPORT_JSON_INVALID','Candidate contains an invalid NUL.')
                ordinal = len(observations)
                observation = {'observation_id':stable_key('file-import-record',batch,ordinal),'source_id':source,
                    'source_version':version,'account_key':account,'body':body,'kind':'unknown',
                    'direction':'unknown','platform_success':'unknown','time_precision':'unknown',
                    'occurred_lower_utc':None,'occurred_upper_utc':None,'coverage':{'state':'unknown'},
                    'platform_message_key':platform,'platform_conversation_key':conv,'export_candidate_json':raw}
                if identity: observation.update({k:selection[k] for k in triple})
                # One retained observation also becomes a domain-event payload.
                # Charge conservative copies before append, so repeated headers/
                # members cannot amplify a small preview into unbounded memory.
                charged_bytes += 3 * len(canonical_json(observation)) + 1024
                if charged_bytes > MAX_PREVIEW_BYTES:
                    raise AppError(413,'IMPORT_BATCH_OUTPUT_LIMIT','Expanded observations exceed the complete-batch byte budget.')
                observations.append((key,observation))
                reject_hidden(self.db,account,**observation_selectors(observation))
        if set(selections) != keys:
            raise AppError(400,'IMPORT_MAPPING_INVALID','Mappings must cover exactly the preview record keys.')
        old_batch = self.db.conn.execute('SELECT * FROM file_import_batch WHERE batch_id=?',(batch,)).fetchone()
        withdrawn = self.identities.origin_withdrawn(principal.device_key,account,source,version)
        if withdrawn and not old_batch:
            raise AppError(422,'SOURCE_WITHDRAWN','This origin source version was withdrawn; use a separately registered source version.')
        meta = dict(self.db.conn.execute("SELECT key,value FROM app_meta WHERE key IN ('data_version','device_key','epoch')"))
        state = {'data_version':meta.get('data_version'), 'device':meta.get('device_key'),'epoch':meta.get('epoch'),
                 'account':dict(account_row),'source':dict(source_row),'aliases':alias_state,
                 'binding':dict(binding) if binding else None,'withdrawn':withdrawn}
        scope = self._scope(account,principal.device_key,source,version)
        state['origin_scope_hash'] = scope['scope_hash']
        plan_hash = digest({'input':input_hash,'mapping':mapping_hash,'state':state,
                            'principal':{'operator':principal.operator_key,'device':principal.device_key,
                                         'role':principal.role,'scopes':sorted(principal.scopes),'accounts':sorted(principal.account_keys)}})
        counts = {'records':len(rows),'versions':len(observations),'conflicts':conflicts,'pending':len(observations)}
        return {'plan_hash':plan_hash,'batch_id':batch,'account_key':account,'source_id':source,
                'source_version':version,'account_namespace':namespace,'counts':counts,'authority':'pending_only',
                'persisted':False,'already_committed':bool(old_batch),'withdrawal_scope':scope,
                'output_budget':{'charged_bytes':charged_bytes,'limit_bytes':MAX_PREVIEW_BYTES}}, observations, old_batch

    @guarded
    def plan(self, preview, mapping, principal):
        with self.db.lock:
            return self._prepare(preview,mapping,principal)[0]

    @guarded
    def commit(self, preview, mapping, expected_plan_hash, principal):
        require_text(expected_plan_hash,'expected_plan_hash',128)
        with self.db.transaction() as conn:
            plan, observations, old = self._prepare(preview,mapping,principal)
            if old:
                if expected_plan_hash not in (old['plan_hash'],plan['plan_hash']):
                    raise AppError(409,'IMPORT_PLAN_STALE','Expected plan does not match this committed batch.')
                result = self._commit_result(plan,'duplicate')
                result['withdrawn'] = old['status'] == 'withdrawn'
                return result
            if expected_plan_hash != plan['plan_hash']:
                raise AppError(409,'IMPORT_PLAN_STALE','Database or mapping state changed; review a new plan before committing.')
            conn.execute('INSERT OR IGNORE INTO file_import_binding VALUES (?,?,?,?)',
                         (plan['account_namespace'],plan['account_key'],plan['source_id'],plan['source_version']))
            conn.execute('INSERT INTO file_import_batch VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         (plan['batch_id'],plan['account_key'],plan['source_id'],plan['source_version'],
                          plan['account_namespace'],principal.device_key,digest(preview),digest(mapping),
                          plan['plan_hash'],'committed',utc_now()))
            for ordinal,(key,observation) in enumerate(observations):
                saved = self.messages.ingest(observation,principal)
                if saved['message_key'] is not None or saved['status'] not in ('pending','duplicate'):
                    raise AppError(409,'IMPORT_AUTHORITY_GATE_FAILED','File evidence must remain pending-only.')
                conn.execute('INSERT INTO file_import_item VALUES (?,?,?,?)',
                             (plan['batch_id'],ordinal,key,saved['observation_key']))
            return self._commit_result(plan,'committed')

    def _commit_result(self, plan, status):
        keys = [r[0] for r in self.db.conn.execute('SELECT observation_key FROM file_import_item WHERE batch_id=? ORDER BY ordinal',(plan['batch_id'],))]
        device=self.db.conn.execute("SELECT value FROM app_meta WHERE key='device_key'").fetchone()[0]
        scope = self._scope(plan['account_key'],device,plan['source_id'],plan['source_version'])
        return {'status':status,'committed':True,'persisted':True,'batch_id':plan['batch_id'],
                'account_key':plan['account_key'],'counts':plan['counts'],'observation_keys':keys,
                'authority':'pending_only','withdrawal_scope':scope}

    def _pending_sender_uid(self,account,person,principal):
        """Resolve an existing active UID; this read registers or promotes nothing."""
        require_text(person,'person_key',200)
        owner=self.db.conn.execute('SELECT * FROM own_account WHERE account_key=?',(account,)).fetchone()
        contact=self.db.conn.execute('SELECT * FROM contact WHERE account_key=? AND person_key=?',(account,person)).fetchone()
        if not owner or not owner['verified'] or owner['platform']!='douyin' or not contact or not contact['verified'] or contact['object_kind']!='person':return None
        rows=self.db.conn.execute("SELECT * FROM identity_alias WHERE account_key=? AND person_key=? AND identity_kind='uid' LIMIT 2",(account,person)).fetchall()
        if len(rows)!=1:return None
        alias=dict(rows[0]);uid=alias['identity_value']
        if alias['platform']!='douyin' or alias['object_kind']!='person' or not alias['verified'] or not re.fullmatch('[0-9]{1,128}',uid):return None
        if (not re.fullmatch('[0-9]{1,128}',owner['platform_uid']) or uid==owner['platform_uid'] or
                account!=stable_key('account','douyin',owner['platform_uid']) or
                person!=stable_key('person',account,'douyin','uid',uid)):return None
        for row,capability in ((owner,'account_identity'),(alias,'person_identity')):
            source=self.identities.get_source(row['source_id'],row['source_version'])
            if (not source or source['source_kind']!='qualified_adapter' or
                    not self.identities.trusted(row['source_id'],row['source_version'],capability,account) or
                    self.identities.origin_withdrawn(principal.device_key,account,row['source_id'],row['source_version'])):return None
        # A direct cross-key alias retains its independently registered pair.
        if alias['mapping_state']=='verified_direct':
            try:proof=json.loads(alias['evidence_json'])
            except (ValueError,TypeError):return None
            source=self.identities.get_source(alias['source_id'],alias['source_version'])
            records=source['evidence'].get('direct_identity_records',[]) if isinstance(source['evidence'],dict) else []
            if (not self.identities.trusted(alias['source_id'],alias['source_version'],'direct_identity_mapping',account) or
                    type(proof) is not dict or proof.get('account_key')!=account or proof.get('uid')!=uid or proof not in records):return None
            pair=self.db.conn.execute("SELECT * FROM identity_alias WHERE account_key=? AND platform='douyin' AND object_kind='person' AND identity_kind='sec_uid' AND identity_value=?",(account,proof.get('sec_uid'))).fetchone()
            if (not pair or not pair['verified'] or pair['mapping_state']!='verified_direct' or pair['person_key']!=person or
                    pair['source_id']!=alias['source_id'] or pair['source_version']!=alias['source_version'] or
                    pair['evidence_json']!=alias['evidence_json']):return None
        elif alias['mapping_state']!='separate':return None
        return uid

    def _pending_sender_row(self,row,payload,item,binding,uid,preview_sha256):
        """Do not combine conflicting revisions or use a declaration from another batch."""
        if self.identities.origin_withdrawn(row['device_key'],binding['account_key'],row['source_id'],row['source_version']):
            raise AppError(409,'IMPORT_SENDER_SOURCE_WITHDRAWN','来源已撤回，未按此对象展示原文。')
        receipt=self.db.conn.execute('''SELECT i.source_key,i.batch_id,b.account_key,b.source_id,b.source_version,
            b.account_namespace,b.device_key,b.status,b.input_digest FROM file_import_item i
            JOIN file_import_batch b ON b.batch_id=i.batch_id WHERE i.observation_key=?''',(row['observation_key'],)).fetchone()
        expected=tuple(binding[key] for key in ('account_key','source_id','source_version','account_namespace'))
        if (receipt is None or tuple(receipt[key] for key in ('account_key','source_id','source_version','account_namespace'))!=expected or
                tuple(row[key] for key in ('account_key','source_id','source_version'))!=expected[:3] or
                row['message_key'] is not None or row['status']!='pending' or receipt['status']!='committed' or
                receipt['device_key']!=row['device_key'] or receipt['batch_id']!=item['batch_id'] or
                receipt['source_key']!=item['source_key'] or item['account_namespace']!=binding['account_namespace'] or
                item['record'].get('source_key')!=item['source_key'] or item['record'].get('account_namespace')!=binding['account_namespace'] or
                item['version'].get('platform_sender_id')!=uid or item['version'].get('original_body')!=payload.get('body') or
                item['record'].get('revision_state')!='single_candidate' or
                (preview_sha256 is not None and receipt['input_digest']!=preview_sha256) or
                self.db.conn.execute('SELECT COUNT(*) FROM file_import_item WHERE batch_id=? AND source_key=?',
                    (item['batch_id'],item['source_key'])).fetchone()[0]!=1):
            raise AppError(409,'IMPORT_SENDER_EVIDENCE_CONFLICT','待核验记录的来源或版本不一致，未按此对象展示。')

    @guarded
    def observations(self, account_key, principal, *, batch_id=None, keyword=None, before=None, limit=50,
                     person_key=None,binding=None,preview_sha256=None):
        with self.db.lock:
            self._schema(); self._authorize(principal,account_key,write=False)
            if type(limit) is not int or not 1 <= limit <= 200:
                raise AppError(400,'INVALID_LIMIT','Observation limit must be between one and 200.')
            if batch_id is not None: require_text(batch_id,'batch_id',200)
            if keyword is not None and (not isinstance(keyword,str) or len(keyword)>200 or '\x00' in keyword):
                raise AppError(400,'INVALID_KEYWORD','Search requires bounded literal text.')
            selected=person_key is not None or binding is not None or preview_sha256 is not None
            sender_scope=None;uid=None
            if selected:
                from .import_binding import ImportBindingService
                ImportBindingService._binding(binding)
                if binding['account_key']!=account_key:raise AppError(403,'IMPORT_SOURCE_ACCOUNT_DENIED','筛选来源不属于当前账号。')
                if preview_sha256 is not None and (type(preview_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',preview_sha256)):
                    raise AppError(400,'IMPORT_SENDER_SCOPE_INVALID','批准预览摘要无效。')
                source=self.db.conn.execute('SELECT * FROM source WHERE source_id=? AND source_version=?',(binding['source_id'],binding['source_version'])).fetchone()
                if source is None:raise AppError(409,'IMPORT_REGISTRATION_REQUIRED','来源尚未登记。')
                ImportBindingService._caps(source,account_key)
                permanent=self.db.conn.execute('SELECT * FROM file_import_binding WHERE account_namespace=?',(binding['account_namespace'],)).fetchone()
                if permanent is None or any(permanent[k]!=v for k,v in binding.items()):raise AppError(409,'IMPORT_NAMESPACE_BOUND','已保存来源的命名空间绑定缺失或不一致。')
                uid=self._pending_sender_uid(account_key,person_key,principal)
                sender_scope={'person_key':person_key,'kind':'export_sender_claim','binding':dict(binding),
                    'state':'ready' if uid else 'identity_unavailable','private_chat_scope':'unknown','coverage':'unknown'}
                if uid is None:return {'items':[],'batches':[],'next_cursor':None,'coverage':'unknown','authority':'pending_only','sender_scope':sender_scope}
            scope_hash = digest([account_key,batch_id,keyword,*([person_key,binding,uid,preview_sha256] if selected else [])])
            candidate_sql="CASE WHEN json_valid(json_extract(payload_json,'$.export_candidate_json')) THEN json_extract(payload_json,'$.export_candidate_json') ELSE '{}' END"
            params=[account_key]; clauses=['account_key=?',sql_visible(self.db,'o','observation'),"json_extract(payload_json,'$.export_candidate_json') IS NOT NULL",
                'json_extract('+candidate_sql+",'$.import_protocol')=?"]
            params.append(PROTOCOL)
            if selected:
                clauses.extend(['source_id=?','source_version=?','json_extract('+candidate_sql+",'$.account_namespace')=?",
                    'json_type('+candidate_sql+",'$.version.platform_sender_id')='text'",
                    'json_extract('+candidate_sql+",'$.version.platform_sender_id')=?"])
                params.extend((binding['source_id'],binding['source_version'],binding['account_namespace'],uid))
                if preview_sha256 is not None:
                    clauses.append('EXISTS (SELECT 1 FROM file_import_item pi JOIN file_import_batch pb ON pb.batch_id=pi.batch_id WHERE pi.observation_key=o.observation_key AND pb.input_digest=?)')
                    params.append(preview_sha256)
            for key,kind in [('batch_id','text'),('source_key','text'),('account_namespace','text'),('record','object'),('version','object'),('mapping','object')]:
                clauses.append('json_type('+candidate_sql+",'$.' || ?) = ?")
                params.extend((key,kind))
            if batch_id is not None:
                clauses.append('json_extract('+candidate_sql+",'$.batch_id')=?");params.append(batch_id)
            if keyword is not None:
                clauses.append("instr(json_extract(payload_json,'$.body'),?)>0");params.append(keyword)
            if before is not None:
                try:
                    if not isinstance(before,str) or len(before)>1024: raise ValueError()
                    cursor=json.loads(base64.urlsafe_b64decode(before.encode('ascii')))
                    if set(cursor)!={'scope','key'} or cursor['scope']!=scope_hash: raise ValueError()
                    require_text(cursor['key'],'cursor',200)
                except (ValueError,TypeError,UnicodeError,AppError) as exc:
                    raise AppError(400,'INVALID_CURSOR','Cursor is invalid for the selected account/search/batch.') from exc
                clauses.append('observation_key<?');params.append(cursor['key'])
            params.append(limit+1)
            rows=self.db.conn.execute('SELECT o.* FROM message_observation o WHERE '+' AND '.join(clauses)+' ORDER BY observation_key DESC LIMIT ?',params).fetchall()
            items=[]; batches={}; origins={}
            for row in rows[:limit]:
                payload=json.loads(row['payload_json']); candidate=self._candidate(payload)
                if candidate is None: continue
                if selected:self._pending_sender_row(row,payload,candidate,binding,uid,preview_sha256)
                origin_key=(row['device_key'],row['source_id'],row['source_version'])
                if origin_key not in origins: origins[origin_key]=self._scope(account_key,*origin_key)
                origin=origins[origin_key]
                batch=candidate['batch_id']
                withdrawn=self.identities.origin_withdrawn(row['device_key'],account_key,row['source_id'],row['source_version'])
                items.append({'batch_id':batch,'observation_key':row['observation_key'],'source_key':candidate['source_key'],
                    'body':payload['body'],'export_candidate':candidate,'quality':json.loads(row['quality_json']),
                    'status':row['status'],'withdrawn':withdrawn,'authority':'pending_only','withdrawal_scope':origin})
                batches[batch]={'batch_id':batch,'withdrawn':withdrawn,'withdrawal_scope':origin,
                                'withdrawable_here':row['device_key']==principal.device_key and
                                    self.db.conn.execute('SELECT 1 FROM file_import_batch WHERE batch_id=?',(batch,)).fetchone() is not None}
            next_cursor=base64.urlsafe_b64encode(canonical_json({'scope':scope_hash,'key':rows[limit-1]['observation_key']})).decode('ascii') if len(rows)>limit else None
            result={'items':items,'batches':list(batches.values()),'next_cursor':next_cursor,'coverage':'unknown','authority':'pending_only'}
            if selected:result['sender_scope']=sender_scope
            return result

    @guarded
    def withdraw(self, batch_id, principal, *, account_key=None, expected_scope_hash=None):
        require_text(batch_id,'batch_id',200)
        with self.db.transaction() as conn:
            self._schema()
            batch=conn.execute('SELECT * FROM file_import_batch WHERE batch_id=?',(batch_id,)).fetchone()
            if not batch: raise AppError(404,'IMPORT_BATCH_NOT_FOUND','No local import batch was found.')
            self._authorize(principal,batch['account_key'])
            if account_key is not None and account_key!=batch['account_key']:
                raise AppError(403,'IMPORT_ACCOUNT_MISMATCH','The requested account does not own this batch.')
            if batch['device_key']!=principal.device_key:
                raise AppError(403,'IMPORT_LOCAL_DEVICE_REQUIRED','A peer batch cannot be withdrawn by this device.')
            self._dedicated(batch['source_id'],batch['source_version'])
            scope=self._scope(batch['account_key'],batch['device_key'],batch['source_id'],batch['source_version'])
            already=self.identities.origin_withdrawn(batch['device_key'],batch['account_key'],batch['source_id'],batch['source_version'])
            if not already and expected_scope_hash != scope['scope_hash']:
                raise AppError(409,'IMPORT_WITHDRAWAL_SCOPE_STALE','Withdrawal scope changed or was not reviewed; inspect the current source-wide scope first.')
            result=self.identities.withdraw_origin(batch['device_key'],batch['account_key'],batch['source_id'],batch['source_version'],reason='file_import_origin_withdrawn',emit_event=True)
            conn.execute("UPDATE file_import_batch SET status='withdrawn' WHERE account_key=? AND device_key=? AND source_id=? AND source_version=?",
                         (batch['account_key'],batch['device_key'],batch['source_id'],batch['source_version']))
            return {'status':result['status'],'batch_id':batch_id,'account_key':batch['account_key'],
                    'withdrawal_scope':scope,'affected_batch_ids':scope['batch_ids'],
                    'affected_import_observations':scope['affected_import_observations'],
                    'originals_retained':True,'batch_only':False,'authority':'pending_only'}
