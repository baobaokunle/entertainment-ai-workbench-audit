"""Offline candidate: atomic preparation of a reviewed stopped gap queue.

There is deliberately no dispatcher/CLI integration or approval-file loader.
Preparation leaves the run stopped. It does not verify a browser connection or
authorize source reads, and must not be followed by the frozen resume method.
All decoded tasks and receipts remain in memory or the protected ciphertext DB.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import uuid

from .contract import encode
from .gaps import CODE_HASH, FROZEN, queue_class
from .guarded_gap_successors import derive_guarded_successors, moment
from .host import ExclusiveHost
from .security import CollectorError
from .store import now, uid

FORMAT = 'b-gap-guarded-preparation/1'
CANDIDATE_FILES = FROZEN + ('src/b_collector/guarded_gap_prepare.py',
    'src/b_collector/guarded_gap_contracts.py',
    'src/b_collector/guarded_gap_successors.py', 'src/b_collector/gaps.py',
    'src/b_collector/store.py', 'src/b_collector/host.py',
    'src/b_collector/contract.py', 'src/b_collector/security.py')


def require_lock(store, lock):
    if (not isinstance(lock, ExclusiveHost) or not lock.handle or lock.handle.closed
            or Path(lock.handle.name).resolve() != (store.root / 'host.lock').resolve()):
        raise CollectorError('HOST_LOCK_REQUIRED')


def digest_rows(rows):
    """Length framed values, including raw ciphertext; no decryption."""
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        count += 1
        for value in row:
            data = value if isinstance(value, bytes) else encode(value)
            digest.update(len(data).to_bytes(8, 'big'))
            digest.update(data)
    return {'count': count, 'sha256': digest.hexdigest()}


def checkpoint(store, run):
    """Caller must hold a transaction and exclusive host lock."""
    if not store.db.in_transaction:
        raise CollectorError('GUARDED_TRANSACTION_REQUIRED')
    row = store.db.execute('SELECT generation,state,cipher,count FROM runs WHERE id=?', (uid(run),)).fetchone()
    if not row:
        raise CollectorError('RUN_NOT_FOUND')
    result = {'run_id': run, 'generation': row[0], 'state': row[1], 'records': row[3],
              'meta_sha256': hashlib.sha256(row[2]).hexdigest()}
    for name, sql in (
        ('targets', 'SELECT rowid,id,cipher FROM gap_targets WHERE run_id=? ORDER BY rowid'),
        ('operations', 'SELECT rowid,id,target,phase,state,cipher FROM gap_operations WHERE run_id=? ORDER BY rowid'),
        ('observations', 'SELECT id,sequence,cipher FROM observations WHERE run_id=? ORDER BY sequence'),
    ):
        result[name] = digest_rows(store.db.execute(sql, (run,)))
    if result['observations']['count'] != row[3]:
        raise CollectorError('REPAIR_RECORD_MISMATCH')
    return result


def _position(row):
    op = row['operation']
    if op['domain'] == 'profile':
        return op['user_offset']
    if op['domain'] in {'speech', 'comment', 'gift'}:
        return moment(op['task']['start'])
    return None


def future_chain_guard(operations):
    """Frozen lazy commit must never enqueue an already recorded later task.

    A normal pending seed has no previous attempt. Any existing later task in
    that chain makes using frozen lazy continuation unproved, so reject the
    entire preparation rather than hiding the conflict or skipping a failure.
    """
    chains = {}
    for row in operations:
        chains.setdefault((row['target'], row['operation']['domain']), []).append(row)
    for row in operations:
        if row['state'] != 'pending':
            continue
        op = row['operation']
        if any(k in op for k in ('attempt', 'first_sequence', 'last_sequence', 'committed_count')):
            raise CollectorError('GUARDED_PENDING_ATTEMPT')
        position = _position(row)
        if position is None:
            continue
        for peer in chains[(row['target'], op['domain'])]:
            if peer is row:
                continue
            # Only a valid terminal no_targets seed has no source request.
            if peer['state'] == 'no_targets':
                raise CollectorError('GUARDED_FUTURE_CHAIN_UNPROVEN')
            if _position(peer) >= position:
                raise CollectorError('GUARDED_FUTURE_CHAIN_UNPROVEN')


def prepare_reviewed_stop(store, lock, approval, *, verify_code, validate_review=None):
    """Consume an exact reviewed preparation contract atomically; never start.

    A future trusted host must supply verified review provenance and source-code
    hashes. There is no issuer of real approved contracts in this candidate.
    The existing product rejects the candidate client before reaching this code.
    """
    required = {'format', 'approval_id', 'approved_to_prepare', 'checkpoint', 'code_sha256', 'frozen_code_hash'}
    if (not isinstance(approval, dict) or set(approval) != required or approval['format'] != FORMAT
            or approval['approved_to_prepare'] is not True or approval['frozen_code_hash'] != CODE_HASH):
        raise CollectorError('GUARDED_PREPARATION_NOT_APPROVED')
    contract = deepcopy(approval)
    hashes = contract['code_sha256']
    if (not isinstance(hashes, dict) or set(hashes) != set(CANDIDATE_FILES)
            or any(not isinstance(v, str) or re.fullmatch('[0-9a-f]{64}', v) is None for v in hashes.values())):
        raise CollectorError('VERSION_MISMATCH')
    approval_id = uid(contract['approval_id'])
    run = uid(contract['checkpoint']['run_id'])
    require_lock(store, lock)
    store._guard()
    with store.transaction():
        require_lock(store, lock)
        if not contract['code_sha256'] or verify_code() != contract['code_sha256']:
            raise CollectorError('VERSION_MISMATCH')
        if checkpoint(store, run) != contract['checkpoint']:
            raise CollectorError('GUARDED_STOP_CHANGED')
        if store.db.execute("SELECT 1 FROM runs WHERE state='running'").fetchone():
            raise CollectorError('TASK_ALREADY_RUNNING')
        row, meta = store._run(run)
        if row[1] not in {'failed', 'disconnected'}:
            raise CollectorError('GUARDED_STOP_STATE')
        if validate_review is not None:
            validate_review(deepcopy(meta))
        gap = meta.get('gap_repair', {})
        if (gap.get('format') != 'b-backend-gap/1' or gap.get('code_hash') != CODE_HASH
                or {'chat', 'legacy_chat'} & set(meta['scope']['domains'])):
            raise CollectorError('GAP_NOT_REGISTERED')
        # Bypass the frozen constructor (DDL), never its resume method.
        queue_type = queue_class()
        queue = queue_type.__new__(queue_type)
        queue.s, queue.db = store, store.db
        queue.parents(meta)
        store.db.execute('''CREATE TABLE IF NOT EXISTS guarded_gap_preparations(
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, approval_id TEXT NOT NULL,
            cipher BLOB NOT NULL, PRIMARY KEY(run_id,approval_id))''')
        # A distinct later contract must explicitly handle an earlier preparation;
        # this first candidate cannot pile preparations on a stopped run.
        if store.db.execute('SELECT 1 FROM guarded_gap_preparations WHERE run_id=?', (run,)).fetchone():
            raise CollectorError('GUARDED_PREPARATION_ALREADY_EXISTS')
        targets = {key: store._decrypt(cipher) for key, cipher in store.db.execute(
            'SELECT id,cipher FROM gap_targets WHERE run_id=?', (run,))}
        operations = [{'id': key, 'target': target, 'phase': phase, 'state': state,
                       'operation': store._decrypt(cipher)}
                      for key, target, phase, state, cipher in store.db.execute(
                          'SELECT id,target,phase,state,cipher FROM gap_operations WHERE run_id=? ORDER BY rowid', (run,))]
        plan = derive_guarded_successors(meta['scope'], targets, operations)
        added = []
        for successor in plan['successors']:
            op = successor['operation']
            key_parts = [run, successor['target'], op['domain'],
                         op['user_offset'] if op['domain'] == 'profile' else [op['task']['start'], op['task']['end']]]
            key = hashlib.sha256(encode(key_parts)).hexdigest()[:32]
            item = {'id': key, 'target': successor['target'], 'phase': successor['phase'],
                    'state': 'pending', 'operation': op}
            operations.append(item)
            added.append({**successor, 'created_operation': key})
        future_chain_guard(operations)
        for item in operations[-len(added):] if added else []:
            store.db.execute('INSERT INTO gap_operations(id,run_id,target,phase,state,cipher) VALUES(?,?,?,?,?,?)',
                             (item['id'], run, item['target'], item['phase'], 'pending', store._encrypt(item['operation'])))
        new_generation = uuid.uuid4().hex
        receipt = {'format': FORMAT, 'approval_id': approval_id, 'prepared_at': now(),
                   'before': contract['checkpoint'], 'code_sha256': contract['code_sha256'],
                   'old_state': row[1], 'generation': new_generation,
                   'successors': added, 'unresolved_reading': plan['unresolved_reading'],
                   'deferred_chains': plan['deferred_chains'], 'original_scope_complete': False,
                   'source_execution_authorized': False}
        store.db.execute('INSERT INTO guarded_gap_preparations VALUES(?,?,?)', (run, approval_id, store._encrypt(receipt)))
        # Keep old finish time, stop reason, metadata ciphertext and count intact.
        store.db.execute('UPDATE runs SET generation=? WHERE id=?', (new_generation, run))
        # Expiry, parent and lock checks also cover time spent planning/writing.
        require_lock(store, lock)
        queue.parents(meta)
        if verify_code() != contract['code_sha256']:
            raise CollectorError('VERSION_MISMATCH')
        if validate_review is not None:
            validate_review(deepcopy(meta))
    return {'state': row[1], 'records': row[3], 'successors_prepared': len(added),
            'unresolved_reading': len(plan['unresolved_reading']),
            'source_execution_authorized': False, 'original_scope_complete': False}
