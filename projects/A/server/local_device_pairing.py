"""User-bound device enrollment and bounded local pairing for the read-only pilot.

No business service, database, index, browser session or legacy enrollment is
opened here. Device secrets are protected by the existing Windows user DPAPI.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from .auth import AuthStore, EXTENSION_RE, ID_RE, ITERATIONS
from .backup_scratch import reject_links
from .common import AppError, canonical_json
from .security import DPAPIProtector


OPERATOR_KEY = 'identity-pilot'
DEVICE_KEY = 'F-identity-pilot'
ROLE = 'manager'
AUTH_FILE = 'pilot-device-auth.dpapi'
KEY_FILE = 'pilot-device-key.dpapi'
NONCE_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
PROOF_RE = re.compile(r'^[0-9a-f]{64}$')
_KEY_FIELDS = frozenset({'version', 'extension_id', 'operator_key', 'device_key',
                         'account_key', 'credential'})
_RECORD_FIELDS = frozenset({'role', 'device_key', 'account_keys', 'salt',
                            'iterations', 'digest'})
_NO_ACCOUNT = object()


def _error(status, code, message):
    return AppError(status, code, message)


def _binding(extension_id, account_key=_NO_ACCOUNT):
    if not isinstance(extension_id, str) or not EXTENSION_RE.fullmatch(extension_id):
        raise _error(422, 'invalid_extension', 'An exact registered extension ID is required.')
    if account_key is not _NO_ACCOUNT and (not isinstance(account_key, str) or not ID_RE.fullmatch(account_key)):
        raise _error(422, 'invalid_accounts', 'An explicit pilot account binding is required.')


def _file_stat(path, *, missing_ok=False):
    reject_links(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise _error(503, 'device_store_invalid', 'Protected device enrollment is incomplete.') from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _error(403, 'device_path_forbidden', 'Device enrollment must use ordinary exclusive files.')
    return info


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_protected(path, maximum):
    info = _file_stat(path)
    if not 0 < info.st_size <= maximum:
        raise _error(503, 'device_store_invalid', 'Protected device enrollment format is invalid.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(info) or opened.st_nlink != 1:
            raise _error(503, 'device_store_changed', 'Protected device enrollment changed while opening.')
        raw = stream.read(maximum + 1)
        finished = os.fstat(stream.fileno())
    if len(raw) != info.st_size or _identity(finished) != _identity(info):
        raise _error(503, 'device_store_changed', 'Protected device enrollment changed while reading.')
    if _identity(_file_stat(path)) != _identity(info):
        raise _error(503, 'device_store_changed', 'Protected device enrollment changed while reading.')
    return raw


def _write_new(path, protected):
    """Exclusive creation; an interrupted partial file is retained and rejected."""
    reject_links(path)
    try:
        with path.open('xb') as stream:
            stream.write(protected)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise _error(503, 'device_store_create_failed', 'Device enrollment could not be created; existing files were preserved.') from None


def _json(raw, protector):
    def strict_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate field')
            result[key] = value
        return result
    try:
        return json.loads(protector.unprotect(raw), object_pairs_hook=strict_pairs)
    except Exception:
        raise _error(503, 'device_store_invalid', 'Protected device enrollment could not be verified.') from None


def _records(records, account_key):
    if not isinstance(records, dict) or set(records) != {OPERATOR_KEY}:
        raise _error(503, 'device_binding_invalid', 'Protected device authority does not match the pilot binding.')
    record = records[OPERATOR_KEY]
    if (not isinstance(record, dict) or set(record) != _RECORD_FIELDS
            or record['role'] != ROLE or record['device_key'] != DEVICE_KEY
            or record['account_keys'] != [account_key]
            or type(record['iterations']) is not int or record['iterations'] != ITERATIONS):
        raise _error(503, 'device_binding_invalid', 'Protected device authority does not match the pilot binding.')
    for name in ('salt', 'digest'):
        if not isinstance(record[name], str) or not PROOF_RE.fullmatch(record[name]):
            raise _error(503, 'device_store_invalid', 'Protected device authority format is invalid.')


class _DeviceAuthStore(AuthStore):
    """Only the independent device enrollment can be created, never replaced."""
    def __init__(self, path, protector, *, extension_id, account_key):
        self._device_account_key = account_key
        super().__init__(path, protector, allowed_extension_ids=[extension_id],
                         session_ttl_seconds=300)
        if self.path.exists():
            _records(self._records, account_key)

    def _stamp(self):
        _file_stat(self.path, missing_ok=True)
        return super()._stamp()

    def _refresh(self):
        _file_stat(self.path, missing_ok=True)
        super()._refresh()
        if self.path.exists():
            _records(self._records, self._device_account_key)

    def _save(self, records):
        _records(records, self._device_account_key)
        protected = self.protector.protect(canonical_json({'version': 1, 'records': records}))
        _write_new(self.path, protected)
        self._store_stamp = self._stamp()


def prepare_local_device_auth(state, extension_id, account_key, protector=None, *, require_existing=False):
    """Create or verify independent protected enrollment without replacing files.

    Returns the existing AuthStore interface and its random device credential.
    Callers must keep that credential out of logs, reports and page content.
    A single existing file is an interrupted enrollment, never auto-recovered.
    """
    _binding(extension_id, account_key)
    state = reject_links(Path(state))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    reject_links(state)
    if not state.is_dir():
        raise _error(403, 'device_path_forbidden', 'Device enrollment requires its own local directory.')
    auth_path, key_path = state / AUTH_FILE, state / KEY_FILE
    auth_present = _file_stat(auth_path, missing_ok=True) is not None
    key_present = _file_stat(key_path, missing_ok=True) is not None
    if require_existing and not (auth_present and key_present):
        raise _error(503, 'device_store_incomplete', 'Existing device enrollment is required for this validation round.')
    if auth_present != key_present:
        raise _error(503, 'device_store_incomplete', 'Device enrollment is incomplete; retained files require review.')
    protector = protector if protector is not None else DPAPIProtector()
    if key_present:
        payload = _json(_read_protected(key_path, 4096), protector)
        if (not isinstance(payload, dict) or set(payload) != _KEY_FIELDS
                or type(payload['version']) is not int or payload['version'] != 1
                or payload['extension_id'] != extension_id or payload['operator_key'] != OPERATOR_KEY
                or payload['device_key'] != DEVICE_KEY or payload['account_key'] != account_key
                or not isinstance(payload['credential'], str) or not NONCE_RE.fullmatch(payload['credential'])):
            raise _error(503, 'device_binding_invalid', 'Protected device key does not match the pilot binding.')
        credential = payload['credential']
        auth_raw = _read_protected(auth_path, 16 * 1024 * 1024)
        auth_payload = _json(auth_raw, protector)
        if (not isinstance(auth_payload, dict) or set(auth_payload) != {'version', 'records'}
                or type(auth_payload['version']) is not int or auth_payload['version'] != 1):
            raise _error(503, 'device_store_invalid', 'Protected device authority format is invalid.')
        _records(auth_payload['records'], account_key)
        auth = _DeviceAuthStore(auth_path, protector, extension_id=extension_id, account_key=account_key)
        if auth_raw != _read_protected(auth_path, 16 * 1024 * 1024):
            raise _error(503, 'device_store_changed', 'Protected device authority changed while loading.')
        # A virtual local session verifies both protected copies agree. No token
        # or credential is logged or left active by this consistency check.
        login = auth.login(OPERATOR_KEY, credential, device_key=DEVICE_KEY,
                           client_kind='extension', extension_id=extension_id)
        auth.logout(login['token'])
        return auth, credential
    credential = secrets.token_urlsafe(32)
    payload = {'version': 1, 'extension_id': extension_id, 'operator_key': OPERATOR_KEY,
               'device_key': DEVICE_KEY, 'account_key': account_key, 'credential': credential}
    protected = protector.protect(canonical_json(payload))
    _write_new(key_path, protected)
    # The key's exclusive create serializes concurrent new enrollment attempts.
    # _save also uses xb so a separately created auth file cannot be overwritten.
    auth = _DeviceAuthStore(auth_path, protector, extension_id=extension_id, account_key=account_key)
    if auth.path.exists():
        raise _error(503, 'device_store_changed', 'Device authority appeared during enrollment; retained files require review.')
    auth.enroll(OPERATOR_KEY, ROLE, DEVICE_KEY, credential, account_keys=[account_key])
    return auth, credential


class LocalPairingGrant:
    """One pairing code and bounded mutual authentication for a local host."""
    def __init__(self, auth, credential, extension_id, ttl_seconds=300, clock=time.monotonic, *, host_ttl_seconds=None):
        _binding(extension_id)
        if not isinstance(credential, str) or not NONCE_RE.fullmatch(credential):
            raise _error(422, 'invalid_device_credential', 'A protected random device credential is required.')
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 300):
            raise _error(422, 'invalid_pairing_ttl', 'Local pairing is limited to at most 300 seconds.')
        if host_ttl_seconds is None:
            host_ttl_seconds = ttl_seconds
        if (isinstance(host_ttl_seconds, bool) or not isinstance(host_ttl_seconds, (int, float))
                or not math.isfinite(host_ttl_seconds) or not ttl_seconds <= host_ttl_seconds <= 1800):
            raise _error(422, 'invalid_host_ttl', 'Local verification is limited to a finite host window up to 1800 seconds.')
        self._auth = auth
        self._credential = credential
        self._extension_id = extension_id
        self._clock = clock
        now = clock()
        self._pairing_deadline = now + ttl_seconds
        self._deadline = now + host_ttl_seconds
        self._code = secrets.token_urlsafe(32)
        self.expires_at_utc = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        self._redeemed = False
        self._failures = 0
        self._pending = {}
        self._lock = threading.Lock()

    @property
    def code(self):
        return self._code

    def _check_extension(self, extension_id):
        if (not isinstance(extension_id, str) or not EXTENSION_RE.fullmatch(extension_id)
                or not hmac.compare_digest(extension_id, self._extension_id)):
            raise _error(403, 'pairing_extension_forbidden', 'This extension is not bound to the local pilot.')

    def _live(self):
        now = self._clock()
        if now >= self._deadline:
            self._pending.clear()
            raise _error(410, 'pairing_expired', 'This local pilot pairing window has expired.')
        return now

    def _login(self):
        return self._auth.login(OPERATOR_KEY, self._credential, device_key=DEVICE_KEY,
                                client_kind='extension', extension_id=self._extension_id)

    def redeem(self, extension_id, code):
        self._check_extension(extension_id)
        with self._lock:
            self._live()
            if self._clock() >= self._pairing_deadline:
                raise _error(410, 'pairing_expired', 'This local pilot pairing window has expired.')
            if self._redeemed:
                raise _error(410, 'pairing_consumed', 'This local pilot pairing code has already been used.')
            if self._failures >= 5:
                raise _error(429, 'pairing_attempt_limit', 'Local pairing attempt limit reached.')
            if (not isinstance(code, str) or not NONCE_RE.fullmatch(code)
                    or not hmac.compare_digest(code, self._code)):
                self._failures += 1
                if self._failures >= 5:
                    raise _error(429, 'pairing_attempt_limit', 'Local pairing attempt limit reached.')
                raise _error(401, 'pairing_invalid', 'Local pairing proof is invalid.')
            # A correct proof is consumed before authentication. If enrollment
            # refresh or response delivery fails, the same code cannot mint a
            # second session on a retry.
            self._redeemed = True
            result = self._login()
            return dict(result, device_credential=self._credential)

    def _proof(self, domain, client_challenge, server_challenge):
        message = '\n'.join((domain, self._extension_id, client_challenge, server_challenge)).encode('ascii')
        return hmac.new(self._credential.encode('ascii'), message, hashlib.sha256).hexdigest()

    def challenge(self, extension_id, client_challenge):
        self._check_extension(extension_id)
        if not isinstance(client_challenge, str) or not NONCE_RE.fullmatch(client_challenge):
            raise _error(401, 'device_challenge_invalid', 'Local device challenge is invalid.')
        with self._lock:
            now = self._live()
            self._pending = {key: value for key, value in self._pending.items() if now < value[1]}
            if len(self._pending) >= 8:
                raise _error(429, 'device_challenge_limit', 'Pending local device challenge limit reached.')
            server_challenge = secrets.token_urlsafe(32)
            self._pending[server_challenge] = (client_challenge, min(now + 30, self._deadline))
            return {'extension_id': self._extension_id, 'client_challenge': client_challenge,
                    'server_challenge': server_challenge,
                    'proof': self._proof('identity-pilot-host-1', client_challenge, server_challenge)}

    def resume(self, extension_id, client_challenge, server_challenge, proof):
        self._check_extension(extension_id)
        with self._lock:
            now = self._live()
            if (not isinstance(client_challenge, str) or not NONCE_RE.fullmatch(client_challenge)
                    or not isinstance(server_challenge, str) or not NONCE_RE.fullmatch(server_challenge)):
                raise _error(401, 'device_proof_invalid', 'Local device proof is invalid.')
            # Consume before checking proof, so incorrect proofs and concurrent
            # requests cannot reuse the same server nonce.
            pending = self._pending.pop(server_challenge, None)
            if pending is None:
                raise _error(401, 'device_proof_invalid', 'Local device proof is invalid or already used.')
            if now >= pending[1]:
                raise _error(410, 'device_challenge_expired', 'This local device challenge has expired.')
            if (not hmac.compare_digest(client_challenge, pending[0])
                    or not isinstance(proof, str) or not PROOF_RE.fullmatch(proof)
                    or not hmac.compare_digest(proof, self._proof('identity-pilot-client-1', client_challenge, server_challenge))):
                raise _error(401, 'device_proof_invalid', 'Local device proof is invalid.')
            return self._login()
