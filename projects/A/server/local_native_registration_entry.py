"""Explicit protected host submission, reviewed setup or runtime revocation.

The caller supplies the previously reviewed SHA, not a hash calculated here and
then called approval. File protection authenticates local custody, not the truth
of identity evidence. Legacy submissions consume qualified prerequisites;
explicit setup installs only the selected reviewed identity and metadata chain.
Neither mode creates a review or grants authority to a browser page.
"""
import hashlib
import json
import re

from .auth import AuthStore, ROLE_SCOPES
from .common import AppError
from .db import Database, digest
from .dsh_app import Application
from .local_device_pairing import _read_protected
from .local_pending_import import check_import_storage, check_import_workspace
from .local_workspace_preparation import _acl, _file, _shell_path, local_f_path
from .native_registration import REQUEST_FIELDS
from .paths import Paths
from .workbench_pairing_entry import _packet, _selection

MAX_REQUEST_BYTES = 32768


def _parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def constant(_):
        raise ValueError()

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=constant)
        from .native_runtime import SETUP, REVOKE
        if type(value) is dict and value.get('format') == REVOKE:
            if (set(value) != {'format','account_key','operator_key','source_id','source_version'} or
                    not all(type(v) is str and 0 < len(v) <= 256 for v in value.values())): raise ValueError()
            return value
        setup = type(value) is dict and value.get('format') == SETUP
        fields = REQUEST_FIELDS | {'format', 'record', 'review_basis'}
        if setup: fields |= {'identity_source','observation','runtime_policy'}
        if (type(value) is not dict or set(value) != fields or
                value['format'] not in ('local-native-registration-submit-1', SETUP) or
                type(value['account_key']) is not str or type(value['review_basis']) is not dict or
                type(value['review_basis'].get('execution_review')) is not dict or
                type(value['review_basis']['execution_review'].get('operator_key')) is not str):
            raise ValueError()
        return value
    except (UnicodeError, ValueError, RecursionError):
        raise AppError(400, 'LOCAL_NATIVE_FILE_INVALID', '受保护登记请求格式无效。') from None


class _NativeSubmissionPermit:
    """Ephemeral exact-operation grant, owned by the host's outer transaction.

    Not a public constructor/API. The entry creates it inside the sole outer
    transaction, never returns it, and invalidates it before leaving that scope.
    Private Python calls are trusted host code, not a sandbox against injected code.
    """
    def __init__(self, app, request, principal):
        self.app, self.auth, self.db = app, app.auth, app.db
        self.config = digest(app.config)
        self.request = digest(request)
        self.registration = {key: request[key] for key in REQUEST_FIELDS}
        self.principal = principal
        self.active = True

    def close(self):
        self.active = False

    def _check(self, app, principal):
        if (not self.active or app is not self.app or app.auth is not self.auth or app.db is not self.db or
                principal != self.principal or digest(app.config) != self.config or
                app.config['source_mode'] != 'unverified' or app.read_only or app.db.read_only or
                app.writes_enabled or not app.db.conn.in_transaction):
            raise AppError(403, 'LOCAL_NATIVE_PERMIT_INVALID', '本次有限登记许可已失效或范围变化。')
        principal.require('source:register', self.registration['account_key'])

    def check_submission(self, app, request, principal):
        self._check(app, principal)
        if digest(request) != self.request:
            raise AppError(403, 'LOCAL_NATIVE_PERMIT_INVALID', '本次登记内容与许可不同。')

    def check_registration(self, app, request, principal):
        self._check(app, principal)
        if request != self.registration:
            raise AppError(403, 'LOCAL_NATIVE_PERMIT_INVALID', '本次登记来源版本与许可不同。')


def submit_protected_native_registration(data_root, request_file, expected_sha256, powershell_exe):
    """One exact reviewed request, existing DPAPI authority, no HTTP listener.

    Legacy submissions do not create identities or observations. Explicit setup
    installs reviewed prerequisites and a separate runtime policy atomically;
    explicit revocation tombstones that policy. No discovery or review is made
    here. Failures preserve the request and roll back the complete operation.
    """
    if type(expected_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise AppError(400, 'LOCAL_NATIVE_REVIEWED_HASH_REQUIRED', '需要事先选定并审核的请求摘要。')
    paths = Paths.from_root(str(local_f_path(data_root)))
    request_path = local_f_path(request_file)
    if request_path.parent != paths.root / 'validation' or request_path.suffix != '.json':
        raise AppError(400, 'LOCAL_NATIVE_FILE_LOCATION', '登记请求须在本工作区受保护的validation目录。')
    shell = _shell_path(powershell_exe)

    def read_request():
        _file(request_path)
        _acl(shell, [paths.root / 'validation', request_path], timeout_seconds=5)
        raw = _read_protected(request_path, MAX_REQUEST_BYTES)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise AppError(409, 'LOCAL_NATIVE_FILE_CHANGED', '登记请求与事先审核的摘要不一致。')
        return raw

    config = check_import_workspace(paths, shell, prepared_login=True)
    raw = read_request()
    request = _parse(raw)
    account = request['account_key']
    from .native_runtime import SETUP, REVOKE, prepare_setup, install_policy, revoke_policy
    operator = request['operator_key'] if request['format'] == REVOKE else request['review_basis']['execution_review']['operator_key']
    auth = AuthStore(paths.auth, allowed_extension_ids=config['extension_ids'])
    packet = _packet(paths, auth.protector)
    expected = _selection(paths, config, operator, [account], packet['extension_id'])
    if packet['account_keys'] != [account] or any(packet[key] != value for key, value in expected.items()):
        raise AppError(403, 'LOCAL_NATIVE_LOGIN_BINDING_CHANGED', '需要原主管、唯一账号及本机设备登记。')
    wanted = {key: expected[key] for key in ('operator_key', 'role', 'device_key', 'account_keys')}
    wanted['scopes'] = sorted(ROLE_SCOPES['manager'])
    session = database = app = None
    try:
        session = auth.login(operator, packet['login_credential'], device_key=config['device_key'], client_kind='workbench')
        token = session['token']
        principal = auth.authenticate(token, scope='source:register', account_key=account)
        if principal.role != 'manager' or {k: v for k, v in principal.as_dict().items() if k != 'session_id'} != wanted:
            raise AppError(403, 'LOCAL_NATIVE_LOGIN_BINDING_CHANGED', '主管登记权限已变化。')
        if check_import_workspace(paths, shell, prepared_login=True) != config or read_request() != raw:
            raise AppError(409, 'LOCAL_NATIVE_INPUT_CHANGED', '打开数据库前工作区或请求已变化。')
        database = Database(paths.db, create=False)
        app = Application(paths, auth=auth, database=database)
        if app.config != config:
            raise AppError(409, 'LOCAL_NATIVE_INPUT_CHANGED', '工作区配置已变化。')
        check_import_storage(app, shell, prepared_login=True)

        def reauthorize(original, requested_account):
            if original != principal or requested_account != account:
                raise AppError(403, 'LOCAL_NATIVE_AUTH_CHANGED', '本次登记授权范围变化。')
            return auth.authenticate(token, scope='source:register', account_key=account)

        from .local_native_registration import LocalNativeRegistration
        # Established order: DB -> auth -> transaction. Auth stays held through
        # COMMIT; file/protection checks here are bounded, not network operations.
        with database.lock:
            with auth.authorized(token, scope='source:register', account_key=account) as current:
                if current != principal:
                    raise AppError(403, 'LOCAL_NATIVE_AUTH_CHANGED', '提交前授权已变化。')
                with database.transaction():
                    check_import_storage(app, shell, prepared_login=True)
                    if read_request() != raw:
                        raise AppError(409, 'LOCAL_NATIVE_INPUT_CHANGED', '提交请求已变化。')
                    submission = prepare_setup(app, request, principal) if request['format'] == SETUP else request
                    permit = None if request['format'] == REVOKE else _NativeSubmissionPermit(app, submission, principal)
                    try:
                        if request['format'] == REVOKE:
                            result = revoke_policy(app, request, principal)
                        else:
                            result = LocalNativeRegistration(app)._submit(submission, principal, reauthorize, permit=permit)
                            if request['format'] == SETUP: install_policy(app, request, principal)
                        check_import_storage(app, shell, prepared_login=True)
                        if read_request() != raw or reauthorize(principal, account) != principal:
                            raise AppError(409, 'LOCAL_NATIVE_INPUT_CHANGED', '最终提交前输入或授权已变化。')
                        if permit: permit.check_submission(app, submission, principal)
                    finally:
                        if permit: permit.close()
        # Success is emitted only after the outer COMMIT. No native identity,
        # source keys, credential, token, request content or review reason escapes.
        return {'format': 'local-native-protected-submit-1', 'state': result['state'],
                'request_sha256': expected_sha256, 'general_writes_enabled': False,
                'context_authorization': 'not_granted', 'native_page_verification': 'background_required',
                'identity_or_observation_created': request['format'] == SETUP, 'service_started': False}
    finally:
        try:
            if session is not None:
                auth.logout(session['token'])
        finally:
            if app is not None:
                app.close()
            elif database is not None:
                database.close()
