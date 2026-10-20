"""Windows EFS scratch, verified before any plaintext database is written.

Deletion is cleanup, not a secure-erasure claim. A crash can leave an encrypted
orphan; plaintext pages remain protected by the OS EFS user key, not a config flag.
"""
import ctypes
import os
from pathlib import Path
import secrets
from .common import AppError

def reject_links(path):
    path=Path(path).absolute()
    for part in (path,*path.parents):
        if part.is_symlink() or getattr(part,'is_junction',lambda:False)():
            raise AppError(403,'backup_path_forbidden','Backup and restore paths must not traverse links or junctions.')
    return path

def require_efs(path,*,encrypt=False):
    if os.name!='nt':raise AppError(503,'backup_protected_scratch_unavailable','Verified Windows EFS scratch is required.')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetFileAttributesW.argtypes=[ctypes.c_wchar_p];kernel.GetFileAttributesW.restype=ctypes.c_uint32
    if encrypt:
        advapi=ctypes.WinDLL('advapi32',use_last_error=True)
        advapi.EncryptFileW.argtypes=[ctypes.c_wchar_p];advapi.EncryptFileW.restype=ctypes.c_int
        if not advapi.EncryptFileW(str(path)):raise AppError(503,'backup_protected_scratch_unavailable','Windows EFS protection could not be established.')
    attributes=kernel.GetFileAttributesW(str(path))
    if attributes==0xffffffff or not attributes&0x4000 or attributes&0x400:
        raise AppError(503,'backup_protected_scratch_unavailable','Scratch encryption could not be verified.')

class ProtectedScratch:
    def __init__(self,parent):self.parent=reject_links(parent);self.root=None;self.database=None
    def __enter__(self):
        self.root=self.parent/('.protected-backup-'+secrets.token_hex(16))
        self.root.mkdir(mode=0o700)
        try:
            require_efs(self.root,encrypt=True)
            self.database=self.root/'snapshot.sqlite3'
            with self.database.open('xb'):pass
            require_efs(self.database,encrypt=True)
            # Directory attributes alone are insufficient under restricted user
            # tokens. Prove this process can use the encrypted file, with only a
            # random synthetic marker, before handing it any database plaintext.
            marker=secrets.token_bytes(32)
            with self.database.open('r+b') as probe:
                probe.write(marker);probe.flush();os.fsync(probe.fileno())
                probe.seek(0)
                if probe.read()!=marker:raise AppError(503,'backup_protected_scratch_unavailable','Encrypted scratch read/write verification failed.')
                probe.seek(0);probe.truncate();probe.flush();os.fsync(probe.fileno())
            require_efs(self.database)
            return self
        except OSError as exc:
            self.__exit__(None,None,None)
            raise AppError(503,'backup_protected_scratch_unavailable','The current Windows user cannot use verified EFS scratch.') from exc
        except BaseException:
            self.__exit__(None,None,None);raise
    def __exit__(self,*args):
        if self.root is not None:
            # Fixed, freshly owned scratch only. Never recurse through a link.
            for name in ('snapshot.sqlite3','snapshot.sqlite3-wal','snapshot.sqlite3-shm','snapshot.sqlite3-journal'):
                (self.root/name).unlink(missing_ok=True)
            self.root.rmdir()
