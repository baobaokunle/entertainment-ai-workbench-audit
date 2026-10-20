"""Isolated deletion-intent log prototype; never enabled by application startup.

The log is independent of the business database. It contains authenticated,
body-free target selectors, not platform credentials or message text. A caller
must supply an independently retained expected head. This prototype neither
deletes data nor proves the latest head when all checkpoints have been lost.
"""
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import threading

from .auth import Principal
from .common import AppError, canonical_json, utc_now
from .security import SecureEnvelope

FORMAT = 'dfa-deletion-intent-log-1'
MAX_RECORDS = 5000
MAX_RECORD_BYTES = 1048576
MAX_LOG_BYTES = 16 * 1048576


def _text(value, limit=200):
    return isinstance(value, str) and 0 < len(value) <= limit and '\x00' not in value


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(x in '0123456789abcdef' for x in value)


def _owned_path(path):
    raw = Path(path)
    if not raw.is_absolute():
        raise AppError(422, 'DELETION_LOG_PATH', 'An explicit absolute fixture log path is required.')
    for candidate in (raw, *raw.parents):
        if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)():
            raise AppError(422, 'DELETION_LOG_PATH', 'Deletion log paths cannot traverse links.')
    if raw.exists() and (not raw.is_file() or raw.stat().st_nlink != 1):
        raise AppError(422, 'DELETION_LOG_PATH', 'Deletion log must be one owned file without hard links.')
    return raw.resolve()


def validate_intent(intent):
    fields = {'intent_id', 'account_key', 'device_key', 'operator_key', 'plan_hash',
              'targets', 'alias_selectors', 'event_refs', 'ai_job_keys'}
    if not isinstance(intent, dict) or set(intent) != fields:
        raise AppError(422, 'DELETION_INTENT_INVALID', 'Only the finite body-free intent projection is accepted.')
    if any(not _text(intent[k]) for k in ('intent_id', 'account_key', 'device_key', 'operator_key')) or not _hash(intent['plan_hash']):
        raise AppError(422, 'DELETION_INTENT_INVALID', 'Intent identity and complete plan digest are required.')
    collections = ('targets', 'alias_selectors', 'event_refs', 'ai_job_keys')
    if any(not isinstance(intent[k], list) for k in collections) or not intent['targets'] or sum(len(intent[k]) for k in collections) > MAX_RECORDS:
        raise AppError(413, 'DELETION_INTENT_LIMIT', 'A complete bounded target closure is required; no truncation.')
    account = intent['account_key']
    seen = set()
    for target in intent['targets']:
        if (not isinstance(target, dict) or set(target) != {'account_key', 'target_kind', 'target_key', 'version_digest'}
                or target['account_key'] != account or target['target_kind'] not in ('message', 'observation')
                or not _text(target['target_key']) or not _hash(target['version_digest'])):
            raise AppError(422, 'DELETION_INTENT_INVALID', 'Targets must bind one account and exact version digests.')
        signature = (target['target_kind'], target['target_key'])
        if signature in seen: raise AppError(422, 'DELETION_INTENT_INVALID', 'Target closure cannot contain duplicates.')
        seen.add(signature)
    for selector in intent['alias_selectors']:
        if not isinstance(selector, dict): raise AppError(422, 'DELETION_INTENT_INVALID', 'Finite alias selector required.')
        kind = selector.get('kind')
        extra = {'platform_conversation_key', 'platform_message_key'} if kind == 'platform_message' else {'account_namespace', 'source_key', 'source_id', 'source_version'} if kind == 'file_record' else None
        limits = {'platform_conversation_key': 512, 'platform_message_key': 512,
                  'account_namespace': 256, 'source_key': 2048, 'source_id': 200, 'source_version': 80}
        if extra is None or set(selector) != {'kind', 'account_key'} | extra or selector['account_key'] != account or any(not _text(selector[k], limits[k]) for k in extra):
            raise AppError(422, 'DELETION_INTENT_INVALID', 'Aliases must be exact, account-scoped platform or file-record selectors.')
    event_ids = set()
    for reference in intent['event_refs']:
        if (not isinstance(reference, dict) or set(reference) != {'event_id', 'event_digest'}
                or not _text(reference['event_id']) or not _hash(reference['event_digest']) or reference['event_id'] in event_ids):
            raise AppError(422, 'DELETION_INTENT_INVALID', 'Event suppression references bind immutable IDs and digests.')
        event_ids.add(reference['event_id'])
    if any(not _text(key) for key in intent['ai_job_keys']) or len(set(intent['ai_job_keys'])) != len(intent['ai_job_keys']):
        raise AppError(422, 'DELETION_INTENT_INVALID', 'Annotation references must be bounded unique keys.')
    if len(canonical_json(intent)) > MAX_RECORD_BYTES:
        raise AppError(413, 'DELETION_INTENT_LIMIT', 'Intent exceeds its byte budget.')
    return intent


class AuthenticatedDeletionLog:
    """Explicit fictional-fixture primitive, with SQLite serialization and CAS.

    No default key generation, recovery enrollment, production registration,
    peer authority, product execute route or background worker is provided.
    """
    def __init__(self, path, *, key, fixture_mode=False):
        if fixture_mode is not True:
            raise AppError(403, 'DELETION_FIXTURE_ONLY', 'Deletion intent writes remain limited to explicit fictional fixtures.')
        if not isinstance(key, bytes) or len(key) != 32:
            raise AppError(422, 'invalid_key', 'An independently supplied 256-bit fixture log key is required.')
        self.path, self.key, self.lock = _owned_path(path), key, threading.RLock()
        if not self.path.exists(): raise AppError(503, 'DELETION_LOG_MISSING', 'Missing deletion log cannot be replaced by a new empty log.')
        stamp = self.path.stat()
        self.file_identity = (stamp.st_dev, stamp.st_ino)
        self.conn = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('PRAGMA busy_timeout=5000')
        try:
            rows = dict(self.conn.execute('SELECT key,value FROM intent_meta'))
            if set(rows) != {'format', 'log_id', 'key_fingerprint'} or rows['format'] != FORMAT or not _hash(rows['log_id']) or rows['key_fingerprint'] != hashlib.sha256(key).hexdigest():
                raise AppError(422, 'DELETION_LOG_INVALID', 'Log registration or fixture key does not match.')
            self.log_id = rows['log_id']
            self.genesis_head = hashlib.sha256(canonical_json({'format': FORMAT, 'log_id': self.log_id, 'seq': 0})).hexdigest()
        except (sqlite3.Error, AppError):
            self.conn.close()
            raise AppError(422, 'DELETION_LOG_INVALID', 'Authenticated deletion log cannot be opened.') from None

    @classmethod
    def initialize(cls, path, *, key, fixture_mode=False):
        if fixture_mode is not True: raise AppError(403, 'DELETION_FIXTURE_ONLY', 'Only an explicit new fictional log may be initialized.')
        if not isinstance(key, bytes) or len(key) != 32: raise AppError(422, 'invalid_key', 'An explicit 256-bit fixture key is required.')
        path = _owned_path(path)
        if not path.parent.is_dir(): raise AppError(422, 'DELETION_LOG_PATH', 'Fixture log parent must already exist.')
        try:
            with path.open('xb'): pass
        except FileExistsError:
            raise AppError(409, 'DELETION_LOG_EXISTS', 'Initialization cannot overwrite any prior deletion log.') from None
        connection = sqlite3.connect(path)
        try:
            connection.execute('PRAGMA synchronous=FULL')
            connection.executescript('BEGIN IMMEDIATE; CREATE TABLE intent_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE intent_record(seq INTEGER PRIMARY KEY,record_digest TEXT UNIQUE NOT NULL,envelope BLOB NOT NULL);')
            connection.executemany('INSERT INTO intent_meta VALUES(?,?)', [('format', FORMAT), ('log_id', secrets.token_hex(32)), ('key_fingerprint', hashlib.sha256(key).hexdigest())])
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally: connection.close()
        return cls(path, key=key, fixture_mode=True)

    def _read(self, expected_head):
        if not _hash(expected_head): raise AppError(422, 'DELETION_HEAD_REQUIRED', 'An independent exact expected deletion-log head is required.')
        _owned_path(self.path)
        if not self.path.exists(): raise AppError(503, 'DELETION_LOG_MISSING', 'Deletion log disappeared; restore must remain held.')
        stamp = self.path.stat()
        if (stamp.st_dev, stamp.st_ino) != self.file_identity:
            raise AppError(409, 'DELETION_LOG_REPLACED', 'Opened deletion log was replaced; do not use a cached chain.')
        metadata = dict(self.conn.execute('SELECT key,value FROM intent_meta'))
        if metadata != {'format': FORMAT, 'log_id': self.log_id, 'key_fingerprint': hashlib.sha256(self.key).hexdigest()}:
            raise AppError(422, 'DELETION_LOG_INVALID', 'Persistent log registration changed during this session.')
        if self.path.stat().st_size > 64 * 1048576: raise AppError(413, 'DELETION_LOG_LIMIT', 'Fixture log file exceeds its finite budget.')
        count, byte_count, largest = self.conn.execute('SELECT count(*),coalesce(sum(length(envelope)),0),coalesce(max(length(envelope)),0) FROM intent_record').fetchone()
        if count > MAX_RECORDS or byte_count > MAX_LOG_BYTES or largest > MAX_RECORD_BYTES + 128:
            raise AppError(413, 'DELETION_LOG_LIMIT', 'Complete fixture log exceeds its finite budget.')
        rows = self.conn.execute('SELECT seq,record_digest,envelope FROM intent_record ORDER BY seq LIMIT ?', (MAX_RECORDS + 1,)).fetchall()
        previous, records, ids = self.genesis_head, [], set()
        for index, row in enumerate(rows, 1):
            if row['seq'] != index: raise AppError(422, 'DELETION_LOG_CHAIN', 'Deletion log sequence is incomplete.')
            aad = canonical_json([FORMAT, self.log_id, index])
            try:
                raw = SecureEnvelope.open(bytes(row['envelope']), self.key, aad=aad)
                record = json.loads(raw)
                if (len(raw) > MAX_RECORD_BYTES or canonical_json(record) != raw or not isinstance(record, dict)
                        or set(record) != {'format', 'seq', 'previous_digest', 'created_at_utc', 'intent'}
                        or record['format'] != FORMAT or record['seq'] != index or record['previous_digest'] != previous
                        or not _text(record['created_at_utc'], 80) or hashlib.sha256(raw).hexdigest() != row['record_digest']):
                    raise ValueError()
                validate_intent(record['intent'])
                if record['intent']['intent_id'] in ids: raise ValueError()
                ids.add(record['intent']['intent_id'])
            except (AppError, ValueError, TypeError, UnicodeError, RecursionError):
                raise AppError(422, 'DELETION_LOG_CHAIN', 'Deletion log authentication or complete chain validation failed.') from None
            previous = row['record_digest']; records.append(record)
        if previous != expected_head:
            raise AppError(409, 'DELETION_HEAD_MISMATCH', 'Log differs from the independent checkpoint; do not open restored content.')
        return records

    def read(self, *, expected_head):
        with self.lock:
            self.conn.execute('BEGIN')
            try: return self._read(expected_head)
            finally: self.conn.execute('ROLLBACK')

    def append(self, intent, principal, *, expected_head, local_device_key):
        validate_intent(intent)
        if not isinstance(principal, Principal) or principal.role != 'manager':
            raise AppError(403, 'DELETION_MANAGER_REQUIRED', 'Only the explicit fixture manager can persist deletion intent.')
        principal.require('deletion:fixture:execute', intent['account_key'])
        if (principal.device_key != local_device_key or intent['device_key'] != local_device_key or intent['operator_key'] != principal.operator_key):
            raise AppError(403, 'DELETION_AUTHORITY_MISMATCH', 'Intent must bind the configured local fixture manager.')
        with self.lock:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                records = self._read(expected_head)
                for record in records:
                    if record['intent']['intent_id'] == intent['intent_id']:
                        if record['intent'] != intent: raise AppError(409, 'DELETION_INTENT_REUSED', 'Intent ID already binds a different target closure.')
                        self.conn.execute('COMMIT')
                        return {'head': expected_head, 'seq': len(records), 'status': 'duplicate', 'intent_id': intent['intent_id']}
                record = {'format': FORMAT, 'seq': len(records) + 1, 'previous_digest': expected_head, 'created_at_utc': utc_now(), 'intent': intent}
                raw = canonical_json(record)
                if len(records) >= MAX_RECORDS or len(raw) > MAX_RECORD_BYTES:
                    raise AppError(413, 'DELETION_LOG_LIMIT', 'Log or intent exceeds its finite budget.')
                sealed = SecureEnvelope.seal(raw, self.key, aad=canonical_json([FORMAT, self.log_id, record['seq']]))
                if self.conn.execute('SELECT coalesce(sum(length(envelope)),0) FROM intent_record').fetchone()[0] + len(sealed) > MAX_LOG_BYTES:
                    raise AppError(413, 'DELETION_LOG_LIMIT', 'Complete log byte budget would be exceeded.')
                head = hashlib.sha256(raw).hexdigest()
                self.conn.execute('INSERT INTO intent_record VALUES(?,?,?)', (record['seq'], head, sealed))
                self.conn.execute('COMMIT')
                return {'head': head, 'seq': record['seq'], 'status': 'intent_durable', 'intent_id': intent['intent_id'],
                        'deletion_executed': False, 'copies_state': 'copies_pending', 'physical_purged': False}
            except BaseException:
                if self.conn.in_transaction: self.conn.execute('ROLLBACK')
                raise

    def close(self):
        with self.lock: self.conn.close()


def record_fixture_intent(db, log, account_key, target_kind, target_key, principal, *,
                          expected_plan_hash, expected_log_head, fixture_mode=False):
    """Bind a fresh D1 manifest to durable intent; do not apply a tombstone.

    The business database is held in its own transaction during the plan CAS.
    The independently committed log survives a subsequent business rollback.
    Actual suppression, copy retirement and restore activation are separate,
    unfinished stages, so this function never claims execution is available.
    """
    if fixture_mode is not True or not isinstance(log, AuthenticatedDeletionLog):
        raise AppError(403, 'DELETION_FIXTURE_ONLY', 'Only explicit fictional plan-to-intent verification is available.')
    if not _hash(expected_plan_hash):
        raise AppError(422, 'DELETION_PLAN_REQUIRED', 'A complete displayed plan digest is required.')
    if not isinstance(principal, Principal) or principal.role != 'manager':
        raise AppError(403, 'DELETION_MANAGER_REQUIRED', 'Only the registered local fixture manager may persist intent.')
    principal.require('deletion:fixture:execute', account_key)
    from .deletion_manifest import DeletionManifestService
    from .db import stable_key
    with db.transaction():
        manifest = DeletionManifestService(db).plan(account_key, target_kind, target_key, principal)
        if not hmac.compare_digest(manifest['plan_hash'], expected_plan_hash):
            raise AppError(409, 'DELETION_PLAN_CHANGED', 'Target closure changed; display a fresh complete plan first.')
        local_device = manifest['metadata']['device_key']
        events = {}
        for reference in manifest['event_refs']:
            prior = events.setdefault(reference['event_id'], reference['event_digest'])
            if prior != reference['event_digest']:
                raise AppError(409, 'DELETION_EVENT_CONFLICT', 'An immutable event ID has inconsistent registered digests.')
        intent = {'intent_id': stable_key('deletion_intent', account_key, local_device, expected_plan_hash),
            'account_key': account_key, 'device_key': local_device, 'operator_key': principal.operator_key,
            'plan_hash': expected_plan_hash, 'targets': manifest['targets'],
            'alias_selectors': manifest['alias_selectors'],
            'event_refs': [{'event_id': key, 'event_digest': value} for key, value in sorted(events.items())],
            'ai_job_keys': manifest['ai_job_keys']}
        result = log.append(intent, principal, expected_head=expected_log_head, local_device_key=local_device)
    return {**result, 'plan_hash': expected_plan_hash, 'execution_available': False,
            'deletion_executed': False, 'physical_purged': False, 'copies_state': 'copies_pending',
            'business_tombstone_applied': False, 'business_body_removed': False, 'product_entry_enabled': False}
