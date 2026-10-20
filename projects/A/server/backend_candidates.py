"""B v1 backend projections -> bounded, unapproved A candidates, in memory only.

No B imports, decryption, database, identity registration or business DTOs. A
caller must separately review source permission and evidence before any use.
The optional file entry reads only explicitly selected, hash-pinned projections.
"""
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import math
import re

from .common import AppError, canonical_json

FORMAT = 'a-backend-candidates-1'
MAX_RUN_BYTES = 256 * 1024
MAX_OBSERVATION_BYTES = 256 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024
MAX_BATCH_ROWS = 100
DOMAINS = frozenset(('account', 'session_list', 'session', 'summary', 'rank', 'profile', 'trend', 'capability'))
B_DOMAINS = DOMAINS | {'comment', 'gift', 'speech', 'chat', 'legacy_chat'}
BAD = re.compile(r'cookie|authorization|password|access.?token|refresh.?token|csrf|signature|a_bogus|msToken|headers|credentials', re.I)
BODY = re.compile(r'(?:^|[./_])(text|content|body)(?:$|[./_])', re.I)
TREND_BAD = re.compile(r'cookie|token|secret|auth|signature|password|content|body|text', re.I)
NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')
PATHS = {
    'account': '/passport/account/info/v2/',
    'session_list': '/anchor_pc_tinker_proxy/lego/native/webcast_api/room/replay/history_list',
    'session': '/anchor_pc_tinker_proxy/lego/native/simple/room/replay/room_base_v2',
    'summary': '/anchor_pc_tinker_proxy/lego/native/webcast_api/room/replay/overview_v3',
    'rank': '/anchor_pc_tinker_proxy/lego/native/webcast_api/anchor/public/rank',
    'profile': '/anchor_pc_tinker_proxy/lego/native/webcast_api/anchor/audience/info_card',
    'trend': '/anchor_pc_tinker_proxy/lego/native/webcast_api/room/replay/minute_trend',
    'capability': '/adapter-capability',
}
FIELDS = {
    'account': set('user_id_str sec_user_id app_id'.split()),
    'session_list': set('roomID roomTitle startTime endTime startTimeUnix endTimeUnix duration liveDuration serverWatchUcntTdDirect roomLiveEarnScore liveNewFollowUcnt isNormalGroupLive'.split()),
    'session': set('room_id anchor_id start_time end_time duration title room_title status'.split()),
    'summary': set('earn_score consume_ucnt consume_rate show_ucnt watch_ucnt watch_rate acu pcu avg_watch_duration_mins_rate comment_ucnt like_cnt follow_ucnt share_cnt join_fansclub_ucnt fans_like_cnt non_fans_like_cnt like_rate room_id anchor_id'.split()),
    'rank': set('rank value watch_time like_cnt comment_cnt contribute_value accompany_day_cnt user.id_str user.id user.sec_uid user.nickname user.display_id'.split()),
    'profile': set('interact_index.watch_duration interact_index.watch_duration_7d interact_index.watch_cnt_7d interact_index.consume_cnt_7d interact_index.comment_cnt_7d'.split()),
    'capability': {'domain', 'status'},
}
LOCAL_FIELDS = set('b_observer.local_date b_observer.timezone b_observer.utc_offset_minutes b_projection.omitted_fields b_request.user_id b_repair.operation b_repair.attempt b_diagnostic.operation b_diagnostic.stage b_diagnostic.phase b_diagnostic.http_status b_diagnostic.exception b_diagnostic.request_stage'.split())
SOURCE_META_NAMES = set('start_time end_time begin_time start_date end_date update_time updated_at server_time current_time cutoff_time data_time timezone time_zone tz unit value_unit precision decimal_places total total_count count limit offset has_more hasMore is_complete inclusive_start inclusive_end'.split())
SOURCE_META_PREFIXES = ('root', 'data', 'root.meta', 'data.meta', 'root.window', 'data.window', 'root.pagination', 'data.pagination')
SOURCE_META_FIELDS = frozenset('b_source.' + prefix + '.' + name for prefix in SOURCE_META_PREFIXES for name in SOURCE_META_NAMES)
FLAGS = {'state': 'candidate', 'persisted': False, 'formal_acceptance': False,
         'business_writes': 0, 'sabcde_eligible': False, 'platform_complete': False}


def fail(code='BACKEND_CANDIDATE_INVALID', status=422):
    raise AppError(status, code, '后台候选未通过有限核对；未登记身份或写入业务资料。')


def keys(obj, required, optional=()):
    if type(obj) is not dict or not set(required) <= set(obj) <= set(required) | set(optional):
        fail()


def text(value, maximum=80, pattern=None):
    if type(value) is not str or len(value) > maximum or (pattern is not None and re.fullmatch(pattern, value) is None):
        fail()


def key(value):
    text(value, 32, r'[0-9a-f]{32}')


def count(value, maximum=1000000):
    if type(value) is not int or not 0 <= value <= maximum:
        fail()


def stamp(value):
    text(value, 40)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            fail()
        return parsed
    except ValueError:
        fail()


def scalar(obj):
    keys(obj, ('type', 'value', 'lossless'))
    t, v = obj['type'], obj['value']
    if type(t) is not str or type(obj['lossless']) is not bool:
        fail()
    if t in ('string', 'number_token'):
        text(v, 32768, NUMBER.pattern if t == 'number_token' else None)
    elif t == 'number':
        if type(v) not in (int, float) or abs(v) > 9007199254740991 or not math.isfinite(v):
            fail('BACKEND_UNSAFE_NUMBER')
    elif t == 'boolean':
        if type(v) is not bool:
            fail()
    elif t != 'null' or v is not None:
        fail()


def decode(raw, maximum):
    if type(raw) is not bytes or len(raw) > maximum:
        fail('BACKEND_CANDIDATE_LIMIT', 413)
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                fail('BACKEND_JSON_DUPLICATE_KEY')
            result[k] = v
        return result
    try:
        obj = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: fail('BACKEND_JSON_INVALID'))
        stack = [(obj, 0)]
        while stack:
            node, depth = stack.pop()
            if depth > 16:
                fail('BACKEND_JSON_INVALID')
            if type(node) is dict:
                stack.extend((v, depth + 1) for v in node.values())
            elif type(node) is list:
                stack.extend((v, depth + 1) for v in node)
        # Reject infinities from numeric overflow and invalid Unicode before use.
        canonical_json(obj)
        return obj
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        fail('BACKEND_JSON_INVALID')


def _planning_counts(obj, depth=0):
    if type(obj) is int:
        count(obj, 10**12)
    elif type(obj) is dict and len(obj) <= 32 and depth < 3:
        for name, child in obj.items():
            text(name, 80, r'[a-z_]+')
            _planning_counts(child, depth + 1)
    else:
        fail()


def _client_resumes(value):
    # Resume events are provenance only, not a client-to-observation mapping.
    # Bound like code_revisions; preserve duplicates and source order.
    if type(value) is not list or len(value) > 100:
        fail()
    for event in value:
        keys(event, ('client', 'at', 'queue_sha256'))
        text(event['client'], 80, r'b-gap-client/1')
        text(event['at'], 29, r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}\+00:00')
        stamp(event['at'])
        text(event['queue_sha256'], 64, r'[0-9a-f]{64}')


def validate_run(run, *, now=None):
    keys(run, ('format', 'run_id', 'scope', 'started_at', 'finished_at', 'stop_reason', 'expires_at', 'formal_A_acceptance'), ('base_repair', 'gap_repair', 'fresh_batch'))
    if run['format'] != 'b-capture-run/1' or run['formal_A_acceptance'] is not False:
        fail()
    key(run['run_id'])
    scope = run['scope']
    keys(scope, ('domains', 'start_date', 'end_date', 'include_body', 'retention_days'))
    domains = scope['domains']
    if type(domains) is not list or not domains or any(type(d) is not str or d not in B_DOMAINS for d in domains) or len(domains) != len(set(domains)):
        fail()
    if type(scope['include_body']) is not bool or type(scope['retention_days']) is not int or not 1 <= scope['retention_days'] <= 30:
        fail()
    for name in ('start_date', 'end_date'):
        text(scope[name], 10, r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
        try:
            date.fromisoformat(scope[name])
        except ValueError:
            fail()
    if scope['start_date'] > scope['end_date']:
        fail()
    start, expiry = stamp(run['started_at']), stamp(run['expires_at'])
    if start >= expiry or (run['finished_at'] is not None and stamp(run['finished_at']) < start):
        fail()
    if run['stop_reason'] not in (None, 'completed_requested_scope', 'cancelled', 'source_changed', 'disconnected', 'failed', 'budget'):
        fail()
    if expiry <= (now or datetime.now(timezone.utc)):
        fail('BACKEND_CANDIDATE_EXPIRED', 410)
    if sum(name in run for name in ('base_repair', 'gap_repair', 'fresh_batch')) > 1:
        fail()
    if 'fresh_batch' in run:
        fresh = run['fresh_batch']
        keys(fresh, ('format', 'job', 'code_hash', 'account_reference', 'owner', 'app'))
        if (fresh['format'] != 'b-fresh-backend/1' or scope['include_body'] is not False
                or not set(domains) <= {'account', 'rank', 'session_list', 'capability'}):
            fail()
        text(fresh['job'], 80, r'b-fresh-backend-[0-9]{8}-[0-9]{2}')
        text(fresh['code_hash'], 64, r'[0-9a-f]{64}')
        key(fresh['account_reference'])
        scalar(fresh['owner']); scalar(fresh['app'])
    for name in ('base_repair', 'gap_repair'):
        if name not in run:
            continue
        repair = run[name]
        if name == 'base_repair':
            keys(repair, ('format', 'parent_run', 'owner', 'app', 'rooms', 'unlocated_failures', 'other_diagnostics', 'already_saved'))
            if repair['format'] != 'b-base-repair/1':
                fail()
            key(repair['parent_run'])
            if type(repair['rooms']) is not list or len(repair['rooms']) > 10000:
                fail()
            for room in repair['rooms']:
                text(room, 40, r'[0-9]{1,40}')
            for field in ('unlocated_failures', 'other_diagnostics', 'already_saved'):
                count(repair[field])
        else:
            keys(repair, ('format', 'root_run', 'parents', 'owner', 'app', 'code_hash', 'summary', 'coverage'), ('code_revisions', 'client_resumes'))
            if repair['format'] != 'b-backend-gap/1' or repair['coverage'] != 'request_attempts_not_platform_completeness':
                fail()
            key(repair['root_run'])
            text(repair['code_hash'], 64, r'[0-9a-f]{64}')
            if type(repair['parents']) is not list or not 1 <= len(repair['parents']) <= 100:
                fail()
            for parent in repair['parents']:
                keys(parent, ('run_id', 'count', 'meta_digest'))
                key(parent['run_id']); count(parent['count'], 10**12)
                text(parent['meta_digest'], 64, r'[0-9a-f]{64}')
            _planning_counts(repair['summary'])
            revisions = repair.get('code_revisions', [])
            if type(revisions) is not list or len(revisions) > 100:
                fail()
            for revision in revisions:
                keys(revision, ('from', 'to', 'changed_at', 'reason'))
                for field in ('from', 'to'):
                    text(revision[field], 64, r'[0-9a-f]{64}')
                stamp(revision['changed_at'])
                text(revision['reason'], 80, r'[A-Z0-9_]+')
            _client_resumes(repair.get('client_resumes', []))
        scalar(repair['owner']); scalar(repair['app'])
    return run


def _field_allowed(domain, name, *, fresh=False):
    if fresh and domain in ('rank', 'session_list') and name in SOURCE_META_FIELDS:
        return True
    if name in LOCAL_FIELDS:
        return True
    if domain == 'trend':
        if TREND_BAD.search(name):
            return False
        return (re.fullmatch(r'[A-Za-z0-9_]{1,100}', name) is not None or
                re.fullmatch(r'meta\.(?:unit|title|(?:field|packages|categoryAxis|valueAxis)\[[0-9]{1,3}\](?:\[[0-9]{1,3}\]|\.(?:key|title|unit|field|name))?)', name) is not None)
    return name in FIELDS[domain]


def validate_observation(outer, run):
    keys(outer, ('observation_id', 'run_id', 'sequence', 'ingested_at', 'observation'))
    key(outer['observation_id']); key(outer['run_id'])
    count(outer['sequence'], 2**53 - 1)
    if outer['run_id'] != run['run_id'] or outer['sequence'] == 0:
        fail('BACKEND_OBSERVATION_SCOPE')
    stamp(outer['ingested_at'])
    ob = outer['observation']
    keys(ob, ('contract', 'domain', 'source', 'context', 'observed_at', 'fields', 'coverage', 'issues'))
    domain = ob['domain']
    if type(domain) is not str or domain not in DOMAINS or domain not in run['scope']['domains']:
        fail('BACKEND_DOMAIN_NOT_SUPPORTED')
    if ob['contract'] != 'b-source-observation/1':
        fail()
    stamp(ob['observed_at'])
    source = ob['source']
    keys(source, ('origin', 'path', 'adapter', 'transport', 'record_ref', 'raw_response_saved'))
    if source['origin'] != 'https://anchor.douyin.com' or source['path'] != PATHS[domain] or source['transport'] != ('capability' if domain == 'capability' else 'site_get') or source['raw_response_saved'] is not False:
        fail('BACKEND_SOURCE_NOT_SUPPORTED')
    # This receiving contract was reviewed through B 0.1.6; future versions wait.
    text(source['adapter'], 80, r'b-fixed-sources/0\.1\.[0-6]')
    text(source['record_ref'], 180, r'/(?:projection/rows/[0-9]+)?')
    context = ob['context']
    keys(context, ('owner', 'app_scope', 'session', 'conversation', 'period_label', 'period_start', 'period_end', 'source_timezone', 'source_cutoff'))
    if context['conversation'] is not None:
        fail('BACKEND_DOMAIN_NOT_SUPPORTED')
    for field in ('owner', 'app_scope', 'session'):
        if context[field] is not None:
            scalar(context[field])
    for field in ('period_label', 'period_start', 'period_end', 'source_timezone', 'source_cutoff'):
        if context[field] is not None:
            text(context[field])
    fields = ob['fields']
    if type(fields) is not dict or len(fields) > 160:
        fail()
    for name, value in fields.items():
        text(name, 180)
        if BAD.search(name) or BODY.search(name) or not _field_allowed(domain, name, fresh='fresh_batch' in run):
            fail('BACKEND_FIELD_NOT_SUPPORTED')
        scalar(value)
    coverage = ob['coverage']
    keys(coverage, ('status', 'basis', 'received', 'limit', 'terminal'))
    if coverage['status'] not in ('partial', 'unknown', 'not_available') or coverage['terminal'] not in ('unknown', 'source_no_more', 'budget', 'cancelled', 'selected_file_end', 'cache_only'):
        fail()
    text(coverage['basis'], 60, r'[a-z_]{1,60}')
    for field in ('received', 'limit'):
        if coverage[field] is not None:
            count(coverage[field])
    if type(ob['issues']) is not list or len(ob['issues']) > 30:
        fail()
    for issue in ob['issues']:
        text(issue, 80, r'[A-Z0-9_]+')
    return ob


def _relation(direct, context):
    if direct is None or context is None or direct['type'] == 'null' or context['type'] == 'null':
        return 'unknown'
    if direct['lossless'] is not True or context['lossless'] is not True:
        return 'unverified'
    return 'exact_wrapper_match' if direct == context else 'conflict'


def _adapt(outer, run):
    ob = validate_observation(outer, run)
    domain, fields, context = ob['domain'], ob['fields'], ob['context']
    missing = {'A_SOURCE_APPROVAL_REQUIRED', 'A_IDENTITY_MAPPING_REQUIRED', 'SOURCE_LIFECYCLE_REVIEW_REQUIRED', 'PLATFORM_COVERAGE_UNVERIFIED'}
    relations = {}
    for name, field, ctx in (
        ('owner', 'user_id_str' if domain == 'account' else 'anchor_id', 'owner'),
        ('app', 'app_id', 'app_scope'),
        ('session', 'roomID' if domain == 'session_list' else 'room_id', 'session'),
    ):
        relations[name] = _relation(fields.get(field), context[ctx])
    repair = run.get('base_repair') or run.get('gap_repair')
    if repair:
        relations['repair_owner'] = _relation(context['owner'], repair['owner'])
        relations['repair_app'] = _relation(context['app_scope'], repair['app'])
        missing.add('REPAIR_PARENT_CHAIN_UNVERIFIED')
    if 'fresh_batch' in run:
        relations['fresh_owner'] = _relation(context['owner'], run['fresh_batch']['owner'])
        relations['fresh_app'] = _relation(context['app_scope'], run['fresh_batch']['app'])
        missing.add('FRESH_ACCOUNT_REFERENCE_REVIEW_REQUIRED')
    if any(name in SOURCE_META_FIELDS for name in fields):
        missing.add('SOURCE_ENVELOPE_SEMANTICS_UNVERIFIED')
    if 'conflict' in relations.values():
        missing.add('DIRECT_CONTEXT_CONFLICT')
    if context['owner'] is None:
        missing.add('OWNER_CONTEXT_MISSING')
    if domain not in ('account', 'capability'):
        missing.update(('SOURCE_TIME_BASIS_UNVERIFIED', 'METRIC_UNIT_PRECISION_UNVERIFIED'))
    if context['period_label'] == 'requested_source_labels':
        missing.add('REQUEST_INTERVAL_NOT_VERIFIED_WINDOW')
    if domain == 'rank':
        label = context['period_label'] or ''
        kind, sep, period = label.partition(':')
        rank_kinds = {'score': 'contribution', 'comment_cnt': 'comment', 'comment_cnt_realtime': 'comment_realtime', 'watch_duration': 'watch', 'like': 'like'}
        known = kind in rank_kinds and period in ('room', '30d', '7d', 'today', '1d', 'last_live') and bool(sep)
        scope = {'room': 'session_rank', '30d': 'relative_30d_rank', '7d': 'relative_7d_rank'}.get(period, 'other_relative_rank') if known else 'unknown_rank'
        proposed_kind = rank_kinds.get(kind) if known else None
        if not known:
            missing.add('RANK_SELECTOR_UNREVIEWED')
        missing.update(('EXACT_RANK_WINDOW_UNVERIFIED', 'RANK_POSITION_REVIEW_REQUIRED'))
        if period == 'room' and context['session'] is None:
            missing.add('SESSION_CONTEXT_MISSING')
    else:
        scope = {'summary': 'session_aggregate', 'session': 'session_base', 'session_list': 'session_catalog', 'profile': 'mixed_profile_windows', 'trend': 'session_trend', 'account': 'account_candidate', 'capability': 'diagnostic'}[domain]
        proposed_kind = None
    if domain in ('session', 'session_list'):
        missing.add('SESSION_END_FORMAL_STATUS_UNVERIFIED')
    if domain == 'profile':
        missing.add('REQUEST_TARGET_NOT_RESPONSE_IDENTITY')
    if any(v['lossless'] is not True for v in fields.values()):
        missing.add('LOSSLESS_FALSE')
    if not fields or 'EMPTY_RESPONSE_NOT_COMPLETE' in ob['issues'] or 'NO_ALLOWLISTED_FIELDS' in ob['issues']:
        missing.add('EMPTY_RESULT_NOT_ZERO')
    adapted = []
    for name, value in fields.items():
        group = scope
        if name in SOURCE_META_FIELDS:
            group = 'source_envelope_unreviewed'
        elif name in LOCAL_FIELDS:
            group = 'request_context' if name == 'b_request.user_id' else 'collector_metadata'
        elif domain == 'profile':
            group = 'relative_7d_at_observation' if name.endswith('_7d') else 'session_profile'
        elif domain == 'rank' and name == 'accompany_day_cnt':
            group = 'relative_15d_display'
        elif domain == 'trend' and name.startswith('meta.'):
            group = 'trend_metadata'
        escaped = name.replace('~', '~0').replace('/', '~1')
        adapted.append({'field': name, 'pointer': '/observation/fields/' + escaped,
                        'scalar': value, 'scope': group, 'unit': None, 'precision': 'unreviewed'})
    # Identity of a candidate is a B observation, never a platform event or A key.
    return {'candidate_key': 'b:' + outer['run_id'] + ':' + outer['observation_id'],
            'envelope_sha256': hashlib.sha256(canonical_json(outer)).hexdigest(),
            'evidence': outer, 'domain': domain, 'scope': scope,
            'proposed_rank_kind': proposed_kind, 'fields': adapted, 'relations': relations,
            'missing': sorted(missing), **FLAGS}


def adapt_batch(run_raw, observation_jsonl, *, now=None):
    """Receive at most 100 explicit backend envelopes. No partial result on error.

    Both buffers may contain sensitive identities; callers must not log results.
    Returns detached candidates, preserving all scalars and the original run.
    Only summarize() is suitable for ordinary CLI output.
    """
    run = validate_run(decode(run_raw, MAX_RUN_BYTES), now=now)
    if type(observation_jsonl) is not bytes or len(observation_jsonl) > MAX_BATCH_BYTES:
        fail('BACKEND_CANDIDATE_LIMIT', 413)
    records, by_key, by_sequence = [], {}, {}
    rows = duplicates = 0
    for line_number, line in enumerate(observation_jsonl.splitlines(), 1):
        if not line.strip():
            continue
        rows += 1
        if rows > MAX_BATCH_ROWS:
            fail('BACKEND_CANDIDATE_LIMIT', 413)
        candidate = _adapt(decode(line, MAX_OBSERVATION_BYTES), run)
        cid, hashed = candidate['candidate_key'], candidate['envelope_sha256']
        sequence = candidate['evidence']['sequence']
        if (cid in by_key and by_key[cid] != hashed) or (sequence in by_sequence and by_sequence[sequence] != cid):
            fail('BACKEND_OBSERVATION_CONFLICT', 409)
        if cid in by_key:
            duplicates += 1
            continue
        by_key[cid], by_sequence[sequence] = hashed, cid
        candidate['input_line'] = line_number
        records.append(candidate)
    validate_run(run, now=now)
    return {'format': FORMAT, 'run': run, 'records': records, 'input_rows': rows,
            'duplicates': duplicates, 'input_sha256': {'run_plaintext': hashlib.sha256(run_raw).hexdigest(),
            'observations_plaintext': hashlib.sha256(observation_jsonl).hexdigest()}, **FLAGS}


def summarize(batch):
    """Allowlisted counts/codes only. No arbitrary source strings or identifiers."""
    return {'format': FORMAT, 'input_rows': batch['input_rows'], 'candidates': len(batch['records']),
            'duplicates': batch['duplicates'], 'domains': dict(Counter(r['domain'] for r in batch['records'])),
            'scopes': dict(Counter(r['scope'] for r in batch['records'])),
            'missing': dict(Counter(code for r in batch['records'] for code in r['missing'])), **FLAGS}


def plan_files(run_file, run_sha256, observations_file, observations_sha256):
    """Hash-pinned, locked reads; no discovery/decryption/output files or DB access.

    This is a bounded projection entry, not a B encrypted-package importer.
    Check absolute source expiry before opening the observations, then again
    after reading/adaptation. Ancestor/revocation claims remain unverified.
    """
    from .identity_candidate_files import pinned_bytes
    try:
        run_raw = pinned_bytes(run_file, run_sha256, maximum=MAX_RUN_BYTES)
        validate_run(decode(run_raw, MAX_RUN_BYTES))
        observations = pinned_bytes(observations_file, observations_sha256, maximum=MAX_BATCH_BYTES)
        return adapt_batch(run_raw, observations)
    except AppError as error:
        if error.code.startswith('BACKEND_'):
            raise
        fail('BACKEND_PINNED_INPUT_REJECTED')
    except (OSError, ValueError, TypeError, OverflowError):
        fail('BACKEND_INPUT_REJECTED')
