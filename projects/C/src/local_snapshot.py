"""C-only local snapshot helpers. No application, browser, or network imports."""
from contextlib import contextmanager
from pathlib import Path
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import sqlite3
import stat


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def plain_path(path):
    path = Path(path).absolute()
    for node in (path, *path.parents):
        if node.exists():
            info = node.lstat()
            if node.is_symlink() or getattr(info, 'st_file_attributes', 0) & 1024:
                raise ValueError('linked_path')
            if node.is_dir() and (node / '.git').exists():
                raise ValueError('git_covered_path')
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError('multiple_file_links')
    return path


@contextmanager
def fixed_snapshot(path, expected):
    """Deny concurrent writes/deletion, then verify the known standalone snapshot."""
    path = plain_path(path)
    if os.name != 'nt':
        raise RuntimeError('windows_lock_required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = []
    watched = []
    try:
        for suffix in ('', '-wal', '-shm', '-journal'):
            candidate = plain_path(str(path) + suffix)
            if not candidate.exists():
                if not suffix:
                    raise FileNotFoundError('snapshot_missing')
                watched.append((candidate, None))
                continue
            handle = kernel.CreateFileW(str(candidate), 0x80000000, 1, None, 3, 0x80, None)
            if handle == wintypes.HANDLE(-1).value:
                raise OSError('read_lock_failed')
            handles.append(handle)
            if suffix and candidate.stat().st_size:
                raise ValueError('nonempty_sidecar')
            watched.append((candidate, (candidate.stat().st_size, candidate.stat().st_mtime_ns,
                                        digest(candidate))))
        if digest(path) != expected:
            raise ValueError('snapshot_digest_mismatch')
        yield path
        for candidate, previous in watched:
            now = None if not candidate.exists() else (candidate.stat().st_size,
                    candidate.stat().st_mtime_ns, digest(candidate))
            if now != previous:
                raise RuntimeError('snapshot_changed')
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)


def readonly_connection(path):
    connection = sqlite3.connect(Path(path).as_uri() + '?mode=ro&immutable=1',
                                 uri=True, timeout=1)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA temp_store=MEMORY')
        connection.execute('PRAGMA trusted_schema=OFF')
    except BaseException:
        connection.close()
        raise
    return connection


def copy_exclusive(source, target, expected):
    source, target = Path(source), plain_path(target)
    partial = plain_path(str(target) + '.partial')
    if target.exists() or partial.exists():
        raise FileExistsError('no_overwrite')
    with fixed_snapshot(source, expected):
        with source.open('rb') as src, partial.open('xb') as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if digest(partial) != expected:
            raise ValueError('copy_digest_mismatch')
        conn = readonly_connection(partial)
        try:
            if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('copy_integrity_failed')
        finally:
            conn.close()
        # Windows rename refuses an existing destination; never replace.
        partial.rename(target)
        target.chmod(stat.S_IREAD)
    return {'sha256': digest(target), 'bytes': target.stat().st_size,
            'readonly_attribute': bool(target.stat().st_file_attributes & 1)}


def write_json_new(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
