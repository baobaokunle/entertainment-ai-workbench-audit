"""Limited host-only import through the existing pending observation service.

No identity enrollment, general write activation, HTTP route, collection, model
or sync startup. Caller supplies an existing authenticated, registered workspace.
"""
import json
import re
from contextlib import contextmanager
from pathlib import Path

from .auth import AuthStore
from .common import AppError
from .backup_scratch import reject_links, require_efs
from .db import stable_key
from .dsh_app import Application
from .import_binding import ImportBindingService
from .local_snapshot_preview import opened_snapshot_preview
from .local_workspace_preparation import DIRECTORIES, local_f_path, _acl, _file, _shell_path
from .settings import read_settings


def _login_area_paths(paths, target=None, *, area='workbench-web-login'):
    """Validate only the current login directory and optional handoff file."""
    if area not in ('workbench-web-login', 'workbench-pairing'):
        raise AppError(409, 'LOCAL_NOTES_LOGIN_PATH_INVALID', '本机登录保护文件位置无效。')
    directory = paths.root / 'keys' / area
    reject_links(directory)
    if not directory.exists():
        if target is not None:
            raise AppError(409, 'LOCAL_NOTES_LOGIN_FILE_MISSING', '本机登录保护文件缺失。')
        return []
    require_efs(directory)
    items = [directory]
    if target is not None:
        if target.parent != directory:
            raise AppError(409, 'LOCAL_NOTES_LOGIN_PATH_INVALID', '本机登录保护文件位置无效。')
        _file(target)
        items.append(target)
    return items


def check_import_workspace(paths, powershell_exe, *, prepared_login=False,
                           include_login_area=False, login_file=None):
    """Check existing protected files before opening SQLite or decrypting login."""
    root = local_f_path(paths.root)
    if (root / 'sync-config.json').exists():
        raise AppError(403, 'LOCAL_IMPORT_SYNC_DISABLED_REQUIRED', '本次接入不能同时启用同步。')
    directories = [root] + [root / name for name in DIRECTORIES]
    for directory in directories:
        reject_links(directory)
        if not directory.is_dir():
            raise AppError(409, 'LOCAL_WORKSPACE_INCOMPLETE', '新工作区保护准备尚未完成。')
        require_efs(directory)
    files = [paths.settings, paths.auth, paths.db]
    if prepared_login:
        from .workbench_pairing_entry import KEY_NAME
        files.append(paths.root / 'keys' / KEY_NAME)
    files.extend(path for suffix in ('-wal', '-shm', '-journal') if (path := Path(str(paths.db) + suffix)).exists())
    for path in files:
        _file(path)
    login_paths = _login_area_paths(paths, login_file) if include_login_area else []
    # One fresh inspection of the same objects in this check phase, not a cache.
    _acl(_shell_path(powershell_exe), directories + files + login_paths, timeout_seconds=5)
    config = read_settings(paths.settings)
    if (config['source_mode'] != 'unverified' or config['device_role'] != 'manager' or
            config['ai_enabled'] or config['collection_enabled']):
        raise AppError(403, 'LOCAL_IMPORT_INACTIVE_WORKSPACE_REQUIRED', '仅允许原未激活工作区中的有限待核验导入。')
    return config


def check_import_storage(app, powershell_exe, *, prepared_login=False,
                         include_login_area=False, login_file=None):
    """Real-time checks limited to this operation's config/auth/main/WAL files."""
    if (not isinstance(app, Application) or app.read_only or app.db.read_only or app.writes_enabled or
            app.file_import is None or not isinstance(app.auth, AuthStore) or app.auth.path != app.paths.auth):
        raise AppError(403, 'LOCAL_IMPORT_INACTIVE_WORKSPACE_REQUIRED', '仅允许原未激活工作区中的有限待核验导入。')
    if check_import_workspace(app.paths, powershell_exe, prepared_login=prepared_login,
            include_login_area=include_login_area, login_file=login_file) != app.config:
        raise AppError(409, 'LOCAL_IMPORT_CONFIGURATION_CHANGED', '运行配置已变化，未继续导入。')
    with app.db.lock:
        main = [row[2] for row in app.db.conn.execute('PRAGMA database_list') if row[1] == 'main']
        if (len(main) != 1 or Path(main[0]).resolve() != app.paths.db.resolve() or
                app.db.conn.execute('PRAGMA temp_store').fetchone()[0] != 2):
            raise AppError(409, 'LOCAL_IMPORT_DATABASE_MISMATCH', '只允许原受保护业务库和内存临时存储。')


@contextmanager
def registered_import_application(data_root, operator, binding, powershell_exe):
    """Explicit host entry; consumes existing DPAPI enrollment without a prompt.

    Does not prepare/enroll accounts, emit a token, open HTTP or start workers.
    The normal Web/worker fictional-source restrictions remain untouched.
    """
    from .auth import ROLE_SCOPES
    from .db import Database
    from .paths import Paths
    from .workbench_pairing_entry import _packet, _selection
    paths = Paths.from_root(str(local_f_path(data_root)))
    ImportBindingService._binding(binding)
    config = check_import_workspace(paths, powershell_exe, prepared_login=True)
    auth = AuthStore(paths.auth, allowed_extension_ids=config['extension_ids'])
    packet = _packet(paths, auth.protector)
    if packet['account_keys'] != [binding['account_key']]:
        raise AppError(403, 'LOCAL_IMPORT_LOGIN_ACCOUNT_MISMATCH', '既有登记必须限定本次唯一账号，未打开业务库。')
    expected = _selection(paths, config, operator, packet['account_keys'], packet['extension_id'])
    if any(packet[key] != value for key, value in expected.items()):
        raise AppError(403, 'LOCAL_IMPORT_LOGIN_BINDING_CHANGED', '既有免记密码登记与工作区不一致，未打开业务库。')
    wanted = {key: expected[key] for key in ('operator_key', 'device_key', 'role', 'account_keys')}
    wanted['scopes'] = sorted(ROLE_SCOPES[expected['role']])
    session = None
    database = None
    app = None
    try:
        session = auth.login(operator, packet['login_credential'], device_key=config['device_key'], client_kind='workbench')
        principal = auth.authenticate(session['token'], scope='identities:write', account_key=binding['account_key'])
        if {key: value for key, value in principal.as_dict().items() if key != 'session_id'} != wanted:
            raise AppError(403, 'LOCAL_IMPORT_LOGIN_AUTH_CHANGED', '既有登记权限已变化，未打开业务库。')
        if check_import_workspace(paths, powershell_exe, prepared_login=True) != config:
            raise AppError(409, 'LOCAL_IMPORT_CONFIGURATION_CHANGED', '打开业务库前配置已变化。')
        database = Database(paths.db, create=False)
        app = Application(paths, auth=auth, database=database)
        if app.config != config:
            raise AppError(409, 'LOCAL_IMPORT_CONFIGURATION_CHANGED', '打开业务库时配置已变化。')
        yield app, session['token']
    finally:
        try:
            if session is not None:
                auth.logout(session['token'])
        finally:
            if app is not None:
                app.close()
            elif database is not None:
                database.close()


def import_registered_first_page(data_root, operator, binding, *, source, expected_sha256,
                                 expected_preview_sha256, node_exe, powershell_exe, limit):
    with registered_import_application(data_root,operator,binding,powershell_exe) as (app,token):
        return import_first_pending_page(app,token,binding,source=source,expected_sha256=expected_sha256,
            expected_preview_sha256=expected_preview_sha256,node_exe=node_exe,powershell_exe=powershell_exe,limit=limit)


def import_first_pending_page(app, token, binding, *, source, expected_sha256, expected_preview_sha256, node_exe,
                              powershell_exe, limit=100):
    """One first page, all unknown mappings, normal auth, transactional receipt.

    Source path/hash and binding must come from the explicitly approved local
    selection. This internal function never accepts browser-provided paths.
    It does not initialize storage or invent the missing owner platform UID.
    """
    if not isinstance(app, Application) or type(binding) is not dict:
        raise AppError(400, 'LOCAL_IMPORT_COMPONENT_INVALID', '需要原工作区和明确已有来源绑定。')
    account = binding.get('account_key')
    if type(expected_preview_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', expected_preview_sha256):
        raise AppError(400, 'LOCAL_IMPORT_PREVIEW_HASH_REQUIRED', '需要已核对有限预览的固定摘要。')
    principal = app.auth.authenticate(token, scope='identities:write', account_key=account)
    if principal.role != 'manager':
        raise AppError(403, 'LOCAL_IMPORT_MANAGER_REQUIRED', '需要原管理登记。')
    check_import_storage(app, powershell_exe)
    service = ImportBindingService(app.file_import)
    target = service.check_target(binding, principal)
    with app.db.lock:
        owner = app.db.conn.execute('SELECT platform,platform_uid FROM own_account WHERE account_key=?', (account,)).fetchone()
        if (not owner or owner['platform'] != 'douyin' or not re.fullmatch('[0-9]{1,128}', owner['platform_uid']) or
                stable_key('account', 'douyin', owner['platform_uid']) != account):
            raise AppError(409, 'LOCAL_IMPORT_OWNER_UID_REQUIRED', '本人平台UID尚未取得，不用昵称或本地占位符替代。')
    path = local_f_path(source)
    if path.is_relative_to(app.paths.root) or app.paths.root.is_relative_to(path.parent):
        raise AppError(400, 'LOCAL_IMPORT_SOURCE_SEPARATE_REQUIRED', '快照来源须与新工作区分离。')
    approved = {'format': 'dfa-approved-local-import-1', 'source_path': str(path),
                'snapshot_sha256': expected_sha256, 'preview_sha256': expected_preview_sha256,
                'account_key': account, 'account_namespace': binding['account_namespace'],
                'row_limit': limit, 'selection': 'first_page_binary_message_id'}
    if json.loads(target['source']['evidence_json']).get('local_import') != approved:
        raise AppError(409, 'LOCAL_IMPORT_SELECTION_NOT_REGISTERED', '来源登记未绑定本次明确批准的快照、页界和目标账号。')
    with opened_snapshot_preview(path, expected_sha256, binding['account_namespace'], node_exe, limit=limit) as (preview, summary, verify_source):
        if summary['preview_sha256'] != expected_preview_sha256:
            raise AppError(409, 'LOCAL_IMPORT_PREVIEW_CHANGED', '有限预览与已登记摘要不一致。')
        mapping = {**binding, 'records': {row['source_key']: {} for row in preview['records']}}
        # Normalize outside DB -> auth locks; the bounded ACL checks below
        # may still hold those locks for seconds, not just an in-memory commit.
        with app.db.lock:
            with app.auth.authorized(token, scope='identities:write', account_key=account) as current:
                if current != principal:
                    raise AppError(403, 'LOCAL_IMPORT_AUTH_CHANGED', '登记或会话已变化。')
                with app.db.transaction() as conn:
                    if service.check_target(binding, current) != target:
                        raise AppError(409, 'LOCAL_IMPORT_REGISTRY_CHANGED', '原账号或来源登记已变化。')
                    check_import_storage(app, powershell_exe)
                    before = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                              for table in ('contact', 'identity_alias', 'conversation', 'message')}
                    plan = app.file_import.plan(preview, mapping, current)
                    result = app.file_import.commit(preview, mapping, plan['plan_hash'], current)
                    if any(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] != count for table, count in before.items()):
                        raise AppError(409, 'LOCAL_IMPORT_AUTHORITY_CHANGED', '本次导入不得建立正式身份或消息。')
                    check_import_storage(app, powershell_exe)
                    verify_source(full_hash=False)
                    if app.auth.authenticate(token, scope='identities:write', account_key=account) != principal:
                        raise AppError(403, 'LOCAL_IMPORT_AUTH_CHANGED', '提交前认证已变化。')
                # Auth lock stays held through the outer transaction COMMIT.
        return {'format': 'dfa-local-pending-import-1', 'status': result['status'],
                'selected_rows': summary['selected_rows'], 'counts': dict(result['counts']),
                'snapshot_sha256': expected_sha256, 'preview_sha256': summary['preview_sha256'],
                'business_import_committed': True, 'authority': 'pending_only',
                'general_writes_enabled': False, 'identity_created': False,
                'account_ownership': 'unknown', 'private_chat_scope': 'unknown',
                'source_modified': False, 'candidate_index_rebuilt': False,
                'model_called': False, 'sync_started': False}
