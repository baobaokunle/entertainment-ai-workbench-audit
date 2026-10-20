"""CLI-enrolled identities; credentials and authority never come from a login role."""
from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from .common import AppError, canonical_json
from .security import DPAPIProtector

BASE_SCOPES = frozenset({'history:read', 'sidebar:read', 'observations:write',
                        'actions:write', 'capabilities:read','profile:write','model:read','model:request','model:correct'})
LOCAL_BACKUP_SCOPES = frozenset({'backup:local:create','backup:local:verify','backup:local:restore'})
ROLE_SCOPES = {'manager': BASE_SCOPES | LOCAL_BACKUP_SCOPES | {'identities:write', 'source:register',
               'backup:create', 'backup:verify', 'backup:restore', 'activities:read','collection:request','activity_sources:approve','activities:retain','deletion:plan','diagnostics:read','diagnostics:configure'},
               'baobao': BASE_SCOPES | LOCAL_BACKUP_SCOPES | {'activities:self','collection:run','collection:configure','diagnostics:self'}}
EXTENSION_SCOPES = BASE_SCOPES
ID_RE = re.compile(r'^[A-Za-z0-9_:.\-]{1,128}$')
EXTENSION_RE = re.compile(r'^[a-p]{32}$')
ITERATIONS = 600_000


@dataclass(frozen=True)
class Principal:
    operator_key: str
    role: str
    device_key: str
    session_id: str
    scopes: frozenset
    account_keys: frozenset

    def require(self, scope, account_key=None):
        if scope not in self.scopes or (account_key is not None and account_key not in self.account_keys):
            raise AppError(403, 'forbidden', 'This session is not authorized for that operation or account.')
        return self

    def as_dict(self):
        return dict(operator_key=self.operator_key, role=self.role, device_key=self.device_key,
                    session_id=self.session_id, scopes=sorted(self.scopes), account_keys=sorted(self.account_keys))


class AuthStore:
    def __init__(self, path, protector=None, *, allowed_extension_ids=(), session_ttl_seconds=3600):
        self.path = Path(path)
        self.protector = protector if protector is not None else DPAPIProtector()
        self.allowed_extension_ids = frozenset(allowed_extension_ids)
        if any(not isinstance(v, str) or not EXTENSION_RE.fullmatch(v) for v in self.allowed_extension_ids):
            raise AppError(422, 'invalid_extension', 'Extension IDs must be exact registered Chrome IDs.')
        if isinstance(session_ttl_seconds, bool) or not isinstance(session_ttl_seconds, (int, float)) or not 0 < session_ttl_seconds <= 86400:
            raise AppError(422, 'invalid_ttl', 'Session lifetime must be within one day.')
        self.ttl = session_ttl_seconds
        self._lock = threading.RLock()
        self._sessions = {}
        self._records = {}
        self._failures = {}
        self._dummy_salt = secrets.token_bytes(32)
        if self.path.exists():
            try:
                if self.path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError('size')
                payload = json.loads(self.protector.unprotect(self.path.read_bytes()))
                if payload.get('version') != 1 or not isinstance(payload.get('records'), dict):
                    raise ValueError('format')
                for operator, record in payload['records'].items():
                    self._validate_identity(operator, record['role'], record['device_key'], record['account_keys'])
                    if len(bytes.fromhex(record['salt'])) != 32 or len(bytes.fromhex(record['digest'])) != 32 or record['iterations'] != ITERATIONS:
                        raise ValueError('digest')
                self._records = payload['records']
            except AppError:
                raise
            except Exception as exc:
                raise AppError(503, 'auth_store_invalid', 'Protected enrollment store could not be loaded.') from exc
        self._store_stamp = self._stamp()

    def _stamp(self):
        try:
            info = self.path.stat()
            return info.st_mtime_ns, info.st_size, info.st_ino
        except FileNotFoundError:
            return None

    def _refresh(self):
        """A separate CLI enrollment change invalidates this process's sessions."""
        if self._stamp() != self._store_stamp:
            self._sessions.clear()
            if not self.path.exists():
                self._records.clear()
                raise AppError(503, 'auth_store_missing', 'Enrollment store was removed; sessions revoked.')
            refreshed = AuthStore(self.path, self.protector, allowed_extension_ids=self.allowed_extension_ids,
                                  session_ttl_seconds=self.ttl)
            self._records = refreshed._records
            self._store_stamp = refreshed._store_stamp

    @staticmethod
    def _validate_identity(operator, role, device, accounts):
        if role not in ROLE_SCOPES or any(not isinstance(v, str) or not ID_RE.fullmatch(v) for v in (operator, device)):
            raise AppError(422, 'invalid_enrollment', 'Enrollment identity or role is invalid.')
        if not isinstance(accounts, (tuple, list, set, frozenset)) or len(accounts) > 1000 or any(not isinstance(v, str) or not ID_RE.fullmatch(v) for v in accounts):
            raise AppError(422, 'invalid_accounts', 'Explicit account scopes are required.')

    def _save(self, records):
        data = self.protector.protect(canonical_json({'version': 1, 'records': records}))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.' + secrets.token_hex(8) + '.tmp')
        try:
            with tmp.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            self._store_stamp = self._stamp()
        finally:
            tmp.unlink(missing_ok=True)

    def enroll(self, operator_key, role, device_key, credential, *, account_keys=()):
        self._validate_identity(operator_key, role, device_key, account_keys)
        if not isinstance(credential, str) or not 12 <= len(credential) <= 1024:
            raise AppError(422, 'invalid_credential', 'Enrollment credential must contain 12 to 1024 characters.')
        salt = secrets.token_bytes(32)
        record = dict(role=role, device_key=device_key, account_keys=sorted(set(account_keys)),
                      salt=salt.hex(), iterations=ITERATIONS,
                      digest=hashlib.pbkdf2_hmac('sha256', credential.encode(), salt, ITERATIONS).hex())
        with self._lock:
            self._refresh()
            records = dict(self._records)
            records[operator_key] = record
            self._save(records)
            self._records = records
            self._sessions = {k: v for k, v in self._sessions.items() if v[0].operator_key != operator_key}
        return {'operator_key': operator_key, 'role': role, 'device_key': device_key, 'account_keys': record['account_keys']}

    def login(self, operator_key, credential, *, device_key, client_kind='workbench', extension_id=None, until_logout=False):
        # Host-only opt-in for the normal extension; HTTP startup cannot supply it.
        if type(until_logout) is not bool or until_logout and (client_kind != 'extension' or operator_key == 'identity-pilot'):
            raise AppError(403, 'invalid_session_lifetime', 'Persistent sessions require an explicit normal extension host.')
        if client_kind not in ('workbench', 'extension'):
            raise AppError(403, 'invalid_client', 'Client kind is not permitted.')
        if client_kind == 'extension' and (not isinstance(extension_id, str) or extension_id not in self.allowed_extension_ids):
            raise AppError(403, 'unregistered_extension', 'Extension is not registered.')
        if not isinstance(operator_key, str) or not isinstance(device_key, str) or not isinstance(credential, str) or len(credential) > 1024:
            raise AppError(401, 'invalid_credentials', 'Credentials or device binding are invalid.')
        with self._lock:
            self._refresh()
            now = time.monotonic()
            attempts, until = self._failures.get(operator_key, (0, 0))
            if now < until:
                raise AppError(429, 'login_throttled', 'Wait before attempting authentication again.')
            record = self._records.get(operator_key)
            salt = bytes.fromhex(record['salt']) if record else self._dummy_salt
            digest = hashlib.pbkdf2_hmac('sha256', credential.encode(), salt, ITERATIONS)
            expected = bytes.fromhex(record['digest']) if record else b'\x00' * 32
            credentials_ok = hmac.compare_digest(digest, expected)
            device_ok = hmac.compare_digest(device_key.encode(), (record['device_key'] if record else '').encode())
            if not record or not credentials_ok or not device_ok:
                self._failures[operator_key] = (attempts + 1, now + min(60, 2 ** min(attempts, 6)) if attempts >= 4 else 0)
                if len(self._failures) > 1024:
                    self._failures.pop(next(iter(self._failures)))
                raise AppError(401, 'invalid_credentials', 'Credentials or device binding are invalid.')
            self._failures.pop(operator_key, None)
            self._sessions = {k: v for k, v in self._sessions.items() if v[1] is None or now < v[1]}
            if len(self._sessions) >= 1024:
                raise AppError(429, 'session_limit', 'Active session limit reached.')
            token = secrets.token_urlsafe(32)
            scopes = ROLE_SCOPES[record['role']]
            if client_kind == 'extension':
                scopes &= EXTENSION_SCOPES
            principal = Principal(operator_key, record['role'], device_key, secrets.token_hex(16),
                                  frozenset(scopes), frozenset(record['account_keys']))
            self._sessions[hashlib.sha256(token.encode()).digest()] = (principal, None if until_logout else now + self.ttl)
            return {'token': token, 'principal': principal, 'expires_in': None if until_logout else self.ttl,
                    'expires_at': None if until_logout else datetime.fromtimestamp(time.time() + self.ttl, timezone.utc).isoformat()}

    def authenticate(self, token, *, scope=None, account_key=None):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise AppError(401, 'invalid_session', 'Session is missing, expired or revoked.')
        key = hashlib.sha256(token.encode()).digest()
        with self._lock:
            self._refresh()
            session = self._sessions.get(key)
            if session is None or session[1] is not None and time.monotonic() >= session[1]:
                self._sessions.pop(key, None)
                raise AppError(401, 'invalid_session', 'Session is missing, expired or revoked.')
            principal = session[0]
            if scope:
                principal.require(scope, account_key)
            elif account_key is not None and account_key not in principal.account_keys:
                raise AppError(403, 'forbidden', 'This account is not authorized.')
            return principal

    @contextmanager
    def authorized(self, token, *, scope, account_key=None):
        """Order one short local commit with same-instance logout/enroll.

        Callers using a database must hold its lock first; never hold this
        context across model inference or other slow external operations.
        """
        with self._lock:
            yield self.authenticate(token, scope=scope, account_key=account_key)

    def logout(self, token):
        if isinstance(token, str):
            with self._lock:
                self._sessions.pop(hashlib.sha256(token.encode()).digest(), None)
