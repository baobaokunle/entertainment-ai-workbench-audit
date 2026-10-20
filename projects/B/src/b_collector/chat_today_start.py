"""Unwired, independently pinned new-day contract and atomic local start.

No approval issuer, file discovery, browser calls, history requests or media
downloads. A trusted future caller must supply reviewed byte digests and an
actual native owner/document attestation. Fictional packets are not authority.
This first candidate deliberately accepts exactly one stopped inherited run;
additional runs require a separately reviewed complete inheritance design.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import uuid

from .contract import decode, encode, scalar
from .security import CollectorError
from .store import now
from .chat_today_journal import TodayJournal, FORMAT as JOURNAL, digest, require_lock
from .chat_today_window import TodayWindow, BJ, today_targets, _time
from .chat_reviewed_resume import ChatReviewedRecoveryPlan

FORMAT = 'b-chat-today-reviewed-start/2'
NATIVE = 'b-chat-today-owner-attestation/2'
CODE_FILES = (
    'src/b_collector/chat_today_start.py', 'src/b_collector/chat_today_journal.py',
    'src/b_collector/chat_today_window.py', 'src/b_collector/chat_today_materialize.py',
    'src/b_collector/chat_reviewed_resume.py', 'src/b_collector/chat_resume.py',
    'src/b_collector/chat_store.py', 'src/b_collector/store.py',
    'src/b_collector/contract.py', 'src/b_collector/security.py',
    'src/b_collector/host.py',
    'src/b_collector/chat_today_media.py', 'src/b_collector/chat_download.py',
    'src/b_collector/chat_cipher.py', 'collector-extension/chat-media.js',
    'src/b_collector/chat_today_runtime.py',
    'collector-extension/chat-source.js', 'collector-extension/engine.js',
    'collector-extension/core.js', 'collector-extension/page-read.js',
    'collector-extension/sources.js', 'collector-extension/estimate.js',
    'src/b_collector/chat_today_snapshot.py',
)


def reject(code='CHAT_TODAY_REVIEW_INVALID'):
    raise CollectorError(code)


def native_matches(actual, expected):
    """Compare typed JSON values; Python False == 0 is not native status proof."""
    try:
        options = {'sort_keys': True, 'allow_nan': False, 'separators': (',', ':')}
        return json.dumps(actual, **options) == json.dumps(expected, **options)
    except (TypeError, ValueError, RecursionError):
        return False


def keys(value, allowed):
    if not isinstance(value, dict) or set(value) != set(allowed): reject()


def hexadecimal(value, size=64):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{%s}' % size, value): reject()


def packet(data, expected, limit):
    hexadecimal(expected)
    if type(data) is not bytes or not 0 < len(data) <= limit: reject()
    if hashlib.sha256(data).hexdigest() != expected: reject('CHAT_TODAY_REVIEW_DIGEST_CHANGED')
    try: return decode(data)
    except Exception: reject()


def inventory(store, parent):
    """Read ciphertext envelopes, not media binaries, only in the caller's lock.

    Observation IDs/order/content and all old attempt markers are pinned.
    Media-byte integrity comes from the separately reviewed sealed audit;
    this function does not repeat a multi-gigabyte media audit at startup.
    """
    queries = {
        'observations': ('SELECT id,sequence,cipher FROM observations WHERE run_id=? ORDER BY sequence', (parent,)),
        'batches': ('SELECT id,cipher FROM batches WHERE run_id=? ORDER BY id', (parent,)),
        'media_manifests': ('SELECT id,cipher FROM chat_media WHERE run_id=? ORDER BY id', (parent,)),
        'legacy_binding': ('SELECT slot,cipher FROM chat_legacy_binding ORDER BY slot', ()),
    }
    result = {}
    for name, (sql, args) in queries.items():
        h = hashlib.sha256(); count = 0
        for row in store.db.execute(sql, args):
            count += 1
            for item in row:
                data = item if isinstance(item, bytes) else encode(item)
                h.update(len(data).to_bytes(8, 'big')); h.update(data)
        result[name] = {'count': count, 'sha256': h.hexdigest()}
    return result


class ReviewedTodayStart:
    def __init__(self, data, reviewed_sha256, native, native_sha256, clock):
        self._inputs = (data, reviewed_sha256, native, native_sha256)
        self.clock = clock
        self._validate()

    def _validate(self):
        try:
            data, approved_hash, native, native_hash = self._inputs
            c = packet(data, approved_hash, 64 * 1024)
            n = packet(native, native_hash, 8192)
            keys(c, {'format', 'approval_id', 'approved_to_execute', 'reviewed_at', 'valid_until',
                     'parent', 'owner', 'window', 'targets_sha256', 'target_count', 'page_budget', 'entry_kind',
                     'code_sha256', 'native_sha256', 'native_contract_sha256', 'sealed_audit_sha256',
                     'inheritance_run_ids', 'inventory'})
            if c['format'] != FORMAT or c['approved_to_execute'] is not True: reject()
            hexadecimal(c['approval_id'], 32)
            for k in ('targets_sha256', 'native_sha256', 'native_contract_sha256', 'sealed_audit_sha256'):
                hexadecimal(c[k])
            if c['native_sha256'] != native_hash: reject()
            keys(c['code_sha256'], CODE_FILES)
            for h in c['code_sha256'].values(): hexadecimal(h)
            p = c['parent']
            keys(p, {'run_id', 'generation', 'state', 'records', 'meta_sha256', 'prefix_sha256',
                     'scope_sha256', 'stopped_at', 'expires_at', 'unattributed'})
            hexadecimal(p['run_id'], 32); hexadecimal(p['generation'], 32)
            for k in ('meta_sha256', 'prefix_sha256', 'scope_sha256'): hexadecimal(p[k])
            if (p['state'] not in {'failed', 'disconnected'} or type(p['records']) is not int
                    or not 1 <= p['records'] <= 10_000_000
                    or c['inheritance_run_ids'] != [p['run_id']]): reject()
            if not isinstance(p['unattributed'], dict) or len(p['unattributed']) > 10: reject()
            for sequence, code in p['unattributed'].items():
                if (not re.fullmatch('[1-9][0-9]*', sequence) or int(sequence) > p['records']
                        or code not in {'RPC_TIMEOUT', 'CHAT_HISTORY_EMPTY_END_UNVERIFIED'}): reject()
            scalar(c['owner'])
            if c['owner']['type'] != 'string' or c['owner']['lossless'] is not True or not c['owner']['value']: reject()
            if type(c['target_count']) is not int or not 1 <= c['target_count'] <= 10000: reject()
            if type(c['page_budget']) is not int or not 1 <= c['page_budget'] <= 100: reject()
            if c['entry_kind'] not in {'history_only','cache_only'}: reject()
            keys(c['inventory'], {'observations', 'batches', 'media_manifests', 'legacy_binding'})
            for name, item in c['inventory'].items():
                keys(item, {'count', 'sha256'}); hexadecimal(item['sha256'])
                if type(item['count']) is not int or not 0 <= item['count'] < 2 ** 63: reject()
            if c['inventory']['observations']['count'] != p['records']: reject()
            keys(n, {'format', 'approval_id', 'parent_run_id', 'owner', 'observed_at', 'contract_verified',
                     'methods', 'plugins', 'custom_hooks', 'document_sha256', 'native_contract_sha256'})
            if (n['format'] != NATIVE or n['approval_id'] != c['approval_id']
                    or n['parent_run_id'] != p['run_id'] or n['owner'] != c['owner']
                    or n['native_contract_sha256'] != c['native_contract_sha256']
                    or n['contract_verified'] is not True
                    or type(n['methods']) is not int or n['methods'] != 17
                    or type(n['plugins']) is not int or n['plugins'] != 8
                    or n['custom_hooks'] is not False): reject('CHAT_TODAY_NATIVE_ATTESTATION')
            hexadecimal(n['document_sha256'])
            current = self.clock()
            if not isinstance(current, datetime) or current.tzinfo is None: reject()
            observed, reviewed, until = map(_time, (n['observed_at'], c['reviewed_at'], c['valid_until']))
            if not _time(p['stopped_at']) < observed <= reviewed <= current < until <= _time(p['expires_at']):
                reject('CHAT_TODAY_REVIEW_EXPIRED')
            if (current - observed).total_seconds() > 300: reject('CHAT_TODAY_NATIVE_ATTESTATION_EXPIRED')
            window = TodayWindow.parse(c['window'], now=current, parent_expiry=p['expires_at'])
            if window.approved_at != reviewed or until > window.expires: reject()
            return c, n, window
        except CollectorError: raise
        except (KeyError, ValueError, TypeError, OverflowError, RecursionError): reject()

    def _check(self, store, lock, verify_code, verify_native, new_run=None):
        c, n, window = self._validate()
        require_lock(store, lock); store._guard()
        if not store.db.in_transaction: reject('CHAT_TODAY_TRANSACTION_REQUIRED')
        if verify_code() != c['code_sha256']: reject('VERSION_MISMATCH')
        # The trusted caller must revalidate its retained session/document
        # fence. Returning the input packet without an actual check is not a
        # production integration. No network request is made by this module.
        if not native_matches(verify_native(), n): reject('CHAT_TODAY_NATIVE_ATTESTATION_CHANGED')
        self._validate()  # Either callback could block past the deadline.
        expected_runs = {c['parent']['run_id']} | ({new_run} if new_run else set())
        if {r[0] for r in store.db.execute('SELECT id FROM runs')} != expected_runs:
            reject('CHAT_TODAY_INHERITANCE_SET_CHANGED')
        if store.db.execute("SELECT 1 FROM runs WHERE state='running' AND id<>?", (new_run or '',)).fetchone():
            reject('TASK_ALREADY_RUNNING')
        p = c['parent']; row, meta = store._run(p['run_id'])
        if (row[0], row[1], row[3], hashlib.sha256(row[2]).hexdigest()) != (
                p['generation'], p['state'], p['records'], p['meta_sha256']):
            reject('CHAT_TODAY_PARENT_CHANGED')
        if (meta.get('run_id') != p['run_id'] or meta.get('chat_resume_owner') != c['owner']
                or digest(meta['scope']) != p['scope_sha256']
                or meta.get('finished_at') != p['stopped_at'] or meta['expires_at'] != p['expires_at']):
            reject('CHAT_TODAY_PARENT_CHANGED')
        return c, n, window, row, meta

    def start(self, store, lock, *, verify_code, verify_native, prepared_snapshot=None):
        """All-or-nothing local run + target directory + one-use receipt.

        No source request is issued. A lost return must be resolved by reading
        the encrypted receipt, never by making a second run/activation. The
        receipt survives deletion of its child as a minimal consumed marker.
        """
        with store.transaction():
            c, n, window, row, meta = self._check(store, lock, verify_code, verify_native)
            store.db.execute('''CREATE TABLE IF NOT EXISTS chat_today_starts(
                approval_id TEXT PRIMARY KEY,child_id TEXT,
                parent_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                cipher BLOB NOT NULL,
                FOREIGN KEY(child_id) REFERENCES runs(id) ON DELETE SET NULL)''')
            if store.db.execute('SELECT 1 FROM chat_today_starts WHERE approval_id=?', (c['approval_id'],)).fetchone():
                reject('CHAT_TODAY_APPROVAL_CONSUMED')
            actual_inventory = inventory(store, c['parent']['run_id'])
            if actual_inventory != c['inventory']:
                reject('CHAT_TODAY_INVENTORY_CHANGED')
            p = c['parent']
            if prepared_snapshot is not None:
                from .chat_today_snapshot import TodayPreparedSnapshot
                if type(prepared_snapshot) is not TodayPreparedSnapshot: reject('CHAT_TODAY_SNAPSHOT_REQUIRED')
                targets,evidence=prepared_snapshot.claim_start(store,lock,c,actual_inventory)
            else:
                basis = {'run_id': p['run_id'], 'prefix_count': p['records'], 'prefix_sha256': p['prefix_sha256'],
                         'scope_sha256': p['scope_sha256'], 'expires_at': p['expires_at'], 'unattributed': p['unattributed']}
                plan = ChatReviewedRecoveryPlan(p['run_id'], row, meta, basis)
                for sequence, cipher in store.db.execute(
                        'SELECT sequence,cipher FROM observations WHERE run_id=? ORDER BY sequence', (p['run_id'],)):
                    plan.observe_cipher(sequence, cipher); plan.feed(store._decrypt(cipher), sequence)
                targets, evidence = today_targets(plan)
                if plan.owner != c['owner']: reject('CHAT_TODAY_TARGETS_CHANGED')
            if (len(targets) != c['target_count'] or digest(targets) != c['targets_sha256']):
                reject('CHAT_TODAY_TARGETS_CHANGED')
            self._check(store, lock, verify_code, verify_native)
            run, generation = uuid.uuid4().hex, uuid.uuid4().hex
            scope = {'domains': ['chat', 'account', 'capability'], 'include_body': True,
                     'start_date': window.start.astimezone(BJ).date().isoformat(),
                     'end_date': window.start.astimezone(BJ).date().isoformat(),
                     'retention_days': max(1, math.ceil((window.expires-window.approved_at).total_seconds()/86400))}
            # Derive the label from the review's duration; the fixed absolute
            # expiry below is authoritative and is never renewed at startup.
            policy = {'format': JOURNAL, 'window': deepcopy(c['window']), 'owner': deepcopy(c['owner']),
                      'targets_sha256': c['targets_sha256'], 'page_budget': c['page_budget'], 'entry_kind': c['entry_kind'],
                      'parent': {k: p[k] for k in ('run_id', 'generation', 'state', 'records', 'meta_sha256')}}
            child_meta = {'format': 'b-capture-run/1', 'run_id': run, 'scope': scope, 'started_at': now(),
                          'finished_at': None, 'stop_reason': None, 'expires_at': c['window']['expires_at'],
                          'formal_A_acceptance': False, 'chat_resume_owner': deepcopy(c['owner']),
                          'chat_today': policy,
                          'chat_today_start': {'format': FORMAT, 'approval_id': c['approval_id'],
                                               'review_sha256': self._inputs[1], 'native_sha256': self._inputs[3],
                                               'sealed_audit_sha256': c['sealed_audit_sha256'],
                                               'inheritance_run_ids': c['inheritance_run_ids'], 'evidence': evidence}}
            cipher = store._encrypt(child_meta)
            store.db.execute('INSERT INTO runs VALUES(?,?,?,?,0)', (run, generation, 'running', cipher))
            pin = hashlib.sha256(cipher).hexdigest()
            journal = TodayJournal(store, lock, run, generation, pin)
            target_ids = journal._install_in_transaction(targets)
            receipt = {'format': FORMAT, 'approval_id': c['approval_id'], 'run_id': run, 'generation': generation,
                       'review_sha256': self._inputs[1], 'native_sha256': self._inputs[3],
                       'meta_sha256': pin, 'target_ids': target_ids, 'source_requests': 0,
                       'window_complete': False, 'media_complete': False}
            store.db.execute('INSERT INTO chat_today_starts VALUES(?,?,?,?)',
                             (c['approval_id'], run, p['run_id'], store._encrypt(receipt)))
            self._check(store, lock, verify_code, verify_native, new_run=run)
            journal._check()
            if prepared_snapshot is not None: prepared_snapshot.bind_created(receipt)
        return receipt


def load_today_start(data, *, reviewed_sha256, native_attestation, reviewed_native_sha256, clock=None):
    return ReviewedTodayStart(data, reviewed_sha256, native_attestation, reviewed_native_sha256,
                              clock or (lambda: datetime.now(timezone.utc)))
