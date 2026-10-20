"""Fixed, local-only ciphertext subsets. No capture, import or plaintext export API."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import zipfile

from .contract import decode, encode
from .security import CollectorError, check_path

FORMAT = 'b-fixed-handoff/1'
INDEX_BUDGET = 3500 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def current():
    return datetime.now(timezone.utc)


def check_expiry(value):
    t = datetime.fromisoformat(value)
    if t.utcoffset() is None or t <= current():
        raise CollectorError('HANDOFF_EXPIRED')


def positive(o):
    return o['domain'] != 'capability' and bool(o['fields']) and not any(
        x in o['issues'] for x in ('EMPTY_RESPONSE_NOT_COMPLETE', 'NO_ALLOWLISTED_FIELDS'))


def raw(field):
    return field.get('value') if isinstance(field, dict) and field.get('lossless') is True else None


def typed_key(field):
    # Equality of typed scalar values is independent of JSON member order.
    # This is ONLY for joins, never substitute it for a saved message_key.
    return encode([field['type'],field['value'],field['lossless']]) if raw(field) is not None else None


def direction(o):
    sender = o['fields'].get('sender', o['fields'].get('senderUserId'))
    owner = o['context']['owner']
    if not isinstance(raw(sender), str) or not isinstance(raw(owner), str) or sender.get('type') != 'string' or owner.get('type') != 'string':
        return 'unknown'
    return 'outgoing_owner' if sender == owner else 'incoming_other'


@contextmanager
def readonly(base, relative, protector):
    path = check_path(Path(base) / relative, Path(base))
    protector.verify_acl(path.parent, protected=True)
    for suffix in ('', '-journal', '-wal', '-shm'):
        f = check_path(str(path) + suffix, Path(base))
        if f.exists():
            protector.verify_acl(f)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute("SELECT 1 FROM runs WHERE state='running'").fetchone():
            raise CollectorError('HANDOFF_WAIT_FOR_CHECKPOINT')
        yield db
    finally:
        db.close()


def check_origins(base, origins, protector, bindings=()):
    for origin in origins:
        with readonly(base, origin['database'], protector) as db:
            row = db.execute('SELECT state,cipher,count FROM runs WHERE id=?', (origin['run_id'],)).fetchone()
            if not row or row[0] != origin['state'] or row[2] != origin['count'] or sha(row[1]) != origin['meta_sha256']:
                raise CollectorError('HANDOFF_SOURCE_CHANGED_OR_REMOVED')
            meta = decode(protector.unprotect(row[1]))
            check_expiry(meta['expires_at'])
            queries = {
                'observations': ('SELECT cipher FROM observations WHERE run_id=? AND id=?', ('run_id','id')),
                'gap_operations': ('SELECT cipher FROM gap_operations WHERE run_id=? AND id=?', ('run_id','id')),
                'fresh_operations': ('SELECT cipher FROM fresh_operations WHERE run_id=? AND ordinal=?', ('run_id','ordinal')),
                'chat_media': ('SELECT cipher FROM chat_media WHERE run_id=? AND id=?', ('run_id','id')),
                'chat_media_chunks': ('SELECT cipher FROM chat_media_chunks WHERE asset_id=? AND sequence=?', ('asset_id','sequence')),
                'chat_today_targets': ('SELECT cipher FROM chat_today_targets WHERE run_id=? AND id=? AND state=?', ('run_id','id','state')),
                'chat_today_pages': ('SELECT cipher FROM chat_today_pages WHERE run_id=? AND id=? AND target_id=? AND ordinal=? AND state=?', ('run_id','id','target_id','ordinal','state')),
                'chat_today_page_blocks': ('SELECT b.cipher FROM chat_today_page_blocks b JOIN chat_today_pages p ON p.id=b.page_id WHERE p.run_id=? AND b.page_id=? AND b.position=?', ('run_id','page_id','position')),
                'chat_today_materialized': ('SELECT cipher FROM chat_today_materialized WHERE run_id=? AND page_id=?', ('run_id','page_id')),
                'chat_today_media_receipts': ('SELECT cipher FROM chat_today_media_receipts WHERE run_id=? AND id=? AND page_id=? AND position=? AND state=? AND global_stop=?', ('run_id','id','page_id','position','state','global_stop')),
                'chat_today_starts': ('SELECT cipher FROM chat_today_starts WHERE child_id=? AND approval_id=? AND parent_id=?', ('run_id','approval_id','parent_id')),
            }
            for binding in bindings:
                if binding['database'] != origin['database'] or binding['run_id'] != origin['run_id']: continue
                if binding['table'] not in queries: raise CollectorError('HANDOFF_BINDING_TABLE')
                sql, keys = queries[binding['table']]
                value = db.execute(sql, tuple(binding[k] for k in keys)).fetchone()
                if not value or sha(value[0]) != binding['sha256']:
                    raise CollectorError('HANDOFF_SOURCE_ROW_CHANGED_OR_REMOVED')


def verify_rank_group(rows, expected_count):
    """One original projected response, not a synthesized daily leaderboard."""
    if len(rows) != expected_count or not rows: raise CollectorError('HANDOFF_RANK_BATCH')
    first = rows[0]
    for number, o in enumerate(rows):
        if (o['domain'] != 'rank' or not positive(o) or o['observed_at'] != first['observed_at']
            or o['context'] != first['context'] or o['coverage'] != first['coverage']
            or o['coverage']['received'] != expected_count
            or o['source']['record_ref'] != f'/projection/rows/{number}'
            or {k:v for k,v in o['source'].items() if k!='record_ref'} != {k:v for k,v in first['source'].items() if k!='record_ref'}):
            raise CollectorError('HANDOFF_RANK_BATCH')
    return True


def preflight_bundle(index, members):
    if len(encode(index)) > INDEX_BUDGET:
        raise CollectorError('HANDOFF_INDEX_SIZE')
    if len(members) > 20000 or sum(map(len, members.values())) > 256 * 1024 * 1024:
        raise CollectorError('HANDOFF_SIZE')
    if any(len(v) > 4 * 1024 * 1024 for v in members.values()):
        raise CollectorError('HANDOFF_MEMBER_SIZE')


def write_bundle(base, folder, name, index, members, protector):
    """Caller supplies reviewed selection; all original source values stay in DPAPI."""
    if index.get('format') != FORMAT or len(members) > 20000:
        raise CollectorError('HANDOFF_CONTRACT')
    preflight_bundle(index, members)
    check_expiry(index['expires_at'])
    check_origins(base, index['origins'], protector, index.get('source_bindings', ()))
    folder = check_path(folder, base)
    protector.verify_acl(folder, protected=True)
    path = check_path(folder / (name + '.bhand.zip'), base)
    if path.exists():
        raise CollectorError('HANDOFF_ALREADY_EXISTS')
    if any(not k.endswith('.dpapi') or '..' in k or k.startswith('/') or '\\' in k for k in members):
        raise CollectorError('HANDOFF_MEMBER_PATH')
    if any(not isinstance(v, bytes) or not v.startswith(b'BLC1\0') for v in members.values()):
        raise CollectorError('HANDOFF_CIPHER_REQUIRED')
    if sum(map(len, members.values())) > 256 * 1024 * 1024:
        raise CollectorError('HANDOFF_SIZE')
    # Encryption has a 4 MiB per-object ceiling; never fall back to plaintext.
    members = dict(members)
    members['selection.dpapi'] = protector.protect(encode(index))
    manifest = {'format': FORMAT, 'protection': 'CurrentUser_DPAPI_BLC1_null_entropy',
                'expires_at': index['expires_at'], 'category': index['category'],
                'source_complete': False, 'formal_A_acceptance': False,
                'files': {k: {'sha256': sha(v), 'bytes': len(v)} for k, v in members.items()}}
    # Exclusive creation, no overwrite. An incomplete archive is never advertised.
    with path.open('xb') as handle:
        with zipfile.ZipFile(handle, 'w', compression=zipfile.ZIP_STORED) as z:
            for key, value in members.items():
                z.writestr(key, value)
            z.writestr('manifest.json', encode(manifest))
    protector.verify_acl(path)
    check_origins(base, index['origins'], protector, index.get('source_bindings', ()))
    digest = sha(path.read_bytes())
    return {'path': str(path), 'sha256': digest, 'bytes': path.stat().st_size,
            'format': FORMAT, 'expires_at': index['expires_at']}


def _read_bundle_payload(base, path, expected_sha256, protector):
    """Validate one hashed byte stream; caller MUST verify all source bindings."""
    path = check_path(path, base)
    protector.verify_acl(path.parent, protected=True)
    protector.verify_acl(path)
    if path.stat().st_size > 270 * 1024 * 1024:
        raise CollectorError('HANDOFF_DIGEST')
    data = path.read_bytes()
    if len(data) > 270 * 1024 * 1024 or sha(data) != expected_sha256:
        raise CollectorError('HANDOFF_DIGEST')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if len(names) != len(set(names)) or len(names) > 20002:
            raise CollectorError('HANDOFF_MEMBER_SET')
        if any(x.file_size > 4 * 1024 * 1024 or x.compress_type != zipfile.ZIP_STORED for x in z.infolist()):
            raise CollectorError('HANDOFF_MEMBER_SIZE')
        manifest = decode(z.read('manifest.json'))
        if manifest['format'] != FORMAT or set(names) != set(manifest['files']) | {'manifest.json'}:
            raise CollectorError('HANDOFF_CONTRACT')
        check_expiry(manifest['expires_at'])
        members = {}
        for name, expected in manifest['files'].items():
            value = z.read(name)
            if len(value) != expected['bytes'] or sha(value) != expected['sha256'] or not value.startswith(b'BLC1\0'):
                raise CollectorError('HANDOFF_MEMBER_DIGEST')
            members[name] = value
    index = decode(protector.unprotect(members.pop('selection.dpapi')))
    if index['format'] != FORMAT or index['expires_at'] != manifest['expires_at'] or index['category'] != manifest['category']:
        raise CollectorError('HANDOFF_CONTRACT')
    check_expiry(index['expires_at'])
    return index, members


def read_bundle(base, path, expected_sha256, protector):
    """Return selection and ciphertext only after the original global source gate.

    This is not A authorization. Callers independently enforce registration,
    intended use and revocation. Removal/change and expiry fail on every read.
    """
    index, members = _read_bundle_payload(base, path, expected_sha256, protector)
    check_origins(base, index['origins'], protector, index.get('source_bindings', ()))
    check_expiry(index['expires_at'])
    return index, members
