from dataclasses import asdict
from .auth import AuthStore
from .db import Database
from .identity import IdentityService
from .message_facts import MessageFacts
from .send_evidence import SendEvidence
from .settings import read_settings, VERSIONS
from .common import AppError

class Application:
    def __init__(self, paths, *, auth=None, database=None, key_store=None, read_only=False):
        self.paths = paths
        self.config = read_settings(paths.settings)
        if read_only and database is not None and not database.read_only:raise AppError(400,'READ_ONLY_DATABASE_REQUIRED','只读回退需要只读数据库连接')
        self.db = database if database is not None else Database(paths.db, create=False,read_only=read_only)
        try:
            self._initialize(paths,auth=auth,database=database,key_store=key_store,read_only=read_only)
        except BaseException:
            # A failed constructor never transfers caller-owned resources.
            # Preserve the startup error even if releasing our connection fails.
            if database is None:
                try:self.db.close()
                except BaseException:pass
            raise

    def _initialize(self, paths, *, auth, database, key_store, read_only):
        read_only=bool(read_only or self.db.read_only)
        # A fixture suppression DB cannot open normal product access. Only a
        # caller-owned, authenticated fixture coordinator may bind an injected DB.
        from .local_deletion import LogicalDeletionCoordinator, require_ready
        with self.db.lock:
            fixture_tables=self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND lower(name) GLOB 'local_deletion_*' LIMIT 1").fetchone()
            if fixture_tables:
                gate=getattr(self.db,'_deletion_gate',None)
                if database is not self.db or not isinstance(gate,LogicalDeletionCoordinator) or gate.db is not self.db:
                    raise AppError(503,'LOCAL_DELETION_HOLD','Fixture deletion recovery is required; ordinary product startup remains unavailable.')
                require_ready(self.db)
        meta=dict(self.db.conn.execute('SELECT key,value FROM app_meta'))
        if not self.config['device_key'] or meta.get('device_key') != self.config['device_key']:
            raise AppError(409,'device_identity_mismatch','配置与本地事件设备登记不一致')
        if fixture_tables and self.config['source_mode']!='fictional':
            raise AppError(503,'LOCAL_DELETION_HOLD','Deletion coordinators may only bind explicit fictional application fixtures.')
        self.auth = auth or AuthStore(paths.auth, allowed_extension_ids=self.config['extension_ids'])
        self.identities = IdentityService(self.db)
        self.messages = MessageFacts(self.db)
        self.actions = SendEvidence(self.db)
        from .deletion_manifest import DeletionManifestService
        self.deletion_manifest=DeletionManifestService(self.db)
        from .keys import KeyStore
        self.keys = key_store or KeyStore(paths.root/'keys'/'backup.dpapi')
        self.profiles=None;self.projections=None;self.history=None;self.ai=None
        if meta.get('candidate_features')=='1':
            from .profile import ProfileService
            from .projections import ProjectionService
            from .history_index import HistoryIndex
            self.profiles=ProfileService(self.db);self.projections=ProjectionService(self.db);self.history=HistoryIndex(self.db)
        self.file_import=None
        if meta.get('file_import_schema_version')=='1':
            from .file_import import FileImportService
            self.file_import=FileImportService(self.db,history=self.history)
        if meta.get('candidate_ai')=='1':
            from .ai_jobs import AiService
            self.ai=AiService(self.db)  # No registration, download, socket, scheduler or worker on startup.
        from .contact_stats import ContactStatistics
        self.contact_stats=ContactStatistics(self.db)
        from .sync_runtime import SyncRuntime
        sync_file=paths.root/'sync-config.json';sync_config=None
        if sync_file.is_symlink() or getattr(sync_file,'is_junction',lambda:False)():
            raise AppError(503,'SYNC_CONFIG_INVALID','本机同步配置不得通过链接读取其他位置')
        if sync_file.exists():
            import json
            try:
                if sync_file.stat().st_size>16384:raise ValueError()
                sync_config=json.loads(sync_file.read_text(encoding='utf-8'))
            except (OSError,ValueError):raise AppError(503,'SYNC_CONFIG_INVALID','本机同步配置无效；未启动传输') from None
        services=[self.ai] if self.ai else []
        self.activity_adapter=None;self.activity_retention=None
        self.activity_read=None
        if meta.get('activity_schema')=='1':
            from .activity_read import ActivityReadService
            self.activity_read=ActivityReadService(self.db)
        if meta.get('activity_adapter_schema')=='1':
            from .activity_adapter import ActivityReceiveAdapter
            self.activity_adapter=ActivityReceiveAdapter(self.db);services.append(self.activity_adapter)
        if meta.get('activity_retention_schema')=='1':
            from .activity_retention import ActivityRetention
            self.activity_retention=ActivityRetention(self.db)
        self.sync=SyncRuntime(self.db,paths.root,sync_config,services=services,activity_authority=self.activity_adapter.authority if self.activity_adapter else None)
        # Real source activation is deliberately absent until independently verified protection and adapters.
        self.writes_enabled = self.config['source_mode'] == 'fictional' and not read_only
        self.read_only=read_only
        self.candidate_review=None
        self.local_notes=None  # Explicit host attachment only; configuration alone grants nothing.
        # Reader construction never installs or enables the separate local store.
        from .diagnostics import DiagnosticsStore
        self.diagnostics=DiagnosticsStore(paths.root/'diagnostics'/'events.sqlite3',self.config['device_key'],runtime_root=paths.root)
    def close(self):
        try:
            if getattr(self,'diagnostics',None):self.diagnostics.close()
        finally:self.db.close()
    def health(self):
        sync_ready=self.sync.readiness()
        return {'versions':dict(VERSIONS),'state':'candidate','writes_enabled':self.writes_enabled,
                'read_only_rollback':self.read_only,'sync_state':'explicit_candidate' if sync_ready['configured'] else 'unconfigured',
                'sync_reason':sync_ready['reason'],
                'source_mode':self.config['source_mode'],'storage_protection':{'declared':self.config['storage_protection'],'verification':'unverified'},
                'real_source_activation':'unavailable','model_state':'unverified' if self.ai else 'not_initialized',
                'local_notes_state':('stopped' if self.local_notes.blocked else 'explicit_local_notes') if self.local_notes else 'disabled'}

def principal_json(principal):
    value = asdict(principal)
    for key in ('scopes','account_keys'):
        value[key] = sorted(value[key])
    return value
