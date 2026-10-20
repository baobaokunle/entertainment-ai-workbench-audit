"""Fictional D2 suppression prototype. Original bodies and signed wires retained."""
import hmac
from functools import wraps
from .common import AppError, canonical_json
from .db import digest, stable_key
from .deletion_manifest import DeletionManifestService
from .deletion_intents import AuthenticatedDeletionLog, validate_intent


def guarded(method):
    """Hold the DB lock through readiness, visibility and complete business action."""
    @wraps(method)
    def call(self,*args,**kwargs):
        with self.db.lock:
            require_ready(self.db)
            return method(self,*args,**kwargs)
    return call


def reject_hidden(db, account_key, *, message_key=None, observation_key=None, job_key=None, **selectors):
    if matches_observation(db,account_key,message_key=message_key,observation_key=observation_key,**selectors) or (job_key and is_hidden_job(db,job_key,account_key)):
        raise AppError(410,'LOCAL_DELETION_SUPPRESSED','This exact object is locally suppressed; copies remain pending.')


def observation_selectors(raw):
    candidate=DeletionManifestService._json(raw.get('export_candidate_json')) or {}
    return dict(source_id=raw.get('source_id'),source_version=raw.get('source_version','1'),
                account_namespace=candidate.get('account_namespace'),source_key=candidate.get('source_key'),
                platform_conversation_key=raw.get('platform_conversation_key'),platform_message_key=raw.get('platform_message_key'))


def _hold():
    raise AppError(503, 'LOCAL_DELETION_HOLD', 'Authenticated deletion recovery must finish before business access.')


def install_schema(db, *, fixture_mode=False):
    if fixture_mode is not True:
        raise AppError(403, 'DELETION_FIXTURE_ONLY', 'Explicit fictional schema installation required.')
    with db.transaction():
        for statement in (
            'CREATE TABLE IF NOT EXISTS local_deletion_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS local_deletion_request(intent_id TEXT PRIMARY KEY,intent_json TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS local_deletion_target(account_key TEXT,target_kind TEXT,target_key TEXT,version_digest TEXT,PRIMARY KEY(account_key,target_kind,target_key))',
            'CREATE TABLE IF NOT EXISTS local_deletion_alias(selector_digest TEXT PRIMARY KEY,selector_json TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS local_deletion_event(event_id TEXT PRIMARY KEY,event_digest TEXT NOT NULL,account_key TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS local_deletion_job(account_key TEXT,job_key TEXT,PRIMARY KEY(account_key,job_key))',
        ): db.conn.execute(statement)
        db.conn.execute("INSERT OR IGNORE INTO local_deletion_meta VALUES('fixture_format','d2-fixture-1')")
        db.conn.execute("INSERT OR IGNORE INTO app_meta VALUES('data_version','0')")
        for table in ('request', 'target', 'alias', 'event', 'job'):
            db.conn.execute('CREATE TRIGGER IF NOT EXISTS local_deletion_version_'+table+' AFTER INSERT ON local_deletion_'+table+" BEGIN UPDATE app_meta SET value=CAST(value AS INTEGER)+1 WHERE key='data_version'; END")


def require_ready(db):
    with db.lock:
        names={r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND lower(name) GLOB 'local_deletion_*'")}
        installed=bool(names)
        if not installed: return
        if not {'local_deletion_meta','local_deletion_request','local_deletion_target','local_deletion_alias','local_deletion_event','local_deletion_job'} <= names:_hold()
        gate = getattr(db, '_deletion_gate', None)
        if gate is None or gate.db is not db or not gate.ready: _hold()
        persisted = dict(db.conn.execute('SELECT key,value FROM local_deletion_meta'))
        if persisted.get('fixture_format')!='d2-fixture-1' or persisted.get('head')!=gate.expected_head:
            gate.ready=False
            _hold()
        try: gate.log.read(expected_head=gate.expected_head)
        except BaseException:
            gate.ready = False
            _hold()


def sql_visible(db, alias='m', kind='message'):
    require_ready(db)
    if alias not in ('m', 'message', 'o', 'observation', 'mo') or kind not in ('message', 'observation'):
        raise ValueError('Finite SQL alias and target kind required')
    if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletion_target'").fetchone(): return '1=1'
    key = 'message_key' if kind == 'message' else 'observation_key'
    # Exact persisted observation IDs. Ingest uses matches_observation for new IDs.
    return "NOT EXISTS (SELECT 1 FROM local_deletion_target dt WHERE dt.account_key="+alias+".account_key AND dt.target_kind='"+kind+"' AND dt.target_key="+alias+'.'+key+')'


def sql_visible_job(db, alias='j'):
    require_ready(db)
    if alias not in ('j','ai_job'): raise ValueError('Finite job alias required')
    if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletion_job'").fetchone():return '1=1'
    return 'NOT EXISTS (SELECT 1 FROM local_deletion_job dj WHERE dj.account_key='+alias+'.account_key AND dj.job_key='+alias+'.job_key)'


def is_hidden_action(db,account,key):
    require_ready(db)
    if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletion_event'").fetchone():return False
    return bool(db.conn.execute("SELECT 1 FROM domain_event e JOIN local_deletion_event de ON de.event_id=e.event_id AND de.event_digest=e.event_digest WHERE e.account_key=? AND e.target_key=? AND e.event_type='action.candidate'",(account,key)).fetchone())


def _target(db, account, kind, key):
    return bool(key and db.conn.execute('SELECT 1 FROM local_deletion_target WHERE account_key=? AND target_kind=? AND target_key=?', (account,kind,key)).fetchone())


def matches_observation(db, account_key, *, message_key=None, observation_key=None,
                        source_id=None, source_version=None, account_namespace=None, source_key=None,
                        platform_conversation_key=None, platform_message_key=None):
    require_ready(db)
    if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletion_target'").fetchone(): return False
    if _target(db,account_key,'message',message_key) or _target(db,account_key,'observation',observation_key): return True
    candidates=[]
    if all(isinstance(x,str) and x for x in (source_id,source_version,account_namespace,source_key)):
        candidates.append(dict(kind='file_record',account_key=account_key,source_id=source_id,source_version=source_version,account_namespace=account_namespace,source_key=source_key))
    if all(isinstance(x,str) and x for x in (platform_conversation_key,platform_message_key)):
        candidates.append(dict(kind='platform_message',account_key=account_key,platform_conversation_key=platform_conversation_key,platform_message_key=platform_message_key))
    return any(db.conn.execute('SELECT 1 FROM local_deletion_alias WHERE selector_digest=? AND selector_json=?',(digest(x),canonical_json(x).decode())).fetchone() for x in candidates)


def is_hidden_job(db, key, account_key=None):
    require_ready(db)
    if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletion_job'").fetchone(): return False
    if account_key is None:
        return bool(db.conn.execute('SELECT 1 FROM local_deletion_job WHERE job_key=?',(key,)).fetchone())
    return bool(db.conn.execute('SELECT 1 FROM local_deletion_job WHERE account_key=? AND job_key=?',(account_key,key)).fetchone())


def event_requires_hold(db, event):
    require_ready(db)
    if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletion_event'").fetchone(): return False
    ref=db.conn.execute('SELECT event_digest,account_key FROM local_deletion_event WHERE event_id=?',(event.get('event_id'),)).fetchone()
    if ref:
        if ref[0]!=event.get('event_digest') or ref[1]!=event.get('account_key'):
            raise AppError(409,'DELETION_EVENT_CONFLICT','Immutable event identity mismatch must not be swallowed by suppression.')
        return True
    account=event.get('account_key')
    payload=event.get('payload',{})
    if event.get('event_type') in ('message.observed','observation.pending') and isinstance(payload,dict):
        raw=payload.get('observation')
        if isinstance(raw,dict):
            candidate=DeletionManifestService._json(raw.get('export_candidate_json')) or {}
            if matches_observation(db,account,
                message_key=payload.get('message_key'),observation_key=payload.get('observation_key'),
                source_id=raw.get('source_id'),source_version=raw.get('source_version','1'),
                account_namespace=candidate.get('account_namespace'),source_key=candidate.get('source_key'),
                platform_conversation_key=raw.get('platform_conversation_key'),platform_message_key=raw.get('platform_message_key')):
                return True
    for kind,key in DeletionManifestService._references(event.get('payload',{})):
        if kind=='job_key' and is_hidden_job(db,key,account): return True
        if kind=='action_key' and is_hidden_action(db,account,key):return True
        if kind in ('message_key','observation_key') and _target(db,account,kind[:-4],key): return True
    return False


class LogicalDeletionCoordinator:
    def __init__(self, db, log, expected_head, *, fixture_mode=False):
        if fixture_mode is not True or not isinstance(log,AuthenticatedDeletionLog):
            raise AppError(403,'DELETION_FIXTURE_ONLY','Product execution remains disabled.')
        if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletion_meta'").fetchone(): _hold()
        if getattr(db,'_deletion_gate',None) is not None: _hold()
        self.db,self.log,self.expected_head,self.ready=db,log,expected_head,False
        db._deletion_gate=self

    def _apply(self, intent):
        validate_intent(intent)
        local=dict(self.db.conn.execute('SELECT key,value FROM app_meta')).get('device_key')
        if local!=intent['device_key']: _hold()
        conn=self.db.conn
        raw=canonical_json(intent).decode()
        prior=conn.execute('SELECT intent_json FROM local_deletion_request WHERE intent_id=?',(intent['intent_id'],)).fetchone()
        if prior:
            if prior[0]!=raw: _hold()
        conn.execute('INSERT OR IGNORE INTO local_deletion_request VALUES(?,?)',(intent['intent_id'],raw))
        for t in intent['targets']:
            conn.execute('INSERT OR IGNORE INTO local_deletion_target VALUES(?,?,?,?)',tuple(t[k] for k in ('account_key','target_kind','target_key','version_digest')))
        for a in intent['alias_selectors']:
            conn.execute('INSERT OR IGNORE INTO local_deletion_alias VALUES(?,?)',(digest(a),canonical_json(a).decode()))
        for e in intent['event_refs']:
            prior=conn.execute('SELECT event_digest,account_key FROM local_deletion_event WHERE event_id=?',(e['event_id'],)).fetchone()
            if prior and tuple(prior)!=(e['event_digest'],intent['account_key']): _hold()
            conn.execute('INSERT OR IGNORE INTO local_deletion_event VALUES(?,?,?)',(e['event_id'],e['event_digest'],intent['account_key']))
        for key in intent['ai_job_keys']:
            inserted=conn.execute('INSERT OR IGNORE INTO local_deletion_job VALUES(?,?)',(intent['account_key'],key)).rowcount
            # State/generation is projection metadata; retain all context/result bodies.
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ai_job'").fetchone():
                changed=conn.execute("UPDATE ai_job SET state='suppressed',error_code='LOCAL_DELETION_SUPPRESSED' WHERE account_key=? AND job_key=? AND state!='suppressed'",(intent['account_key'],key)).rowcount
            else:changed=0
            if (inserted or changed) and conn.execute("SELECT 1 FROM sqlite_master WHERE name='ai_execution'").fetchone():
                conn.execute('INSERT INTO ai_execution VALUES(?,0,1) ON CONFLICT(job_key) DO UPDATE SET generation=generation+1',(key,))

    def recover(self, *, expected_head=None):
        with self.db.lock:
            self.ready=False
            if expected_head is not None: self.expected_head=expected_head
            records=self.log.read(expected_head=self.expected_head)
            with self.db.transaction():
                existing={r[0] for r in self.db.conn.execute('SELECT intent_id FROM local_deletion_request')}
                if not existing <= {r['intent']['intent_id'] for r in records}: _hold()
                for record in records: self._apply(record['intent'])
                self.db.conn.execute("INSERT INTO local_deletion_meta VALUES('head',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(self.expected_head,))
            self.ready=True
            return self._result()

    def _result(self):
        return dict(state='local_suppressed',copies_state='copies_pending',physical_purged=False,
                    business_body_removed=False,product_entry_enabled=False,head=self.expected_head)

    def commit(self, account_key,target_kind,target_key,principal,*,expected_plan_hash):
        with self.db.lock:
            require_ready(self.db)
            self.ready=False
            try:
                with self.db.transaction():
                    manifest=DeletionManifestService(self.db).plan(account_key,target_kind,target_key,principal)
                    if not isinstance(expected_plan_hash,str) or not hmac.compare_digest(manifest['plan_hash'],expected_plan_hash):
                        raise AppError(409,'DELETION_PLAN_CHANGED','Display fresh full manifest before commit.')
                    events={}
                    for e in manifest['event_refs']:
                        if e['event_id'] in events and events[e['event_id']]!=e['event_digest']: _hold()
                        events[e['event_id']]=e['event_digest']
                    intent=dict(intent_id=stable_key('deletion_intent',account_key,manifest['metadata']['device_key'],expected_plan_hash),account_key=account_key,device_key=manifest['metadata']['device_key'],operator_key=principal.operator_key,plan_hash=expected_plan_hash,targets=manifest['targets'],alias_selectors=manifest['alias_selectors'],event_refs=[dict(event_id=k,event_digest=v) for k,v in sorted(events.items())],ai_job_keys=manifest['ai_job_keys'])
                    result=self.log.append(intent,principal,expected_head=self.expected_head,local_device_key=intent['device_key'])
                    self.expected_head=result['head'] # Caller must persist this independently, including on crash.
                    self._apply(intent)
                    self.db.conn.execute("INSERT INTO local_deletion_meta VALUES('head',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(self.expected_head,))
                self.ready=True
                return self._result()
            except BaseException:
                self.ready=False
                raise
