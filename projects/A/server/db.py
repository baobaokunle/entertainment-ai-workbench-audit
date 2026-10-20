"""Explicit versioned SQLite database and durable local event transactions."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import sqlite3
import threading
import uuid

from .common import AppError, canonical_json, utc_now

SCHEMA_VERSION = 2


def stable_key(kind, *parts):
    return kind + '_' + str(uuid.uuid5(uuid.NAMESPACE_URL, canonical_json([kind, *parts]).decode('utf-8')))


def digest(data):
    return hashlib.sha256(canonical_json(data)).hexdigest()


def reject_floats(value):
    if isinstance(value, float):
        raise AppError(400, 'FLOAT_UNSUPPORTED', 'Domain evidence must not use floating point values.')
    if isinstance(value, dict):
        for child in value.values():
            reject_floats(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            reject_floats(child)


def _legacy_data_version(conn):
    """Read-only baseline for an older candidate awaiting explicit migration."""
    value=conn.execute('SELECT COUNT(*) FROM domain_event').fetchone()[0]
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sync_received','sync_meta')")}
    if 'sync_received' in tables:value+=conn.execute("SELECT COUNT(*) FROM sync_received WHERE state='applied'").fetchone()[0]
    if 'sync_meta' in tables:value+=conn.execute("SELECT COUNT(*) FROM sync_meta WHERE key GLOB 'withdrawal_preapplied:*'").fetchone()[0]
    return value


def install_data_version(conn):
    """Explicit initialization/migration only; triggers share fact transactions.

    This is a change token, never a transport watermark or coverage assertion.
    The persisted counter survives restart and evidence retention without
    decreasing; rollback also rolls back its increments.
    """
    baseline=_legacy_data_version(conn)
    conn.execute("INSERT INTO app_meta VALUES('data_version',?) ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(app_meta.value AS INTEGER),CAST(excluded.value AS INTEGER))",(str(baseline),))
    change="UPDATE app_meta SET value=CAST(value AS INTEGER)+1 WHERE key='data_version';"
    conn.execute('CREATE TRIGGER IF NOT EXISTS data_version_domain_insert AFTER INSERT ON domain_event BEGIN '+change+' END')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_received'").fetchone():
        conn.execute("CREATE TRIGGER IF NOT EXISTS data_version_received_insert AFTER INSERT ON sync_received WHEN NEW.state='applied' BEGIN "+change+' END')
        conn.execute("CREATE TRIGGER IF NOT EXISTS data_version_received_apply AFTER UPDATE OF state ON sync_received WHEN OLD.state!='applied' AND NEW.state='applied' BEGIN "+change+' END')
        conn.execute("CREATE TRIGGER IF NOT EXISTS data_version_withdrawal AFTER INSERT ON sync_meta WHEN NEW.key GLOB 'withdrawal_preapplied:*' BEGIN "+change+' END')


class Database:
    def __init__(self, path, create=False, *, read_only=False):
        self.path = Path(path).resolve()
        self.lock = threading.RLock()
        self.read_only=read_only
        if read_only and create:raise AppError(400,'READ_ONLY_CREATE','只读回退不能初始化或迁移数据库')
        self._savepoint = 0
        if not self.path.exists() and not create:
            raise AppError(409, 'DATABASE_NOT_INITIALIZED', 'Database requires explicit initialization.')
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = 'ro' if read_only else 'rwc' if create else 'rw'
        self.conn = sqlite3.connect(self.path.as_uri() + '?mode=' + mode, uri=True, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        from .file_import_index import register_import_functions
        register_import_functions(self.conn)
        try:
            # History/import sorting must not spill plaintext into the process
            # TEMP directory, which may be outside the protected workspace.
            self.conn.execute('PRAGMA temp_store=MEMORY')
            if self.conn.execute('PRAGMA temp_store').fetchone()[0] != 2:
                raise AppError(503, 'DATABASE_TEMP_PROTECTION_REQUIRED', 'Database temporary storage must remain in memory.')
            version = self.conn.execute('PRAGMA user_version').fetchone()[0]
            tables = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version == 0 and not tables and create:
                schema = Path(__file__).with_name('schema.sql').read_text(encoding='utf-8')
                self.conn.executescript('BEGIN IMMEDIATE;\n' + schema + '\nCOMMIT;')
                with self.transaction() as conn:
                    conn.executemany('INSERT INTO app_meta VALUES (?,?)', [('schema_version','2'), ('device_key','local_' + uuid.uuid4().hex), ('epoch',uuid.uuid4().hex)])
                    install_data_version(conn)
            elif version != SCHEMA_VERSION:
                raise AppError(409, 'SCHEMA_INCOMPATIBLE', 'Database schema is incompatible; explicit migration required.')
            expected = {'app_meta','source','own_account','contact','identity_alias','conversation','message','message_revision','message_observation','domain_event','outbox','migration_journal'}
            names = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not expected.issubset(names):
                raise AppError(409, 'SCHEMA_INCOMPATIBLE', 'Database schema is incomplete.')
            self.conn.execute('PRAGMA foreign_keys=ON')
            if not read_only:self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('PRAGMA synchronous=FULL')
            self.conn.execute('PRAGMA busy_timeout=5000')
            if read_only:self.conn.execute('PRAGMA query_only=ON')
        except Exception:
            self.conn.close()
            raise

    @contextmanager
    def transaction(self):
        if self.read_only:raise AppError(403,'READ_ONLY_BUILD','只读回退保留原文和同步证据，禁止领域写入')
        with self.lock:
            nested = self.conn.in_transaction
            self._savepoint += 1
            savepoint = 'tx_' + str(self._savepoint)
            self.conn.execute('SAVEPOINT ' + savepoint if nested else 'BEGIN IMMEDIATE')
            try:
                yield self.conn
                self.conn.execute('RELEASE ' + savepoint if nested else 'COMMIT')
            except BaseException:
                if nested:
                    self.conn.execute('ROLLBACK TO ' + savepoint)
                    self.conn.execute('RELEASE ' + savepoint)
                else:
                    self.conn.execute('ROLLBACK')
                raise

    def health(self):
        with self.lock:
            version=self.conn.execute("SELECT value FROM app_meta WHERE key='data_version'").fetchone()
            return {'schema_version':self.conn.execute('PRAGMA user_version').fetchone()[0], 'journal_mode':self.conn.execute('PRAGMA journal_mode').fetchone()[0], 'synchronous':self.conn.execute('PRAGMA synchronous').fetchone()[0], 'foreign_keys':bool(self.conn.execute('PRAGMA foreign_keys').fetchone()[0]), 'data_version':int(version[0]) if version else _legacy_data_version(self.conn)}

    def close(self):
        with self.lock:
            self.conn.close()

    def set_runtime_identity(self, device_key, epoch=None):
        if not isinstance(device_key, str) or not device_key or len(device_key)>200:
            raise AppError(400, 'INVALID_DEVICE', 'A stable local device identifier is required.')
        with self.transaction() as conn:
            current = dict(conn.execute('SELECT key,value FROM app_meta'))
            if conn.execute('SELECT 1 FROM domain_event LIMIT 1').fetchone() and current['device_key'] != device_key:
                raise AppError(409, 'DEVICE_IDENTITY_CONFLICT', 'Existing event device identity cannot be replaced.')
            conn.execute("UPDATE app_meta SET value=? WHERE key='device_key'", (device_key,))
            if epoch is not None:
                if not isinstance(epoch,str) or not epoch:
                    raise AppError(400, 'INVALID_EPOCH', 'A stable local epoch is required.')
                if current['epoch'] != epoch and conn.execute('SELECT 1 FROM domain_event LIMIT 1').fetchone():
                    raise AppError(409, 'EPOCH_CONFLICT', 'Explicit recovery procedure required to change an event epoch.')
                conn.execute("UPDATE app_meta SET value=? WHERE key='epoch'", (epoch,))
            return dict(conn.execute('SELECT key,value FROM app_meta'))

    def append_event(self, conn, *, account_key, target_key, event_type, payload, parents=(), channel='messages', actor=None):
        reject_floats(payload)
        if not isinstance(parents,(list,tuple)) or len(parents)>100 or any(not isinstance(x,str) or not x for x in parents):
            raise AppError(400,'INVALID_PARENTS','事件父版本无效')
        parents=sorted(set(parents))
        meta = dict(conn.execute('SELECT key,value FROM app_meta'))
        device, epoch = meta['device_key'], meta['epoch']
        row = conn.execute('SELECT seq FROM event_sequence WHERE device_key=? AND epoch=? AND channel=?', (device,epoch,channel)).fetchone()
        seq = (row[0] if row else 0) + 1
        conn.execute('INSERT INTO event_sequence VALUES (?,?,?,?) ON CONFLICT(device_key,epoch,channel) DO UPDATE SET seq=excluded.seq', (device,epoch,channel,seq))
        event_id = stable_key('event',device,epoch,channel,seq)
        now = utc_now()
        event = {'event_id':event_id,'device_key':device,'epoch':epoch,'channel':channel,'seq':seq,'account_key':account_key,'target_key':target_key,'event_type':event_type,'actor':actor,'parents':list(parents),'created_at_utc':now,'payload':payload,'payload_hash':digest(payload),'schema_version':2}
        event_digest = digest(event)
        conn.execute('INSERT INTO domain_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (event_id,device,epoch,channel,seq,account_key,target_key,event_type,event['payload_hash'],event_digest,canonical_json(list(parents)).decode(),canonical_json(event).decode(),now))
        conn.execute('INSERT INTO outbox(event_id,created_at_utc) VALUES (?,?)', (event_id,now))
        return event_id
