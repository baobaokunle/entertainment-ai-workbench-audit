"""Prepare a NEW local EFS directory using fictional storage probes only.

This proves current-user access and same-user logical restore, not recovery after
losing Windows/EFS keys. It neither enrolls identities nor activates real writes.
"""
import base64
import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

from .backup_scratch import reject_links, require_efs
from .common import AppError
from .db import Database
from .paths import Paths

DIRECTORIES = ('runtime', 'keys', 'staging', 'backups', 'journal', 'sync',
               'sync/incoming', 'sync/outgoing', 'validation')


def local_f_path(value):
    path = Path(value)
    if (not path.is_absolute() or path.drive.upper() != 'F:' or
            any(part in ('.', '..') or ':' in part for part in path.parts[1:])):
        raise AppError(400, 'LOCAL_WORKSPACE_F_REQUIRED', '需要明确的本机F盘普通绝对路径。')
    reject_links(path)
    # Reuse the fixed-volume check; importing the launcher does not launch it.
    from tools.launch_workbench import local_path
    return local_path(str(path), '本地工作区')


def _shell_path(value):
    shell = Path(value)
    reject_links(shell)
    if (not shell.is_absolute() or shell.name.lower() != 'pwsh.exe' or
            not shell.is_file() or str(shell).startswith('\\\\')):
        raise AppError(400, 'LOCAL_WORKSPACE_SHELL_REQUIRED', '需要已有本机PowerShell7，不安装或寻找替代程序。')
    from tools.launch_workbench import local_path
    return local_path(str(shell), '本机PowerShell')


def _acl(shell, paths, *, configure_new_root=False, timeout_seconds=25):
    # Paths are literal, escaped strings, never commands. No credentials or
    # account identifiers are supplied to or returned by the helper.
    literals = ','.join("'" + str(path).replace("'", "''") + "'" for path in paths)
    script = r"""
$ErrorActionPreference='Stop'
$items=@(__PATHS__)
$current=[Security.Principal.WindowsIdentity]::GetCurrent().User
$allowed=@($current.Value,'S-1-5-18','S-1-5-32-544')
if (__CONFIGURE__) {
    $security=[Security.AccessControl.DirectorySecurity]::new()
    $security.SetOwner($current)
    $security.SetAccessRuleProtection($true,$false)
    foreach($sid in $allowed) {
        $identity=[Security.Principal.SecurityIdentifier]::new($sid)
        $rule=[Security.AccessControl.FileSystemAccessRule]::new($identity,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        $security.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $items[0] -AclObject $security
}
foreach($path in $items) {
    $info=Get-Item -LiteralPath $path -Force
    if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'LINK' }
    $security=Get-Acl -LiteralPath $path
    # Elevated Windows creates child files owned by Administrators. These
    # owners already have FullControl in the same explicit restricted DACL.
    if ($security.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowed) { throw 'OWNER' }
    $entries=@($security.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
    if ($entries.Count -eq 0 -or $entries.Count -gt 12) { throw 'ACL' }
    $own=$false
    foreach($entry in $entries) {
        if ($entry.IdentityReference.Value -notin $allowed -or
            $entry.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { throw 'ACL' }
        if ($entry.IdentityReference.Value -eq $current.Value -and
            ($entry.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq
                [Security.AccessControl.FileSystemRights]::FullControl -and
            ($entry.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -eq 0) { $own=$true }
    }
    if (!$own) { throw 'ACL' }
}
[ordered]@{restricted=$true; checked=$items.Count} | ConvertTo-Json -Compress
""".replace('__PATHS__', literals).replace('__CONFIGURE__', '$true' if configure_new_root else '$false')
    try:
        result = subprocess.run([str(shell), '-NoLogo', '-NoProfile', '-NonInteractive',
                                 '-EncodedCommand', base64.b64encode(script.encode('utf-16le')).decode('ascii')],
                                capture_output=True, timeout=timeout_seconds, creationflags=0x08000000)
        reply = json.loads(result.stdout.decode('utf-8-sig')) if result.returncode == 0 else None
        if reply != {'restricted': True, 'checked': len(paths)}:
            raise ValueError()
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        raise AppError(503, 'LOCAL_WORKSPACE_ACL_UNAVAILABLE', '新工作区权限未通过核验，保留已有结果并停止。') from None


def _file(path):
    reject_links(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise AppError(403, 'LOCAL_WORKSPACE_FILE_INVALID', '工作区文件必须独立且不可通过链接引用。')
    require_efs(path)


def inspect_prepared_directory(root, shell):
    """Only directory metadata, not proof of all future application writes."""
    root = local_f_path(root)
    paths = [root] + [root / name for name in DIRECTORIES]
    for path in paths:
        reject_links(path)
        if not path.is_dir():
            raise AppError(409, 'LOCAL_WORKSPACE_INCOMPLETE', '新工作区保护准备尚未完成。')
        require_efs(path)
    _acl(_shell_path(shell), paths)
    return paths


def prepare_new_directory(data_root, powershell_exe):
    """Exclusive creation; failures retain the owned partial directory for review.

    All database content here is fixed fictional probe data. The business DB,
    enrollment, model, source snapshot and history index are never opened.
    """
    if os.name != 'nt':
        raise AppError(503, 'LOCAL_WORKSPACE_WINDOWS_REQUIRED', '此本地保护准备需要Windows。')
    root = local_f_path(data_root)
    Paths.from_root(str(root))  # Existing rule: outside the program directory.
    shell = _shell_path(powershell_exe)
    if root.exists() or not root.parent.is_dir():
        raise AppError(409, 'LOCAL_WORKSPACE_NEW_REQUIRED', '只准备全新目录；已有目录不覆盖、不修复。')
    try:
        root.mkdir(exist_ok=False)
    except FileExistsError:
        raise AppError(409, 'LOCAL_WORKSPACE_NEW_REQUIRED', '目标目录已出现，未修改。') from None
    _acl(shell, [root], configure_new_root=True)
    require_efs(root, encrypt=True)
    for name in DIRECTORIES:
        path = root / name
        path.mkdir(exist_ok=False)
        require_efs(path)
    directories = inspect_prepared_directory(root, shell)
    probe = root / 'validation/storage-probe.sqlite3'
    with probe.open('xb'):
        pass
    _file(probe)
    database = Database(probe, create=True)
    recovered = root / 'validation/storage-restored.sqlite3'
    try:
        with database.transaction() as connection:
            connection.execute('CREATE TABLE local_storage_probe(value TEXT NOT NULL)')
            connection.execute('INSERT INTO local_storage_probe VALUES (?)', ('fictional-storage-only',))
        active = [probe, Path(str(probe) + '-wal'), Path(str(probe) + '-shm')]
        for path in active:
            _file(path)
        _acl(shell, directories + active)
        if database.conn.execute('PRAGMA temp_store').fetchone()[0] != 2:
            raise AppError(503, 'DATABASE_TEMP_PROTECTION_REQUIRED', '临时存储未保持内存模式。')
        with recovered.open('xb'):
            pass
        _file(recovered)
        destination = sqlite3.connect(recovered.as_uri() + '?mode=rw', uri=True)
        try:
            destination.execute('PRAGMA temp_store=MEMORY')
            database.conn.backup(destination)
        finally:
            destination.close()
        _file(recovered)
    finally:
        database.close()
    restored = Database(recovered, read_only=True)
    try:
        if (restored.conn.execute('SELECT value FROM local_storage_probe').fetchone()[0] != 'fictional-storage-only' or
                restored.conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'):
            raise AppError(503, 'LOCAL_WORKSPACE_RESTORE_FAILED', '虚构存储恢复检查未通过。')
    finally:
        restored.close()
    files = [path for path in (probe, recovered, Path(str(recovered) + '-wal'), Path(str(recovered) + '-shm')) if path.exists()]
    for path in files:
        _file(path)
    _acl(shell, directories + files)
    return {'format': 'dfa-local-workspace-preparation-1', 'prepared': True,
            'directory_count': len(directories), 'efs_main_wal_shm_verified': True,
            'restricted_acl_verified': True, 'sqlite_temporary_storage': 'memory',
            'fictional_storage_readback': True, 'same_user_logical_restore': True,
            'cross_machine_or_lost_key_recovery': 'unverified',
            'business_database_created': False, 'enrollment_created': False,
            'real_source_read': False, 'real_import_committed': False,
            'runtime_write_guard_installed': False, 'activation_allowed': False,
            'model_started': False, 'partial_directories_auto_deleted': False}
