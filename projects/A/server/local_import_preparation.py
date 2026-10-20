"""First-only host preparation for the explicitly selected pending import.

The protected request supplies the UID; this module cannot discover or verify
it. No snapshot contents, browser, HTTP service or model is opened here.
"""
import json
import re
from pathlib import Path

from .auth import AuthStore, EXTENSION_RE
from .common import AppError, canonical_json
from .db import Database, stable_key
from .identity import IdentityService, require_text
from .import_binding import ImportBindingService
from .local_device_pairing import _file_stat, _read_protected, _write_new
from .local_workspace_preparation import (
    _acl, _file, _shell_path, inspect_prepared_directory, local_f_path,
)
from .paths import Paths
from .settings import DEFAULTS, validate_settings

FIELDS = {'format', 'device_key', 'operator_key', 'extension_id', 'owner_platform_uid',
          'owner_source_id', 'owner_source_version', 'source_id', 'source_version',
          'account_namespace', 'snapshot_file', 'snapshot_sha256', 'preview_sha256', 'row_limit'}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _request(raw, paths):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    except (UnicodeError, ValueError):
        raise AppError(400, 'LOCAL_PREPARATION_REQUEST_INVALID', '本机准备请求格式无效。') from None
    if (type(value) is not dict or not FIELDS <= set(value) or set(value) - FIELDS - {'local_access'} or
            value['format'] != 'dfa-local-import-preparation-1' or
            type(value['owner_platform_uid']) is not str or
            not re.fullmatch('[0-9]{1,128}', value['owner_platform_uid']) or
            type(value['row_limit']) is not int or not 1 <= value['row_limit'] <= 100 or
            any(type(value[k]) is not str or not re.fullmatch('[0-9a-f]{64}', value[k])
                for k in ('snapshot_sha256', 'preview_sha256'))):
        raise AppError(400, 'LOCAL_PREPARATION_REQUEST_INVALID', '需要明确本人平台UID、来源和有限页选择；不推断身份。')
    access = value.get('local_access')
    if 'local_access' in value and (type(access) is not dict or
            set(access) not in ({'note'}, {'note', 'reply_on_demand'}) or access.get('note') is not True or
            ('reply_on_demand' in access and access['reply_on_demand'] is not True)):
        raise AppError(400, 'LOCAL_PREPARATION_ACCESS_INVALID', '有限选择仅允许明确备注及可选按需本机回复；不接收另一个账号或来源。')
    for field, limit in (('owner_source_id', 200), ('owner_source_version', 80), ('snapshot_file', 1024)):
        require_text(value[field], field, limit)
    if (value['operator_key'] == 'identity-pilot' or value['device_key'] == 'F-identity-pilot' or
            type(value['extension_id']) is not str or not EXTENSION_RE.fullmatch(value['extension_id']) or
            value['owner_source_id'] == value['source_id']):
        raise AppError(400, 'LOCAL_PREPARATION_SELECTION_INVALID', '需要正常工作台及分开的本人声明和导入来源。')
    account = stable_key('account', 'douyin', value['owner_platform_uid'])
    AuthStore._validate_identity(value['operator_key'], 'manager', value['device_key'], [account])
    binding = {'account_key': account, **{k: value[k] for k in ('source_id', 'source_version', 'account_namespace')}}
    ImportBindingService._binding(binding)
    config = validate_settings(DEFAULTS | {'device_key': value['device_key'], 'device_role': 'manager',
                                          'extension_ids': [value['extension_id']]})
    source = local_f_path(value['snapshot_file'])
    if source.is_relative_to(paths.root) or paths.root.is_relative_to(source.parent):
        raise AppError(400, 'LOCAL_IMPORT_SOURCE_SEPARATE_REQUIRED', '快照来源须与新工作区分离。')
    _file_stat(source)  # Metadata only; bytes/hash are checked by the later importer.
    selection = {'format': 'dfa-approved-local-import-1', 'source_path': str(source),
                 'snapshot_sha256': value['snapshot_sha256'], 'preview_sha256': value['preview_sha256'],
                 'account_key': account, 'account_namespace': value['account_namespace'],
                 'row_limit': value['row_limit'], 'selection': 'first_page_binary_message_id'}
    if access is not None:
        config = validate_settings(config | {'local_access': {
            'format': 'dfa-local-access-1' if access.get('reply_on_demand') else 'dfa-local-notes-1',
            'workspace_root': str(paths.root), 'binding': binding, 'selection': selection, **access}})
    return value, config, binding, selection


def prepare_local_import(data_root, request_file, powershell_exe):
    """An already protected NEW directory only; interrupted files are retained."""
    root = local_f_path(data_root)
    paths = Paths.from_root(str(root))
    shell = _shell_path(powershell_exe)
    directories = inspect_prepared_directory(root, shell)
    request = local_f_path(request_file)
    if request.parent != root / 'validation':
        raise AppError(400, 'LOCAL_PREPARATION_REQUEST_LOCATION', '准备请求必须位于本工作区受保护的validation目录。')
    _file(request)
    _acl(shell, directories + [request])
    raw = _read_protected(request, 16384)
    value, config, binding, selection = _request(raw, paths)
    # No existing account can be silently reused or downgraded: register_account
    # keeps a prior verified=True, and pairing otherwise accepts an existing pair.
    for directory in ('runtime', 'keys', 'staging', 'backups', 'journal', 'sync/incoming', 'sync/outgoing'):
        if any((root / directory).iterdir()):
            raise AppError(409, 'LOCAL_PREPARATION_NEW_REQUIRED', '只准备新的空工作区；已有或半成品资料保留。')
    for path in (paths.settings, root / 'config.tmp', root / 'sync-config.json'):
        if path.exists():
            raise AppError(409, 'LOCAL_PREPARATION_NEW_REQUIRED', '已有配置或半成品保留，不覆盖。')
    _file(request)
    _acl(shell, directories + [request])
    if _read_protected(request, 16384) != raw:
        raise AppError(409, 'LOCAL_PREPARATION_REQUEST_CHANGED', '准备请求已变化，未创建业务资料。')
    # Exclusive config creation is also the first-preparer reservation. Avoid
    # write_settings (replace) and Database(create=True) on an unreserved path.
    _write_new(paths.settings, canonical_json(config))
    _file(paths.settings)
    _write_new(paths.db, b'')
    _file(paths.db)
    database = None
    try:
        database = Database(paths.db, create=True)
        database.set_runtime_identity(value['device_key'])
        from .migrations import install_candidate_features
        from .file_import import initialize_import_schema
        install_candidate_features(database)
        initialize_import_schema(database)
        active = [paths.settings, paths.db] + [p for suffix in ('-wal', '-shm', '-journal')
                                              if (p := Path(str(paths.db) + suffix)).exists()]
        for path in active:
            _file(path)
        _acl(shell, directories + active + [request])
        if _read_protected(request, 16384) != raw:
            raise AppError(409, 'LOCAL_PREPARATION_REQUEST_CHANGED', '准备请求已变化；保留未完成工作区。')
        identities = IdentityService(database)
        with database.transaction():
            identities.register_source(value['owner_source_id'], value['owner_source_version'],
                                       source_kind='unverified_export', capabilities={}, verified=False)
            owner = identities.register_account(value['owner_platform_uid'], source_id=value['owner_source_id'],
                                                source_version=value['owner_source_version'], verified=False)
            source = identities.register_source(value['source_id'], value['source_version'],
                source_kind='unverified_export', capabilities={'account_keys': [binding['account_key']]},
                verified=False, evidence={'local_import': selection})
            if owner['verified'] or source['verified'] or owner['account_key'] != binding['account_key']:
                raise AppError(409, 'LOCAL_PREPARATION_AUTHORITY_CHANGED', '未验证登记状态不符，未完成准备。')
    finally:
        if database is not None:
            database.close()
    from .workbench_pairing_entry import prepare_workbench_pairing
    from .local_pending_import import check_import_workspace
    pairing = prepare_workbench_pairing(paths, config, value['operator_key'],
                                       [binding['account_key']], value['extension_id'])
    if pairing['existing_registration']:
        raise AppError(409, 'LOCAL_PREPARATION_NEW_REQUIRED', '出现已有认证登记，保留资料，不报告准备成功。')
    if check_import_workspace(paths, shell, prepared_login=True) != config:
        raise AppError(409, 'LOCAL_PREPARATION_CONFIGURATION_CHANGED', '准备配置发生变化，保留资料。')
    _file(request)
    _acl(shell, [request])
    if _read_protected(request, 16384) != raw:
        raise AppError(409, 'LOCAL_PREPARATION_REQUEST_CHANGED', '准备请求已变化，保留资料。')
    return {'format': 'dfa-local-import-prepared-1', 'prepared': True,
            'business_database_created': True, 'enrollment_created': True,
            'owner_verified': False, 'source_verified': False, 'snapshot_contents_read': False,
            'snapshot_selection_verified': False, 'import_committed': False,
            'general_writes_enabled': False, 'local_access_enabled': False,
            'local_notes_selected': config['local_access'] is not None,
            'local_reply_selected': bool(config['local_access'] and config['local_access'].get('reply_on_demand')),
            'service_started': False, 'model_started': False}
