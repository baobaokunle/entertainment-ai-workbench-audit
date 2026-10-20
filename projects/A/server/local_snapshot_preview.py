"""One bounded page from an already approved, clean, pinned local snapshot.

No snapshot copy, candidate store, identity registration or business writes.
Private rows stay in local memory; the public summary contains only counts.
"""
import hashlib
from contextlib import ExitStack, contextmanager
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from .backup_scratch import reject_links
from .common import AppError, canonical_json
from .local_workspace_preparation import local_f_path
from tools.export_file_reader import COLUMNS
from tools.large_export_reader import LargeExportReader, SELECTED, SQLITE_LENGTH_BYTES, identity
from tools.large_export_snapshot import LockedRead

ROOT = Path(__file__).resolve().parents[1]
MAX_PAGE_BYTES = 8 * 1024 * 1024


def _sidecars(path):
    result = {}
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        reject_links(sidecar)
        if sidecar.exists():
            stamp = identity(sidecar)
            if stamp[2] != 0:
                raise AppError(409, 'LOCAL_SNAPSHOT_SIDECAR_PRESENT', '指定快照有非空辅助文件，未忽略它们或修改原件。')
            result[sidecar] = stamp
    return result


def _read_rows(path, limit):
    conn = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        conn.enable_load_extension(False)
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.execute('PRAGMA temp_store=MEMORY')
        if conn.execute('PRAGMA temp_store').fetchone()[0] != 2:
            raise AppError(503, 'DATABASE_TEMP_PROTECTION_REQUIRED', '临时存储未保持内存模式。')
        deadline = time.monotonic() + 2
        steps = 0
        def progress():
            nonlocal steps
            steps += 100
            return int(steps > 1000000 or time.monotonic() > deadline)
        conn.set_progress_handler(progress, 100)
        table = conn.execute("SELECT type,sql FROM sqlite_schema WHERE name='messages'").fetchall()
        columns = conn.execute("PRAGMA table_xinfo('messages')").fetchall()
        if (len(table) != 1 or table[0][0] != 'table' or 'VIRTUAL' in (table[0][1] or '').upper() or
                {r[1]: r[2].upper() for r in columns} != COLUMNS or any(r[6] for r in columns) or
                next(r for r in columns if r[1] == 'msg_id')[5] != 1 or
                not next(r for r in columns if r[1] == 'conv_id')[3]):
            raise AppError(409, 'LOCAL_SNAPSHOT_SCHEMA_INVALID', '快照不符合已有导出合同。')
        def authorize(action, table, column, database, origin):
            if origin is not None:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and table == 'messages' and column in SELECTED:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        conn.set_authorizer(authorize)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQLITE_LENGTH_BYTES)
        rows, used, last = [], 0, None
        for values in conn.execute('SELECT ' + ','.join(SELECTED) + ' FROM messages ORDER BY msg_id COLLATE BINARY LIMIT ?', (limit,)):
            row = LargeExportReader._row(values)
            key = row['msg_id'].encode('utf-8')
            if last is not None and key <= last:
                raise ValueError('order')
            last = key
            used += len(canonical_json(row))
            if used > MAX_PAGE_BYTES or time.monotonic() > deadline:
                raise ValueError('budget')
            rows.append(row)
        if not rows:
            raise AppError(409, 'LOCAL_SNAPSHOT_NO_ROWS', '指定快照没有可预览记录。')
        return rows
    finally:
        conn.close()


@contextmanager
def opened_snapshot_preview(source, expected_sha256, namespace, node_exe, *, limit=100):
    """Returns private preview in memory and a separately safe public summary.

    The same existing Windows locked-read lease covers hashing, SQLite reading
    and normalization. It denies writers and replacement; no live source/WAL is
    recovered. Callers must not print the preview or use it as account evidence.
    """
    if (type(expected_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', expected_sha256) or
            type(namespace) is not str or not re.fullmatch('[A-Za-z0-9_-]{8,128}', namespace) or
            type(limit) is not int or not 1 <= limit <= 100):
        raise AppError(400, 'LOCAL_SNAPSHOT_SELECTION_INVALID', '需要明确快照摘要、命名空间及1至100条范围。')
    path = local_f_path(source)
    executable = Path(node_exe)
    reject_links(executable)
    if not executable.is_absolute() or not executable.is_file() or executable.name.lower() != 'node.exe':
        raise AppError(400, 'LOCAL_SNAPSHOT_NODE_REQUIRED', '需要明确已有本机Node程序。')
    from tools.launch_workbench import local_path
    local_path(str(executable), '本地归一化程序')
    deadline = time.monotonic() + 60
    def within_budget():
        if time.monotonic() > deadline:
            raise AppError(409, 'LOCAL_SNAPSHOT_TIME_LIMIT', '本次有限读取超时，未继续处理。')
    try:
        with ExitStack() as stack:
            lease = stack.enter_context(LockedRead(path))
            sidecars = _sidecars(path)
            for sidecar in sidecars:
                companion = stack.enter_context(LockedRead(sidecar))
                if companion.initial['bytes'] != 0:
                    raise AppError(409, 'LOCAL_SNAPSHOT_CHANGED', '辅助文件已变化，未读取消息。')
            source_identity = identity(path)
            if lease.hash_again(check=within_budget)['sha256'] != expected_sha256 or not lease.read_prefix(16).startswith(b'SQLite format 3\x00'):
                raise AppError(409, 'LOCAL_SNAPSHOT_HASH_MISMATCH', '快照与已批准摘要不符；未读取消息。')
            if _sidecars(path) != sidecars:
                raise AppError(409, 'LOCAL_SNAPSHOT_CHANGED', '辅助文件集合已变化，未读取消息。')
            rows = _read_rows(path, limit)
            request = canonical_json({'rows': rows, 'namespace': namespace})
            if len(request) > MAX_PAGE_BYTES:
                raise ValueError('input budget')
            # No inherited NODE_OPTIONS, proxy, credentials, or caller code.
            env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'SYSTEMROOT') if key in os.environ}
            response = subprocess.run([str(executable), str(ROOT / 'tools/local_snapshot_normalize.mjs')],
                input=request, capture_output=True, timeout=20, cwd=ROOT, env=env, creationflags=0x08000000)
            if response.returncode != 0 or response.stderr or len(response.stdout) > 32 * 1024 * 1024:
                raise ValueError('normalization refused')
            preview = json.loads(response.stdout)
            if (preview.get('source_kind') != 'unverified_export' or preview.get('account_namespace') != namespace or
                    preview.get('persisted') is not False or len(preview.get('records', [])) != len(rows)):
                raise ValueError('contract')
            def verify_source(*, full_hash=True):
                within_budget()
                if (_sidecars(path) != sidecars or identity(path) != source_identity or
                        lease.info() != lease.initial or
                        (full_hash and lease.hash_again(check=within_budget)['sha256'] != expected_sha256)):
                    raise AppError(409, 'LOCAL_SNAPSHOT_CHANGED', '快照在本次检查中变化，未保存资料。')
            verify_source()
            summary = {'format': 'dfa-local-snapshot-preview-1', 'snapshot_sha256': expected_sha256,
                'preview_sha256': hashlib.sha256(canonical_json(preview)).hexdigest(),
                'selected_rows': len(rows), 'row_limit': limit, 'selection': 'first_page_binary_message_id',
                'source_hash_verified_before_and_after': True, 'source_read_only': True,
                'empty_auxiliary_files_locked': len(sidecars), 'auxiliary_files_modified': False,
                'source_copied': False, 'candidate_index_rebuilt': False,
                'coverage': 'unknown', 'account_ownership': 'unknown', 'private_chat_scope': 'unknown',
                'identity_mapping': 'unknown', 'business_import_committed': False,
                'private_preview_persisted': False, 'model_called': False}
            # Keep the native source locks across a caller's bounded commit.
            # After full verification outside auth locks, the caller verifies
            # still-held leases and object metadata inside its transaction.
            yield preview, summary, verify_source
    except AppError:
        raise
    except Exception:
        raise AppError(409, 'LOCAL_SNAPSHOT_PREVIEW_REFUSED', '有限快照读取或归一化未通过，未保存资料。') from None


def preview_snapshot(source, expected_sha256, namespace, node_exe, *, limit=100):
    with opened_snapshot_preview(source, expected_sha256, namespace, node_exe, limit=limit) as (preview, summary, _):
        return preview, summary
