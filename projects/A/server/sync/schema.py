"""Explicit additive B migration; never called by a normal read or service constructor."""
from ..common import AppError

DDL = '''
CREATE TABLE IF NOT EXISTS sync_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_peer(device_key TEXT PRIMARY KEY,public_key BLOB NOT NULL,scope_json TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sync_key(key_id TEXT PRIMARY KEY,key_bytes BLOB NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sync_key_channel(key_id TEXT NOT NULL REFERENCES sync_key(key_id),channel TEXT NOT NULL,active INTEGER NOT NULL,PRIMARY KEY(key_id,channel));
CREATE TABLE IF NOT EXISTS sync_package(package_id TEXT PRIMARY KEY,digest TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,wire BLOB NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_package_event(package_id TEXT NOT NULL REFERENCES sync_package(package_id),event_id TEXT NOT NULL,PRIMARY KEY(package_id,event_id));
CREATE TABLE IF NOT EXISTS sync_received(event_id TEXT PRIMARY KEY,event_digest TEXT NOT NULL,device_key TEXT NOT NULL,epoch TEXT NOT NULL,channel TEXT NOT NULL,seq INTEGER NOT NULL,event_json TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,UNIQUE(device_key,epoch,channel,seq));
CREATE TABLE IF NOT EXISTS sync_watermark(device_key TEXT NOT NULL,epoch TEXT NOT NULL,channel TEXT NOT NULL,received INTEGER NOT NULL DEFAULT 0,processed INTEGER NOT NULL DEFAULT 0,applied INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(device_key,epoch,channel));
CREATE TABLE IF NOT EXISTS sync_publication(package_id TEXT PRIMARY KEY,peer_key TEXT NOT NULL,channel TEXT NOT NULL,wire BLOB NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_publication_event(package_id TEXT NOT NULL REFERENCES sync_publication(package_id),event_id TEXT NOT NULL REFERENCES domain_event(event_id),PRIMARY KEY(package_id,event_id));
CREATE TABLE IF NOT EXISTS sync_ack(package_id TEXT PRIMARY KEY,ack_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_control_outbox(control_id TEXT PRIMARY KEY,record_digest TEXT NOT NULL UNIQUE,recipient TEXT NOT NULL,channel TEXT NOT NULL,wire BLOB NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_rejection(wire_digest TEXT PRIMARY KEY,reason TEXT NOT NULL,received_at TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'terminal_rejected');
CREATE TABLE IF NOT EXISTS causal_revision(event_id TEXT PRIMARY KEY,account_key TEXT NOT NULL,target_key TEXT NOT NULL,field TEXT NOT NULL,operation TEXT NOT NULL,value_json TEXT NOT NULL,parents_json TEXT NOT NULL,event_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_config(task_key TEXT PRIMARY KEY,version INTEGER NOT NULL,enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),hour INTEGER NOT NULL CHECK(hour BETWEEN 0 AND 23),minute INTEGER NOT NULL CHECK(minute BETWEEN 0 AND 59),owner_device TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_run(run_key TEXT PRIMARY KEY,task_key TEXT NOT NULL,day TEXT NOT NULL,trigger_kind TEXT NOT NULL,state TEXT NOT NULL,owner_device TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,reason TEXT,UNIQUE(task_key,day));
CREATE UNIQUE INDEX IF NOT EXISTS one_collection_running ON collection_run(task_key) WHERE state='running';
CREATE TABLE IF NOT EXISTS collection_gap(task_key TEXT NOT NULL,day TEXT NOT NULL,reason TEXT NOT NULL,resolved INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(task_key,day));
CREATE TABLE IF NOT EXISTS config_request(request_id TEXT PRIMARY KEY,account_key TEXT NOT NULL,expected_version INTEGER NOT NULL,request_json TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,result_version INTEGER);
CREATE TABLE IF NOT EXISTS rank_revision(revision_key TEXT PRIMARY KEY,account_key TEXT NOT NULL,rank_kind TEXT NOT NULL,period_key TEXT NOT NULL,source_id TEXT NOT NULL,source_version TEXT NOT NULL,state TEXT NOT NULL,payload_json TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rank_active(account_key TEXT NOT NULL,rank_kind TEXT NOT NULL,period_key TEXT NOT NULL,revision_key TEXT NOT NULL REFERENCES rank_revision(revision_key),PRIMARY KEY(account_key,rank_kind,period_key));
CREATE TABLE IF NOT EXISTS session_revision(revision_key TEXT PRIMARY KEY,account_key TEXT NOT NULL,session_key TEXT NOT NULL,source_id TEXT NOT NULL,source_version TEXT NOT NULL,state TEXT NOT NULL,payload_json TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_active(account_key TEXT NOT NULL,session_key TEXT NOT NULL,revision_key TEXT NOT NULL REFERENCES session_revision(revision_key),PRIMARY KEY(account_key,session_key));
CREATE TABLE IF NOT EXISTS session_catalog_revision(revision_key TEXT PRIMARY KEY,account_key TEXT NOT NULL,state TEXT NOT NULL,payload_json TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_catalog_active(account_key TEXT PRIMARY KEY,revision_key TEXT NOT NULL REFERENCES session_catalog_revision(revision_key));
'''

def install_schema(db):
    """Caller must explicitly invoke during init/migrate, before starting workers."""
    with db.transaction() as conn:
        for statement in DDL.split(';'):
            if statement.strip(): conn.execute(statement)
        prior = conn.execute("SELECT value FROM sync_meta WHERE key='schema_version'").fetchone()
        if prior and prior[0] != '1':
            raise AppError(409,'SYNC_SCHEMA_INCOMPATIBLE','Explicit compatible sync migration required.')
        conn.execute("INSERT OR IGNORE INTO sync_meta VALUES('schema_version','1')")
        from ..db import install_data_version
        install_data_version(conn)

def require_schema(db):
    with db.lock:
        row = db.conn.execute("SELECT name FROM sqlite_master WHERE name='sync_meta'").fetchone()
        version = db.conn.execute("SELECT value FROM sync_meta WHERE key='schema_version'").fetchone() if row else None
    if not version or version[0] != '1':
        raise AppError(409,'SYNC_NOT_INITIALIZED','Explicit phase B initialization required.')
