"""Explicit local sync orchestration. No transport installer, background task or source approval."""
import re,json,secrets
from pathlib import Path
from .common import AppError
from .local_deletion import require_ready
from .keys import KeyStore
from .security import DPAPIProtector
from .sync.schema import require_schema
from .sync.pairing import PairingStore
from .sync.crypto import PackageCodec,MAX_BYTES
from .sync.importer import Importer
from .sync.packager import Packager
from .sync.acks import Acks
from .sync.journal import RecoveryJournal

CHANNELS={'messages','actions','profile','collection','config','control','model','activities'}
CHANNEL_TYPES={'messages':('message.observed','observation.pending','source.revoked'),'actions':('action.candidate','send.attribution'),'profile':('scalar.set','scalar.clear','scalar.add','scalar.remove','relation.observation','spark.observation'),'collection':('rank.revision','session.revision','session.catalog'),'config':('collection.config.request','collection.config.result'),'model':('ai.request','ai.result','ai.control','ai.preference'),'activities':('activity.observed',),'control':()}
EVENT_CHANNEL={t:channel for channel,event_types in CHANNEL_TYPES.items() for t in event_types}

def _owned_root(root,db):
    candidate=Path(root)
    if not candidate.is_absolute(): raise AppError(422,'SYNC_ROOT_INVALID','Explicit absolute runtime root required.')
    for path in (candidate,*candidate.parents):
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)(): raise AppError(422,'SYNC_ROOT_INVALID','Runtime root must not traverse a link or junction.')
    candidate=candidate.resolve()
    if db.path.is_relative_to(candidate/'sync'): raise AppError(422,'SYNC_ROOT_INVALID','Database must remain outside transport.')
    return candidate

class SyncRuntime:
    """A disabled/unconfigured instance is read-only and never creates storage."""
    @classmethod
    def initialize(cls,db,root,*,protector=None):
        require_schema(db);root=_owned_root(root,db);protector=protector or DPAPIProtector()
        for relative in ('keys','keys/sync-signing.dpapi','keys/sync-journal.dpapi','staging/sync','sync/incoming','sync/outgoing','journal/sync'):
            path=root/relative
            for component in (path,*[p for p in path.parents if p.is_relative_to(root)]):
                if component.is_symlink() or getattr(component,'is_junction',lambda:False)(): raise AppError(422,'SYNC_PATH_INVALID','Owned sync paths cannot traverse links.')
        stores=[KeyStore(root/'keys'/filename,protector) for filename in ('sync-signing.dpapi','sync-journal.dpapi')]
        existing=[s.path.exists() for s in stores]
        with db.lock:
            durable=any(db.conn.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone() is not None for table in ('sync_peer','sync_key','sync_publication','sync_package')) or db.conn.execute("SELECT 1 FROM sync_meta WHERE key='runtime_key_initialization'").fetchone() is not None
        journal=root/'journal/sync'
        durable=durable or journal.is_dir() and any(journal.iterdir())
        if not all(existing) and (any(existing) or durable): raise AppError(409,'SYNC_KEY_RECOVERY_REQUIRED','Existing sync identity or journal requires recovery of the original protected key pair.')
        if all(existing):
            for store in stores: store.load()
        else:
            # Prepare both protected payloads before changing durable identity.
            # A persistent marker makes any crash or missing-file state fail
            # closed; no subsequent initialize can silently mint replacement keys.
            protected=[protector.protect(KeyStore.KEY_MAGIC+secrets.token_bytes(32)) for _ in stores]
        for name in ('keys','staging/sync','sync/incoming','sync/outgoing','journal/sync'):
            path=root/name
            for component in (path,*[p for p in path.parents if p.is_relative_to(root)]):
                if component.is_symlink() or getattr(component,'is_junction',lambda:False)(): raise AppError(422,'SYNC_PATH_INVALID','Owned sync paths cannot traverse links.')
            path.mkdir(parents=True,exist_ok=True)
        if not all(existing):
            with db.transaction() as conn: conn.execute("INSERT INTO sync_meta VALUES('runtime_key_initialization','started')")
            for store,blob in zip(stores,protected): KeyStore._new_file(store.path,blob)
            for store in stores: store.load()
        with db.transaction() as conn: conn.execute("INSERT INTO sync_meta VALUES('runtime_key_initialization','complete') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        return {'initialized':True,'source_state':'fictional_only','transport_started':False,'tasks_installed':False}

    def __init__(self,db,root=None,config=None,*,protector=None,services=(),activity_authority=None):
        self.db=db;self.configured=False;self.reason='not_configured';self.config=config;self.protector=protector or DPAPIProtector()
        self.services=tuple(services);self.root=None;self.activity_authority=activity_authority;self.collection=None;self.config_requests=None;self.ai=None
        if root is None or config is None: return
        if not isinstance(config,dict) or set(config)-{'enabled','peer_device','channel_keys','source_mode','collector_device'} or not {'enabled','peer_device','channel_keys','source_mode'}<=set(config) or type(config['enabled'])is not bool: raise AppError(422,'SYNC_CONFIG_INVALID','Finite local sync configuration required.')
        if config.get('collector_device') is not None and (not isinstance(config['collector_device'],str) or not config['collector_device'] or len(config['collector_device'])>200): raise AppError(422,'SYNC_CONFIG_INVALID','Explicit designated collection device required.')
        if config['source_mode']!='fictional': raise AppError(503,'SYNC_REAL_SOURCE_DISABLED','Runtime accepts isolated fictional deployment only.')
        if not config['enabled']: self.reason='disabled';return
        if not isinstance(config['peer_device'],str) or not config['peer_device'] or len(config['peer_device'])>200 or not isinstance(config['channel_keys'],dict) or not config['channel_keys'] or set(config['channel_keys'])-CHANNELS or 'control' not in config['channel_keys'] or any(not isinstance(v,str) or not v or len(v)>80 for v in config['channel_keys'].values()): raise AppError(422,'SYNC_CONFIG_INVALID','Explicit peer and finite per-channel key IDs required.')
        for restricted in ('model','activities'):
            if restricted in config['channel_keys'] and any(key==config['channel_keys'][restricted] for c,key in config['channel_keys'].items() if c!=restricted): raise AppError(422,'SYNC_KEY_SEPARATION','Model and activity channels require independent key identities.')
        self.root=_owned_root(root,db)
        required=('keys','staging/sync','sync/incoming','sync/outgoing','journal/sync')
        if any(not (self.root/p).is_dir() for p in required): self.reason='directories_not_initialized';return
        for relative in required+('keys/sync-signing.dpapi','keys/sync-journal.dpapi'):
            path=self.root/relative
            for component in (path,*[p for p in path.parents if p.is_relative_to(self.root)]):
                if component.is_symlink() or getattr(component,'is_junction',lambda:False)(): raise AppError(422,'SYNC_PATH_INVALID','Owned sync paths cannot traverse links.')
        require_schema(db)
        if not all((self.root/'keys'/name).is_file() for name in ('sync-signing.dpapi','sync-journal.dpapi')): self.reason='protected_keys_not_initialized';return
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.signing=Ed25519PrivateKey.from_private_bytes(KeyStore(self.root/'keys/sync-signing.dpapi',self.protector).load())
        self.pairing=PairingStore(db,self.protector)
        with db.lock: self.device=dict(db.conn.execute('SELECT key,value FROM app_meta'))['device_key']
        self.codec=PackageCodec(self.pairing,self.device,self.signing)
        self.journal=RecoveryJournal(self.root/'journal/sync',key=KeyStore(self.root/'keys/sync-journal.dpapi',self.protector).load(),transport_dir=self.root/'sync')
        self.importer=Importer(db,self.codec,journal=self.journal)
        self.packager=Packager(db,self.codec,staging_dir=self.root/'staging/sync',send_dir=self.root/'sync/outgoing',journal=self.journal)
        self.acks=Acks(db,self.codec,staging_dir=self.root/'staging/sync',send_dir=self.root/'sync/outgoing')
        from .sync.scalar import ScalarStore
        from .rank_facts import RankFacts
        from .session_facts import SessionFacts
        from .sync.handlers import register_fact_handlers
        from .history_index import HistoryIndex
        from .projections import ProjectionService
        try: history=HistoryIndex(db)
        except AppError: history=None
        register_fact_handlers(self.importer,history=history)
        from .collection import CollectionService
        from .sync.config import ConfigRequests
        from .ai_jobs import AiService
        provided=list(self.services)
        if config.get('collector_device') and not any(isinstance(s,ConfigRequests) for s in provided):
            self.collection=CollectionService(db,owner_device=config['collector_device']);self.config_requests=ConfigRequests(db,self.collection);provided.append(self.config_requests)
        else: self.config_requests=next((s for s in provided if isinstance(s,ConfigRequests)),None)
        if self.config_requests: self.collection=self.config_requests.collection
        self.ai=next((s for s in provided if isinstance(s,AiService)),None)
        if self.ai is None:
            try: self.ai=AiService(db);provided.append(self.ai)
            except AppError as exc:
                if exc.code!='AI_MIGRATION_REQUIRED': raise
        for service in (ScalarStore(db),RankFacts(db),SessionFacts(db),*provided):
            if hasattr(service,'register_handlers'): service.register_handlers(self.importer)
        try: ProjectionService(db).register_handlers(self.importer)
        except AppError as exc:
            if exc.code!='CD_NOT_INITIALIZED': raise
        for event_type,handler in list(self.importer.handlers.items()):
            self.importer.handlers[event_type]=self._fictional_handler(handler)
        self.configured=True;self.reason=None

    def _fictional_handler(self,handler):
        def apply(conn,event):
            expected=EVENT_CHANNEL.get(event.get('event_type'))
            if expected and event['channel']!=expected: raise AppError(422,'SYNC_EVENT_CHANNEL_INVALID','Domain event must use its canonical channel.')
            if event['channel']=='activities':
                raw=event['payload'].get('event',{})
                if not self.activity_authority: raise AppError(409,'DEPENDENCY_PENDING','Independent designated activity source approval required.')
                if event['account_key']!='device:'+event['device_key'] or raw.get('device_key')!=event['device_key'] or not event.get('actor') or event['actor']['device_key']!=event['device_key'] or self.activity_authority(event) is not True: raise AppError(403,'ACTIVITY_SOURCE_UNAPPROVED','Activity device and raw source must match independent local approval.')
                handler(conn,event);return
            row=conn.execute('SELECT a.verified,s.source_kind,s.verified AS source_verified FROM own_account a JOIN source s ON s.source_id=a.source_id AND s.source_version=a.source_version WHERE a.account_key=?',(event['account_key'],)).fetchone()
            if row is None: raise AppError(409,'DEPENDENCY_PENDING','Independent local account approval is required.')
            if row['source_kind']!='fictional': raise AppError(503,'SYNC_REAL_SOURCE_DISABLED','Only independently approved fictional accounts may be applied.')
            lowering=event.get('event_type')=='source.revoked' or event.get('event_type')=='send.attribution' and event.get('payload',{}).get('actor_state')=='actor_unknown'
            # Withdrawing authority must remain deliverable after that authority
            # was withdrawn. Domain handlers still validate the signed origin,
            # finite body and target; pairing still limits account/type/channel.
            if not lowering and (not row['verified'] or not row['source_verified']): raise AppError(409,'DEPENDENCY_PENDING','Independent local fictional account provenance is required.')
            handler(conn,event)
        return apply

    def public_signing_key(self):
        self._require()
        from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
        return self.signing.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)

    def _require(self):
        require_ready(self.db)
        if not self.configured: raise AppError(409,'SYNC_NOT_CONFIGURED','Explicit protected sync initialization and configuration required.')

    def _key(self,channel):
        self._require()
        if channel not in self.config['channel_keys']: raise AppError(409,'SYNC_CHANNEL_NOT_CONFIGURED','Channel key is not configured.')
        self.pairing.authorize(self.config['peer_device'],channel=channel)
        key_id=self.config['channel_keys'][channel];self.pairing.key(key_id,publish=True,channel=channel)
        return key_id

    def publish(self,channel,*,limit=100):
        key_id=self._key(channel)
        self._key('control')
        if channel=='control': raise AppError(422,'SYNC_CHANNEL_INVALID','Control ACKs are queued by verified DATA receipt only.')
        if not any(t in self.importer.handlers for t in CHANNEL_TYPES[channel]) or channel=='activities' and not self.activity_authority: raise AppError(409,'SYNC_CHANNEL_UNSUPPORTED','Independent domain handler and source authority are unavailable.')
        priority=False
        with self.db.lock:
            if channel=='messages':
                rows=self.packager.withdrawal_candidates(self.config['peer_device'])
                priority=bool(rows)
            if priority:
                for event in rows:self._fictional_handler(lambda conn,event:None)(self.db.conn,event)
            else:
                from .sync.events import from_row
                rows=self.db.conn.execute("SELECT e.* FROM domain_event e JOIN outbox o USING(event_id) WHERE e.device_key=? AND e.channel=? AND (o.state='pending' OR EXISTS(SELECT 1 FROM sync_publication_event m JOIN sync_publication p USING(package_id) WHERE m.event_id=e.event_id AND p.peer_key=? AND p.state='prepared'))",(self.device,channel,self.config['peer_device'])).fetchall()
                for row in rows: self._fictional_handler(lambda conn,event:None)(self.db.conn,from_row(row))
        package=self.packager.prepare(self.config['peer_device'],channel,key_id=key_id,limit=limit,event_types=('source.revoked',) if priority else None)
        if package is None: return {'status':'empty','channel':channel}
        self.packager.publish(package)
        return {'status':'published','package_id':package,'filename':package+'.dfa','priority':'source.revoked' if priority else None}

    def import_file(self,filename):
        self._key('control')
        if not isinstance(filename,str) or not re.fullmatch(r'[0-9a-f]{32}\.dfa',filename): raise AppError(422,'SYNC_FILE_INVALID','Opaque final incoming filename required.')
        path=self.root/'sync/incoming'/filename
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)() or not path.is_file() or path.stat().st_size>MAX_BYTES: raise AppError(422,'SYNC_FILE_INVALID','Owned bounded final incoming package required.')
        wire=path.read_bytes();header,_=self.codec.open(wire)
        if header['sender']!=self.config['peer_device'] or header['package_id']!=filename[:-4]: raise AppError(403,'SYNC_FILE_ROUTE_INVALID','File identity and explicitly configured peer must match.')
        result=self.importer.receive(wire)
        if result['ack_required']:
            control=self.acks.queue(self.importer,result['package_id'],header['sender'],'control',key_id=self._key('control'));self.acks.publish(control);result['control_id']=control
        return result

    def retry_dependencies(self):
        self._key('control');self.importer.retry_staged();queued=[]
        with self.db.lock: packages=[dict(r) for r in self.db.conn.execute('SELECT package_id,wire FROM sync_package')]
        for p in packages:
            header,_=self.codec.open(bytes(p['wire']))
            if header['kind']=='DATA' and header['sender']==self.config['peer_device']:
                control=self.acks.queue(self.importer,p['package_id'],header['sender'],'control',key_id=self._key('control'));self.acks.publish(control);queued.append(control)
        return {'queued_controls':sorted(set(queued))}

    def retry_publication(self,package_id):
        self._require()
        if not isinstance(package_id,str) or not re.fullmatch(r'[0-9a-f]{32}',package_id): raise AppError(422,'SYNC_PACKAGE_INVALID','Opaque publication identifier required.')
        with self.db.lock: row=self.db.conn.execute('SELECT peer_key,channel FROM sync_publication WHERE package_id=?',(package_id,)).fetchone()
        if not row or row['peer_key']!=self.config['peer_device']: raise AppError(403,'SYNC_PUBLICATION_ROUTE_INVALID','Publication must belong to the configured peer.')
        self._key(row['channel'])
        from .sync.events import from_row
        from .sync.handlers import validate_withdrawal_event
        with self.db.lock:
            for member in self.db.conn.execute('SELECT e.* FROM sync_publication_event p JOIN domain_event e USING(event_id) WHERE p.package_id=?',(package_id,)):
                event=from_row(member)
                if event['event_type']=='source.revoked':validate_withdrawal_event(event)
                self._fictional_handler(lambda conn,event:None)(self.db.conn,event)
        self.packager.retry(package_id);return {'status':'published','package_id':package_id}

    def preserve_before_restore(self):
        self._require();entry=self.journal.preserve_pending(self.db)
        return {'preserved':entry is not None,'new_epoch_allowed':False}

    def request_collection_change(self,account,payload,principal):
        self._require()
        if not self.config_requests: raise AppError(409,'COLLECTOR_NOT_CONFIGURED','Designated collection owner is not configured.')
        self._fictional_handler(lambda conn,event:None)(self.db.conn,{'account_key':account,'channel':'config'})
        return self.config_requests.request(account,payload,principal)

    def initialize_collection_plan(self,principal):
        self._require()
        if not self.collection: raise AppError(409,'COLLECTOR_NOT_CONFIGURED','Designated collection owner is not configured.')
        self.collection._authorize(principal);self.collection.initialize()
        return {'initialized':True,'task_installed':False,'collection_started':False}

    def process_collection_requests(self,principal):
        self._require()
        if not self.config_requests: raise AppError(409,'COLLECTOR_NOT_CONFIGURED','Designated collection owner is not configured.')
        return self.config_requests.process_pending(principal)

    def recover_journal(self):
        self._require();local=self.journal.recover_local(self.db,self.importer.handlers,codec=self.importer.codec);incoming=self.journal.replay(self.importer)
        return {'local':local,'incoming':incoming,'new_epoch_allowed':False}

    def readiness(self):
        """Current pairing/key authorization, without scanning receipt ledgers."""
        baseline={'configured':self.configured,'reason':self.reason,'source_state':'fictional_only','transport_started':False,'tasks_installed':False,'coverage':'transport_ledger_only'}
        if not self.configured: return baseline
        try:
            for channel,key_id in self.config['channel_keys'].items():
                self.pairing.authorize(self.config['peer_device'],channel=channel)
                self.pairing.key(key_id,publish=True,channel=channel)
        except AppError as exc:
            baseline.update(configured=False,initialized=True,reason=exc.code)
        return baseline

    def status(self):
        baseline=self.readiness()
        if not self.configured: return baseline
        with self.db.lock:
            publications=[dict(r) for r in self.db.conn.execute('SELECT package_id,peer_key,channel,state,created_at FROM sync_publication')]
            outbox={r[0]:r[1] for r in self.db.conn.execute('SELECT state,count(*) FROM outbox GROUP BY state')}
        supported={c:('supported' if c=='control' or any(t in self.importer.handlers for t in CHANNEL_TYPES[c]) and (c!='activities' or self.activity_authority) else 'unsupported') for c in self.config['channel_keys']}
        return {**baseline,'peer_device':self.config['peer_device'],'channels':sorted(self.config['channel_keys']),'channel_support':supported,'collector_device':self.config.get('collector_device'),'collection_requests':self.config_requests.status() if self.config_requests else None,'handlers':sorted(self.importer.handlers),'receipt':self.importer.status(),'publications':publications,'outbox':outbox,'controls':self.acks.pending()}
