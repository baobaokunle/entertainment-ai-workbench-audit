"""One-use local Web login through the existing AuthStore. No extension grant."""
import math
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .auth import AuthStore, ID_RE, Principal, ROLE_SCOPES
from .common import AppError
from .local_device_pairing import NONCE_RE

ORIGIN = 'http://127.0.0.1:8801'


class WorkbenchWebLogin:
    def __init__(self, auth, credential, *, operator_key, device_key, ttl_seconds=300, clock=time.monotonic):
        if (not isinstance(auth, AuthStore) or type(credential) is not str or not 12 <= len(credential) <= 1024 or
            any(type(value) is not str or not ID_RE.fullmatch(value) for value in (operator_key, device_key)) or
            operator_key == 'identity-pilot' or device_key == 'F-identity-pilot' or
            type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300):
            raise AppError(400, 'WEB_LOGIN_CONFIGURATION_INVALID', '工作台登录需要明确的原登记和有限期限')
        self._auth, self._credential = auth, credential
        self._operator, self._device = operator_key, device_key
        self._clock, self._last = clock, None
        self._lock = threading.Lock()
        self._used, self._failures = False, 0
        self._deadline = self._now() + ttl_seconds
        self.code = secrets.token_urlsafe(32)
        self.expires_at_utc = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        result = self._login()
        try:
            self.authority = self._binding(result['principal'])
        finally:
            auth.logout(result['token'])

    def _now(self):
        value = self._clock()
        if (type(value) not in (int, float) or not math.isfinite(value) or value < 0 or
            self._last is not None and value < self._last):
            self._used = True
            raise AppError(410, 'WEB_LOGIN_EXPIRED', '本次登录码已失效，请重新打开本机登录窗口')
        self._last = value
        return value

    def _binding(self, principal):
        if (not isinstance(principal, Principal) or principal.operator_key != self._operator or
            principal.device_key != self._device or principal.role not in ROLE_SCOPES or
            principal.scopes != ROLE_SCOPES[principal.role] or 'snapshot-owner-unverified' in principal.account_keys):
            raise AppError(403, 'WEB_LOGIN_AUTH_CHANGED', '工作台原登记权限未能核实')
        return {'operator_key': principal.operator_key, 'device_key': principal.device_key,
                'role': principal.role, 'account_keys': sorted(principal.account_keys), 'scopes': sorted(principal.scopes)}

    def _login(self):
        return self._auth.login(self._operator, self._credential, device_key=self._device, client_kind='workbench')

    def require_app(self, app):
        if self._auth is not app.auth or self._device != app.config['device_key']:
            raise AppError(403, 'WEB_LOGIN_APP_MISMATCH', '本次登录码与当前工作台不一致')

    def redeem(self, code):
        with self._lock:
            if self._now() >= self._deadline or self._used or self._failures >= 5:
                raise AppError(410, 'WEB_LOGIN_EXPIRED', '本次登录码已使用或失效，请重新打开本机登录窗口')
            if type(code) is not str or not NONCE_RE.fullmatch(code) or not secrets.compare_digest(code, self.code):
                self._failures += 1
                raise AppError(401, 'WEB_LOGIN_CODE_INVALID', '登录码未通过核对')
            # Consume before the existing login, including authentication failure.
            self._used = True
            result = self._login()
            try:
                current = self._auth.authenticate(result['token'], scope='capabilities:read')
                if (self._now() >= self._deadline or current != result['principal'] or
                    self._binding(current) != self.authority):
                    raise AppError(403, 'WEB_LOGIN_AUTH_CHANGED', '工作台原登记权限已改变或登录码已到期')
                return result
            except BaseException:
                self._auth.logout(result['token'])
                raise


def require_fictional_writable(app):
    if app.read_only or app.db.read_only or not app.writes_enabled or app.config.get('source_mode')!='fictional':
        raise AppError(403,'WEB_LOGIN_FICTIONAL_REQUIRED','可写免记密码入口仅用于明确虚构工作区，不激活真实来源')


def _prepared(app, operator):
    from .workbench_pairing_entry import _packet, _selection
    if not isinstance(app.auth, AuthStore) or app.auth.path != app.paths.auth:
        raise AppError(403, 'WEB_LOGIN_APP_MISMATCH', '需使用当前工作台原认证实例')
    if not app.read_only:
        if getattr(app,'local_notes',None) is not None:app.local_notes.require_login(operator)
        else:require_fictional_writable(app)
    packet = _packet(app.paths, app.auth.protector)
    expected = _selection(app.paths, app.config, operator, packet['account_keys'], packet['extension_id'])
    if any(packet[key] != value for key, value in expected.items()):
        raise AppError(403, 'WEB_LOGIN_PACKET_MISMATCH', '准备记录与工作台不同，保留原登记')
    wanted = {key: expected[key] for key in ('operator_key', 'device_key', 'role', 'account_keys')}
    wanted['scopes'] = sorted(ROLE_SCOPES[expected['role']])
    return packet, wanted


def worker_session_guard(app, session, account):
    auth=app.auth;principal=session['principal'];device=app.config['device_key']
    policy=_finite_reply(app)
    def reauthorize():
        if policy is None:require_fictional_writable(app)
        elif _finite_reply(app) is not policy or not policy.host_active() or policy.account!=account:
            raise AppError(403,'AI_WORKER_AUTH_CHANGED','有限回复的本次宿主会话已变化')
        if app.auth is not auth or app.config['device_key']!=device or principal.role!='manager':
            raise AppError(403,'AI_WORKER_AUTH_CHANGED','后台处理与本次原认证不同')
        current=auth.authenticate(session['token'],scope='model:request',account_key=account)
        current.require('identities:write',account)
        if current!=principal:raise AppError(403,'AI_WORKER_AUTH_CHANGED','后台处理权限已变化')
        return current
    reauthorize()
    return reauthorize


def _finite_reply(app):
    from .local_reply import LocalReplyAccess
    policy=getattr(app,'local_reply',None)
    if policy is None:return None
    if (not isinstance(policy,LocalReplyAccess) or policy.app is not app or
            policy.notes is not app.local_notes or policy.service is not app.ai or
            app.ai.execution_policy is not policy):
        raise AppError(403,'AI_WORKER_AUTH_CHANGED','有限回复必须使用当前工作台原策略')
    return policy


def worker_commit_guard(app, session, account):
    auth=app.auth;reauthorize=worker_session_guard(app,session,account)
    @contextmanager
    def commit():
        with auth.authorized(session['token'],scope='model:request',account_key=account):
            reauthorize()
            yield
    return commit


def prepared_worker_session(app, operator, account):
    policy=_finite_reply(app)
    if policy is None:require_fictional_writable(app)
    elif policy.account!=account:
        raise AppError(403,'AI_WORKER_AUTH_CHANGED','有限回复需使用原登记的唯一账号')
    packet,wanted=_prepared(app,operator)
    result=app.auth.login(operator,packet['login_credential'],device_key=app.config['device_key'],client_kind='workbench')
    try:
        principal=app.auth.authenticate(result['token'],scope='model:request',account_key=account)
        if principal!=result['principal']:raise AppError(403,'WEB_LOGIN_AUTH_CHANGED','本次登录权限已变化')
        actual={key:value for key,value in principal.as_dict().items() if key!='session_id'}
        if actual!=wanted:raise AppError(403,'WEB_LOGIN_AUTH_CHANGED','准备记录与原权限不同')
        if policy is not None:policy.bind_host_session(result)
        guard=worker_session_guard(app,result,account)
        return result,guard
    except BaseException:
        app.auth.logout(result['token']);raise


def attach_web_login(app, operator, duration=300):
    packet,wanted=_prepared(app,operator)
    grant = WorkbenchWebLogin(app.auth, packet['login_credential'], operator_key=operator,
                             device_key=app.config['device_key'], ttl_seconds=duration)
    if grant.authority != wanted:
        raise AppError(403, 'WEB_LOGIN_AUTH_CHANGED', '原权限与准备记录不同，未启用登录码')
    app.web_login = grant
    return grant


def web_login_handoff(app, grant):
    from .backup_scratch import reject_links
    from .common import canonical_json
    from .local_device_pairing import _write_new
    grant.require_app(app)
    directory = app.paths.root / 'keys' / 'workbench-web-login'
    reject_links(directory)
    directory.mkdir(exist_ok=True)
    notes=getattr(app,'local_notes',None)
    if notes is not None:notes.check_login_area()
    target = directory / (secrets.token_hex(16) + '.dpapi')
    packet = {'format': 'workbench-web-login-2', 'origin': ORIGIN,
              'workspace_mode': 'readonly' if app.read_only else 'local-reply' if _finite_reply(app) is not None else 'local-notes' if notes is not None else 'fictional-writable',
              'operator_key': grant.authority['operator_key'], 'device_key': grant.authority['device_key'],
              'role': grant.authority['role'], 'account_count': str(len(grant.authority['account_keys'])),
              'code': grant.code, 'expires_at_utc': grant.expires_at_utc}
    _write_new(target, app.auth.protector.protect(canonical_json(packet)))
    if notes is not None:
        notes.check_login_area(target)
        notes.login_file=target
    return target
