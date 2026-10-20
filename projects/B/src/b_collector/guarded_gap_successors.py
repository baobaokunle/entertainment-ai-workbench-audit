"""Pure, offline successor derivation; no store, lock, approval or resume entry.

Inputs must later come from one complete, reviewed, locked stop snapshot. This
function does not establish that precondition and cannot authorize execution.
Returned tasks contain source values and must remain in memory/encrypted storage.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import re

from .contract import encode
from .security import CollectorError

CONTENT = {'speech': ('4', 1800, 1), 'comment': ('2', 60, 3), 'gift': ('1', 60, 3)}
COMMITTED = {'first_sequence', 'last_sequence', 'committed_count'}


def fail():
    raise CollectorError('GUARDED_SUCCESSOR_UNPROVEN')


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or not value.keys() <= set(required) | set(optional):
        fail()


def identifier(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9]{1,40}', value) is None:
        fail()


def fixed_shape(row, target):
    """Reject unproved request parameters rather than copying cursor state."""
    op = row['operation']
    domain = op['domain']
    if row['state'] not in {'reading', 'pending', 'success', 'empty', 'failed', 'no_targets'}:
        fail()
    fields(op, {'domain', 'task', 'context', 'user_offset' if domain == 'profile' else 'step'},
           {'attempt'} | (COMMITTED if row['state'] in {'success', 'empty', 'failed'} else set()))
    fields(op['task'], {'op', 'room'} | ({'contentType', 'start', 'end'} if domain != 'profile' else set()),
           {'owner', 'includeBody'} | ({'user'} if domain == 'profile' else set()))
    fields(op['context'], {'session'} | ({'period_label', 'period_start', 'period_end'} if domain != 'profile' else set()))
    identifier(target['room'])
    if op['task']['room'] != target['room'] or not session_matches(op['context'], target['room']):
        fail()
    if domain == 'profile':
        if row['phase'] != 2 or op['task']['op'] != 'profile':
            fail()
        if row['state'] == 'reading' and 'user' not in op['task']:
            fail()
    else:
        kind, step, phase = CONTENT[domain]
        if (row['phase'] != phase or op['task']['op'] != 'content' or op['task']['contentType'] != kind
                or type(op['step']) is not int or op['step'] != step
                or op['context']['period_label'] != 'requested_source_labels'
                or op['context']['period_start'] != op['task']['start']
                or op['context']['period_end'] != op['task']['end']):
            fail()


def moment(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        if parsed.strftime('%Y-%m-%d %H:%M:%S') != value:
            fail()
        return parsed
    except (TypeError, ValueError):
        fail()


def label(value):
    return value.strftime('%Y-%m-%d %H:%M:%S')


def session_matches(context, room):
    return context.get('session') == {'type': 'string', 'value': room, 'lossless': True}


def profile_user(operation, target):
    offset = operation.get('user_offset')
    users = target['users']
    if type(offset) is not int or not 0 <= offset < len(users):
        fail()
    user = users[offset]
    if 'user' in operation['task'] and operation['task']['user'] != user:
        fail()
    return user


def derive_guarded_successors(scope, targets, operations):
    """Derive only independent fixed-list/fixed-window successors of reading.

    Never mutate the unknown operation or reconstruct a task already recorded in
    any state. Unknown cursor/result-dependent domains are explicitly deferred.
    A complete input set, code hashes, identity, expiry and atomic idempotence
    remain duties of a future reviewed host; none are bypassed here.
    """
    ids = [row['id'] for row in operations]
    if len(set(ids)) != len(ids):
        fail()
    scope_digest = hashlib.sha256(encode(scope)).hexdigest()
    candidates, deferred, existing = [], [], []
    reading = [row for row in operations if row['state'] == 'reading']
    created_keys = set()
    for row in reading:
        original = row['operation']
        domain = original['domain']
        if domain not in scope['domains'] or not original.get('attempt'):
            fail()
        if domain != 'profile' and domain not in CONTENT:
            deferred.append(row['id'])
            continue
        target = targets.get(row['target'])
        if not target:
            fail()
        fixed_shape(row, target)
        successor = deepcopy(original)
        successor.pop('attempt')
        # claim() will supply the same reviewed owner/body scope in the future.
        successor['task'].pop('owner', None)
        successor['task'].pop('includeBody', None)
        peers = [peer for peer in operations if peer['target'] == row['target'] and peer['operation']['domain'] == domain]
        for peer in peers:
            fixed_shape(peer, target)
        if domain == 'profile':
            users = target['users']
            if not isinstance(users, list):
                fail()
            for user in users:
                identifier(user)
            if len(set(users)) != len(users):
                fail()
            profile_user(original, target)
            offset = original['user_offset'] + 1
            if offset >= len(users):
                continue
            successor['user_offset'] = offset
            successor['task'].pop('user', None)
            key = (row['target'], domain, users[offset])
            matches = []
            for peer in peers:
                if peer['state'] == 'no_targets':
                    # A valid terminal seed has no addressable user. A seed
                    # that now addresses a user contradicts the fixed snapshot.
                    offset_value = peer['operation']['user_offset']
                    if type(offset_value) is not int or offset_value < len(users) or 'user' in peer['operation']['task']:
                        fail()
                    continue
                if profile_user(peer['operation'], target) == users[offset]:
                    matches.append(peer['id'])
        else:
            kind, step, phase = CONTENT[domain]
            task, context = original['task'], original['context']
            start, end = moment(task['start']), moment(task['end'])
            room_start, room_end = moment(target['start']), moment(target['end'])
            if (row['phase'] != phase or task.get('op') != 'content' or task.get('contentType') != kind
                    or type(original.get('step')) is not int or original['step'] != step
                    or not room_start <= start <= room_end
                    or (start-room_start).total_seconds() % step
                    or end != min(start+timedelta(seconds=step-1), room_end)
                    or context.get('period_label') != 'requested_source_labels'
                    or context.get('period_start') != task['start'] or context.get('period_end') != task['end']):
                fail()
            next_start = start + timedelta(seconds=step)
            while next_start <= room_end:
                next_end = min(next_start+timedelta(seconds=step-1), room_end)
                if [domain, label(next_start), label(next_end)] not in target['content_success']:
                    break
                next_start += timedelta(seconds=step)
            if next_start > room_end:
                continue
            successor['task'].update(start=label(next_start), end=label(next_end))
            successor['context'].update(period_start=label(next_start), period_end=label(next_end))
            key = (row['target'], domain, label(next_start), label(next_end))
            matches = []
            for peer in peers:
                other = peer['operation']['task']
                other_start, other_end = moment(other['start']), moment(other['end'])
                if other_start <= next_end and next_start <= other_end:
                    if (other_start, other_end) != (next_start, next_end):
                        raise CollectorError('GUARDED_SUCCESSOR_OVERLAP')
                    matches.append(peer['id'])
        if len(matches) > 1 or key in created_keys:
            raise CollectorError('GUARDED_SUCCESSOR_AMBIGUOUS')
        if matches:
            existing.append(row['id'])
            continue
        created_keys.add(key)
        candidates.append({'source_reading': row['id'], 'source_attempt': original['attempt'],
                           'target': row['target'], 'phase': row['phase'], 'scope_digest': scope_digest,
                           'operation': successor})
    return {'successors': candidates, 'unresolved_reading': [row['id'] for row in reading],
            'deferred_chains': deferred, 'already_recorded_successors': existing,
            'original_scope_complete': False, 'execution_authorized': False}
