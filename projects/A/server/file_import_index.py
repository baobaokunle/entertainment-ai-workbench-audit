"""Explicit candidate expression indexes; no projections or schema-on-read.

Unregistered legacy/native writers fail closed while these UDF indexes exist.
Explicit uninstall restores their write compatibility. This is not an authority
or deletion index: original retained observations remain the evidence.
"""
import json
import hashlib

from .common import AppError

PROTOCOL = 'file-import-candidate-v1'
VERSION = '1'
VALID = 'dfa_file_candidate_valid_v1'
NAMESPACE = 'dfa_file_candidate_namespace_v1'
BATCH = 'dfa_file_candidate_batch_v1'


def candidate(payload):
    raw = payload.get('export_candidate_json')
    if not isinstance(raw, str): return None
    try: value = json.loads(raw)
    except (ValueError, RecursionError): return None
    if not isinstance(value, dict) or value.get('import_protocol') != PROTOCOL: return None
    for field, maximum in [('batch_id',200), ('source_key',2048), ('account_namespace',256)]:
        child = value.get(field)
        if not isinstance(child,str) or not child.strip() or len(child)>maximum or '\x00' in child: return None
    if any(type(value.get(key)) is not dict for key in ('record','version','mapping')): return None
    return value


def _parse(raw):
    # Existing service assumes outer payloads are objects. Refuse malformed
    # outer JSON at an index write, rather than silently classify it non-file.
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError('Invalid observation payload')
    return candidate(value)


def register_import_functions(conn):
    conn.create_function(VALID, 1, lambda raw: int(_parse(raw) is not None), deterministic=True)
    conn.create_function(NAMESPACE, 1, lambda raw: (_parse(raw) or {}).get('account_namespace'), deterministic=True)
    conn.create_function(BATCH, 1, lambda raw: (_parse(raw) or {}).get('batch_id'), deterministic=True)


CORE_INDEXES = {
    'idx_file_candidate_namespace_v1': f'CREATE INDEX idx_file_candidate_namespace_v1 ON message_observation({NAMESPACE}(payload_json),account_key,source_id,source_version) WHERE {VALID}(payload_json)=1',
    'idx_file_candidate_origin_v1': f'CREATE INDEX idx_file_candidate_origin_v1 ON message_observation(account_key,device_key,source_id,source_version,observation_key,semantic_digest,{BATCH}(payload_json)) WHERE {VALID}(payload_json)=1',
    'idx_file_non_candidate_source_v1': f'CREATE INDEX idx_file_non_candidate_source_v1 ON message_observation(source_id,source_version) WHERE {VALID}(payload_json)=0',
}


def indexes_available(conn):
    flag = conn.execute("SELECT value FROM app_meta WHERE key='file_import_index_version'").fetchone()
    if not flag: return False
    if flag[0] != VERSION:
        raise AppError(409,'FILE_IMPORT_INDEX_INCOMPATIBLE','Explicit compatible import index migration required.')
    actual = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND name LIKE 'idx_file_%'"))
    if any(actual.get(name) != sql for name,sql in CORE_INDEXES.items()):
        raise AppError(409,'FILE_IMPORT_INDEX_INCOMPATIBLE','Import index definition is incomplete or incompatible.')
    return True


def _source_indexes(conn):
    result={}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        table=row[0]
        if table.startswith('sqlite_'): continue
        quoted='"'+table.replace('"','""')+'"'
        columns={r[1] for r in conn.execute('PRAGMA table_info('+quoted+')')}
        if {'source_id','source_version'}.issubset(columns):
            name='idx_file_source_v1_'+hashlib.sha256(table.encode()).hexdigest()[:16]
            result[name]='CREATE INDEX '+name+' ON '+quoted+'(source_id,source_version)'
        elif table in ('session_catalog_revision','activity_event') and 'payload_json' in columns:
            name='idx_file_payload_source_v1_'+table
            result[name]='CREATE INDEX '+name+' ON '+quoted+"(json_extract(payload_json,'$.source_id'),json_extract(payload_json,'$.source_version'))"
    return result


def install_import_indexes(db):
    """Explicit, atomic additive migration, including retained peer candidates."""
    with db.transaction() as conn:
        flag = conn.execute("SELECT value FROM app_meta WHERE key='file_import_index_version'").fetchone()
        if flag and flag[0] != VERSION:
            raise AppError(409,'FILE_IMPORT_INDEX_INCOMPATIBLE','Explicit compatible import index migration required.')
        for name,sql in CORE_INDEXES.items():
            existing=conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone()
            if existing and existing[0]!=sql:
                raise AppError(409,'FILE_IMPORT_INDEX_INCOMPATIBLE','Existing import index differs from the finite definition.')
            if not existing: conn.execute(sql)
        # These ordinary indexes require no UDF and retain exact old SQL
        # semantics for optional source-bearing evidence tables.
        for name,sql in _source_indexes(conn).items():
            old=conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone()
            if old and old[0]!=sql: raise AppError(409,'FILE_IMPORT_INDEX_INCOMPATIBLE','Existing source index differs from the finite definition.')
            if not old: conn.execute(sql)
        conn.execute("INSERT INTO app_meta VALUES('file_import_index_version',?) ON CONFLICT(key) DO NOTHING",(VERSION,))
        indexes_available(conn)


def uninstall_import_indexes(db):
    """Explicit transactional compatibility rollback; retained facts untouched."""
    with db.transaction() as conn:
        owned=CORE_INDEXES|_source_indexes(conn)
        existing=[]
        for name,sql in owned.items():
            old=conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone()
            if old and old[0]!=sql: raise AppError(409,'FILE_IMPORT_INDEX_INCOMPATIBLE','Conflicting index cannot be removed as owned migration.')
            if old: existing.append(name)
        for name in existing: conn.execute('DROP INDEX "'+name+'"')
        conn.execute("DELETE FROM app_meta WHERE key='file_import_index_version'")
