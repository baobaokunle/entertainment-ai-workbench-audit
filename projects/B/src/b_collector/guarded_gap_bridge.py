"""Unwired candidate host for a separately reviewed prepared backend queue.

No production dispatcher, runner, approval issuer or browser imports this module.
Tests inject fictional contracts; a trusted loader remains required for any real
execution. Never call the frozen resume method, which would retry old reading.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import uuid

from .gaps import CODE_HASH, queue_class, verify_code as verify_frozen
from .contract import keys, UUID
from .guarded_gap_prepare import CANDIDATE_FILES as PREPARE_FILES, checkpoint, require_lock
from .security import CollectorError
from .store import now, uid

CLIENT = 'b-gap-guarded-client/1'
FORMAT = 'b-gap-guarded-open/1'
CODE_FILES = PREPARE_FILES + ('src/b_collector/guarded_gap_bridge.py',
    'tools/backend_guarded_gap_client.mjs', 'tools/backend_global_stop_policy.mjs')
SOURCE_ROOT = Path(__file__).resolve().parents[2]


class GuardedGapBridge:
    def __init__(self, store, lock, approval, *, verify_code, validate_review=None):
        self.s, self.lock = store, lock
        self.approval = deepcopy(approval)
        self.verify_code = verify_code
        self.validate_review = validate_review
        self.queue = self.owned = self.current = self.preflight = None
        self.account_ok = self.blocked = self.used = False

    def _code(self):
        hashes = self.approval['code_sha256']
        if (not isinstance(hashes, dict) or set(hashes) != set(CODE_FILES)
                or any(not isinstance(v, str) or re.fullmatch('[0-9a-f]{64}', v) is None for v in hashes.values())
                or self.verify_code() != hashes):
            raise CollectorError('VERSION_MISMATCH')
        verify_frozen(SOURCE_ROOT)

    def open(self, run, client):
        if client != CLIENT:
            raise CollectorError('VERSION_MISMATCH')
        required = {'format', 'approved_to_execute', 'approval_id', 'preparation_id',
                    'preparation_sha256', 'checkpoint', 'code_sha256', 'external_change_evidence_sha256'}
        a = self.approval
        if (not isinstance(a, dict) or set(a) != required or a['format'] != FORMAT
                or a['approved_to_execute'] is not True):
            raise CollectorError('GUARDED_EXECUTION_NOT_APPROVED')
        if self.used or self.owned:
            raise CollectorError('GUARDED_EXECUTION_ALREADY_USED')
        if uid(run) != a['checkpoint']['run_id']:
            raise CollectorError('REPAIR_SCOPE')
        for field in ('preparation_sha256', 'external_change_evidence_sha256'):
            if not isinstance(a[field], str) or re.fullmatch('[0-9a-f]{64}', a[field]) is None:
                raise CollectorError('GUARDED_EXECUTION_NOT_APPROVED')
        uid(a['approval_id']); uid(a['preparation_id'])
        require_lock(self.s, self.lock)
        queue_type = queue_class()
        queue = queue_type.__new__(queue_type)
        queue.s, queue.db, queue.stage = self.s, self.s.db, {}
        with self.s.transaction():
            self._code()
            if checkpoint(self.s, run) != a['checkpoint']:
                raise CollectorError('GUARDED_STOP_CHANGED')
            if self.s.db.execute("SELECT 1 FROM runs WHERE state='running'").fetchone():
                raise CollectorError('TASK_ALREADY_RUNNING')
            row, meta = self.s._run(run)
            if row[1] not in {'disconnected', 'failed'} or meta['gap_repair']['code_hash'] != CODE_HASH:
                raise CollectorError('GUARDED_STOP_STATE')
            # Bind the reviewed stop before clearing finished_at for activation.
            stopped_meta = deepcopy(meta)
            if self.validate_review is not None:
                self.validate_review(deepcopy(stopped_meta))
            queue.parents(meta)
            saved = self.s.db.execute('SELECT cipher FROM guarded_gap_preparations WHERE run_id=? AND approval_id=?',
                                      (run, a['preparation_id'])).fetchone()
            if not saved or hashlib.sha256(saved[0]).hexdigest() != a['preparation_sha256']:
                raise CollectorError('GUARDED_PREPARATION_CHANGED')
            receipt = self.s._decrypt(saved[0])
            if (receipt['generation'] != row[0] or receipt['before']['meta_sha256'] != hashlib.sha256(row[2]).hexdigest()
                    or receipt['original_scope_complete'] is not False
                    or receipt['source_execution_authorized'] is not False):
                raise CollectorError('GUARDED_PREPARATION_CHANGED')
            self.s.db.execute('''CREATE TABLE IF NOT EXISTS guarded_gap_activations(
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                preparation_id TEXT NOT NULL, cipher BLOB NOT NULL, PRIMARY KEY(run_id,preparation_id))''')
            if self.s.db.execute('SELECT 1 FROM guarded_gap_activations WHERE run_id=?', (run,)).fetchone():
                raise CollectorError('GUARDED_EXECUTION_ALREADY_USED')
            if not self.s.db.execute("SELECT 1 FROM gap_operations WHERE run_id=? AND state='pending'", (run,)).fetchone():
                raise CollectorError('GAP_NO_UNATTEMPTED')
            account_pending = []
            for key, target, phase, cipher in self.s.db.execute(
                    "SELECT id,target,phase,cipher FROM gap_operations WHERE run_id=? AND state='pending'", (run,)):
                op = self.s._decrypt(cipher)
                if any(k in op for k in ('attempt', 'committed_count', 'first_sequence', 'last_sequence')):
                    raise CollectorError('GUARDED_PENDING_ATTEMPT')
                if op['domain'] == 'account':
                    if target is not None or phase != -1 or op != {'domain': 'account', 'task': {'op': 'account'}, 'context': {}}:
                        raise CollectorError('ACCOUNT_CHECK_REQUIRED')
                    account_pending.append(key)
            if len(account_pending) > 1 or 'account' not in meta['scope']['domains']:
                raise CollectorError('ACCOUNT_CHECK_REQUIRED')
            preflight = account_pending[0] if account_pending else queue.enqueue(
                run, None, -1, {'domain': 'account', 'task': {'op': 'account'}, 'context': {}})
            generation = uuid.uuid4().hex
            activation = {'format': FORMAT, 'approval_id': a['approval_id'], 'prepared_snapshot': a['checkpoint'],
                          'preparation_sha256': a['preparation_sha256'], 'code_sha256': a['code_sha256'],
                          'external_change_evidence_sha256': a['external_change_evidence_sha256'],
                          'generation': generation, 'preflight': preflight, 'started_at': now(),
                          'unresolved_reading': receipt['unresolved_reading'], 'original_scope_complete': False}
            self.s.db.execute('INSERT INTO guarded_gap_activations VALUES(?,?,?)',
                              (run, a['preparation_id'], self.s._encrypt(activation)))
            meta['gap_repair'].setdefault('client_resumes', []).append({
                'client': CLIENT, 'at': now(), 'approval_id': a['approval_id'], 'preparation_id': a['preparation_id']})
            meta.update(finished_at=None, stop_reason=None)
            self.s.db.execute("UPDATE runs SET generation=?,state='running',cipher=? WHERE id=?",
                              (generation, self.s._encrypt(meta), run))
            require_lock(self.s, self.lock)
            queue.parents(meta)
            self._code()
            if self.validate_review is not None:
                self.validate_review(deepcopy(stopped_meta))
        self.used = True
        self.queue, self.preflight = queue, preflight
        self.owned = {'run_id': run, 'generation': generation}
        return {**self.owned, 'owner': meta['gap_repair']['owner'], 'app': meta['gap_repair']['app'],
                'scope': meta['scope'], 'unresolved_reading': len(receipt['unresolved_reading'])}

    def _owned(self, run, generation):
        require_lock(self.s, self.lock)
        if self.owned != {'run_id': run, 'generation': generation}:
            raise CollectorError('TASK_NOT_OWNED')

    def claim(self, run, generation):
        self._owned(run, generation)
        if self.blocked:
            raise CollectorError('BACKEND_ACCESS_BLOCKED')
        if self.current:
            raise CollectorError('REPAIR_CURSOR')
        if not self.account_ok:
            first = self.s.db.execute("SELECT id FROM gap_operations WHERE run_id=? AND state='pending' ORDER BY phase,rowid LIMIT 1", (run,)).fetchone()
            if not first or first[0] != self.preflight:
                self.blocked = True
                raise CollectorError('ACCOUNT_CHECK_REQUIRED')
        try:
            self.current = self.queue.claim(run, generation)
            return self.current
        except BaseException:
            self.blocked = True
            raise

    def _current(self, args):
        if (not self.current or args['op_id'] != self.current['id']
                or args['attempt'] != self.current['attempt']):
            raise CollectorError('REPAIR_CURSOR')

    def part(self, run, generation, **args):
        self._owned(run, generation)
        if self.blocked:
            raise CollectorError('BACKEND_ACCESS_BLOCKED')
        try:
            self._current(args)
            for item in args['items']:
                if item['domain'] == 'capability' and item['fields'].get('status', {}).get('value') in {'SOURCE_CHANGED', 'SOURCE_ORIGIN'}:
                    raise CollectorError('SOURCE_CHANGED')
            return self.queue.part(run, generation, **args)
        except BaseException:
            self.blocked = True
            raise

    def commit(self, run, generation, **args):
        self._owned(run, generation)
        if self.blocked:
            raise CollectorError('BACKEND_ACCESS_BLOCKED')
        try:
            self._current(args)
            domain = self.current['domain']
            result = self.queue.commit(run, generation, **args)
            # All source capability failures stop the entire host, including
            # nested account checks hidden under profile/rank/content domains.
            self.blocked |= result['state'] == 'failed'
            if domain == 'account':
                self.account_ok = result['state'] == 'success'
                self.blocked |= not self.account_ok
            self.current = None
            return {**result, 'must_stop': self.blocked}
        except BaseException:
            self.blocked = True
            raise

    def finish(self, run, generation, reason):
        self._owned(run, generation)
        if reason not in {'completed_requested_scope', 'failed', 'cancelled', 'source_changed', 'disconnected', 'budget'}:
            raise CollectorError('STOP_REASON')
        with self.s.transaction():
            row, meta = self.s._run(run, generation)
            if row[1] != 'running':
                raise CollectorError('RUN_NOT_ACTIVE')
            counts = dict(self.s.db.execute('SELECT state,COUNT(*) FROM gap_operations WHERE run_id=? GROUP BY state', (run,)))
            pending, unknown = counts.get('pending', 0), counts.get('reading', 0)
            effective = reason
            if reason == 'completed_requested_scope':
                if self.blocked or self.current or not self.account_ok or pending:
                    raise CollectorError('GAP_INCOMPLETE')
                if unknown:
                    effective = 'partial_requested_scope'
            state = 'finished' if effective == 'completed_requested_scope' else 'disconnected' if effective == 'partial_requested_scope' else effective
            meta.update(finished_at=now(), stop_reason='disconnected' if effective == 'partial_requested_scope' else effective)
            meta['gap_repair']['guarded_last_result'] = {
                'reason': effective, 'pending_seeds': pending, 'unresolved_reading': unknown,
                'failed_operations': counts.get('failed', 0), 'platform_completeness_verified': False}
            self.s.db.execute('UPDATE runs SET generation=?,state=?,cipher=? WHERE id=?',
                              (uuid.uuid4().hex, state, self.s._encrypt(meta), run))
        self.queue.stage.clear()
        self.owned = self.current = None
        self.account_ok = False
        return {'state': state, 'total': row[3], 'reason': effective,
                'unresolved_reading': unknown, 'pending_seeds': pending,
                'failed_operations': counts.get('failed', 0), 'platform_completeness_verified': False}


class GuardedGapDispatcher:
    """Candidate's narrow protocol, deliberately absent from production serve."""
    def __init__(self, bridge):
        self.bridge = bridge

    def call(self, request):
        rid = request.get('id') if isinstance(request, dict) else None
        rid = rid if isinstance(rid, str) and UUID.fullmatch(rid) else None
        try:
            keys(request, {'protocol', 'id', 'op', 'args'})
            if request['protocol'] != 'b-native/1' or rid is None:
                raise CollectorError('PROTOCOL_VERSION')
            op, args = request['op'], request['args']
            if op == 'gap_open':
                keys(args, {'run_id', 'client'})
                result = self.bridge.open(args['run_id'], args['client'])
            elif op in {'gap_claim', 'gap_part', 'gap_commit', 'gap_finish'}:
                extra = {'gap_claim': set(), 'gap_part': {'op_id', 'attempt', 'offset', 'items'},
                         'gap_commit': {'op_id', 'attempt', 'count'}, 'gap_finish': {'reason'}}[op]
                keys(args, {'run_id', 'generation'} | extra)
                rest = {k: args[k] for k in extra}
                result = getattr(self.bridge, op[4:])(args['run_id'], args['generation'], **rest)
            else:
                raise CollectorError('OP_NOT_ALLOWED')
            return {'id': rid, 'ok': True, 'result': result}
        except CollectorError as error:
            return {'id': rid, 'ok': False, 'error': str(error)}
        except Exception:
            return {'id': rid, 'ok': False, 'error': 'INTERNAL_HOST_ERROR'}

    def disconnect(self):
        if not self.bridge.owned:
            return {'state': 'idle'}
        try:
            return self.bridge.finish(self.bridge.owned['run_id'], self.bridge.owned['generation'], 'disconnected')
        except Exception:
            # Never discard ownership or pretend an unacknowledged finish worked.
            raise CollectorError('GUARDED_CLEANUP_REQUIRED') from None
