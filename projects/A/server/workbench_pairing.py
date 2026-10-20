"""Explicit normal-app adapter over the existing bounded local pairing grant.

No enrollment, file, database, source activation, UI or credential discovery.
The extension gets a separate HMAC key, never the normal login credential.
"""
import hashlib
import hmac
import math
import secrets
import threading
import time

from .auth import AuthStore, EXTENSION_SCOPES, ID_RE, Principal
from .common import AppError, canonical_json
from .local_device_pairing import LocalPairingGrant, NONCE_RE


class WorkbenchPairingGrant(LocalPairingGrant):
    def __init__(self, auth, login_credential, device_credential, extension_id, *,
                 operator_key, device_key, ttl_seconds=300, host_ttl_seconds=None,
                 clock=time.monotonic, until_stopped=False):
        if type(until_stopped) is not bool or until_stopped and host_ttl_seconds is not None:
            raise AppError(422, 'WORKBENCH_PAIRING_LIFETIME_INVALID', 'Choose either a finite normal host or until stopped.')
        self._until_stopped = until_stopped
        if not isinstance(auth, AuthStore):
            raise AppError(422, 'WORKBENCH_PAIRING_AUTH_REQUIRED', '配对必须复用当前工作台认证实例')
        if (type(login_credential) is not str or not 12 <= len(login_credential) <= 1024 or
            type(device_credential) is not str or not NONCE_RE.fullmatch(device_credential) or
            secrets.compare_digest(login_credential.encode('utf-8'), device_credential.encode('ascii'))):
            raise AppError(422, 'WORKBENCH_PAIRING_KEYS_INVALID', '正常登录凭据与独立配对密钥必须分开')
        if (type(operator_key) is not str or not ID_RE.fullmatch(operator_key) or operator_key == 'identity-pilot' or
            type(device_key) is not str or not ID_RE.fullmatch(device_key) or device_key == 'F-identity-pilot'):
            raise AppError(422, 'WORKBENCH_PAIRING_BINDING_INVALID', '配对需要明确的正常操作者与设备')
        self._operator = operator_key
        self._normal_device = device_key
        self._login_credential = login_credential
        self._access_guard = None
        self._finite_notes = None
        self._clock_lock = threading.Lock()
        self._last_clock = None
        def finite_clock():
            with self._clock_lock:
                value = clock()
                if (type(value) not in (int, float) or not math.isfinite(value) or value < 0 or
                    self._last_clock is not None and value < self._last_clock):
                    raise AppError(410, 'WORKBENCH_PAIRING_CLOCK_INVALID', '配对时钟未能核实，停止本次连接')
                self._last_clock = value
                return value
        super().__init__(auth, device_credential, extension_id, ttl_seconds, finite_clock,
                         host_ttl_seconds=host_ttl_seconds)
        if until_stopped:
            # Only this normal-workbench instance loses the host cutoff. Parent
            # pilot semantics, one-use code and 30-second challenges are unchanged.
            self._deadline = math.inf
        # Authenticate the existing enrollment; never create or replace it.
        result = self._normal_login()
        try:
            self._authority = self._binding(result['principal'])
            self.authority_digest = hashlib.sha256(canonical_json(self._authority)).hexdigest()
        finally:
            self._auth.logout(result['token'])

    def _binding(self, principal):
        if (not isinstance(principal, Principal) or principal.operator_key != self._operator or
            principal.device_key != self._normal_device or principal.role not in ('manager', 'baobao') or
            not principal.scopes <= EXTENSION_SCOPES or 'capabilities:read' not in principal.scopes or
            'snapshot-owner-unverified' in principal.account_keys):
            raise AppError(403, 'WORKBENCH_PAIRING_BINDING_INVALID', '正常配对授权不匹配；不返回会话')
        return {'operator_key': principal.operator_key, 'device_key': principal.device_key,
                'role': principal.role, 'account_keys': sorted(principal.account_keys),
                'scopes': sorted(principal.scopes), 'extension_id': self._extension_id}

    def _normal_login(self):
        return self._auth.login(self._operator, self._login_credential, device_key=self._normal_device,
                                client_kind='extension', extension_id=self._extension_id,
                                until_logout=self._until_stopped)

    def _login(self):
        result = self._normal_login()
        try:
            if self._binding(result['principal']) != self._authority:
                raise AppError(403, 'WORKBENCH_PAIRING_AUTH_CHANGED', '配对期间原授权已改变，需要重新核对')
            current = self._auth.authenticate(result['token'], scope='capabilities:read')
            if current != result['principal'] or self._binding(current) != self._authority:
                raise AppError(403, 'WORKBENCH_PAIRING_AUTH_CHANGED', '配对期间原授权已改变，需要重新核对')
            return result
        except BaseException:
            self._auth.logout(result['token'])
            raise

    def _proof(self, domain, client_challenge, server_challenge):
        domains = {'identity-pilot-host-1': 'workbench-host-1',
                   'identity-pilot-client-1': 'workbench-client-1'}
        if domain not in domains:
            raise AppError(400, 'WORKBENCH_PAIRING_DOMAIN_INVALID', '配对证明域无效')
        message = '\n'.join((domains[domain], self._extension_id, self.authority_digest,
                             client_challenge, server_challenge)).encode('ascii')
        return hmac.new(self._credential.encode('ascii'), message, hashlib.sha256).hexdigest()

    def redeem(self, extension_id, code):
        return dict(self._checked(super().redeem, extension_id, code), authority_digest=self.authority_digest)

    def challenge(self, extension_id, client_challenge):
        return dict(self._checked(super().challenge, extension_id, client_challenge), authority_digest=self.authority_digest)

    def resume(self, extension_id, client_challenge, server_challenge, proof):
        return dict(self._checked(super().resume, extension_id, client_challenge, server_challenge, proof),
                    authority_digest=self.authority_digest)

    def _checked(self, action, *args):
        if self._access_guard is not None:self._access_guard()
        result=action(*args)
        try:
            if self._access_guard is not None:self._access_guard()
            return result
        except BaseException:
            if isinstance(result,dict) and 'token' in result:self._auth.logout(result['token'])
            raise

    def require_app(self, app, extension_id):
        if (self._auth is not app.auth or self._normal_device != app.config['device_key'] or
            extension_id != self._extension_id):
            raise AppError(403, 'WORKBENCH_PAIRING_APP_MISMATCH', '配对与本工作台安装绑定不同')

    def status(self):
        return self._checked(self._status)

    def _status(self):
        with self._lock:
            now = self._live()
            return {'format': 'workbench-pairing-status-1', 'available': True,
                    'code_available': not self._redeemed and self._failures < 5 and now < self._pairing_deadline,
                    'authenticated': False, 'native_page_verified': False}
