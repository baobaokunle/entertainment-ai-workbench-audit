"""Finite backend handoff preflight. No B imports, decryption or database access.

The private pipe is evidence transport, never source approval or identity trust.
Only the returned allowlisted summary may be displayed or saved in a report.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import backend_candidates as backend
from .common import canonical_json

FORMAT = 'a-fixed-handoff-receipt-1'
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 5000
CATEGORIES = {'rank30d': {'account', 'rank'},
              'session_directory': {'account', 'session_list'},
              'session_facts': {'account', 'session_list', 'session', 'rank', 'profile'}}


def fail():
    backend.fail('BACKEND_HANDOFF_INVALID')


def inspect_payload(raw, expected_sha256, category, *, now=None):
    """Inspect all selected rows; chunking does not relax the existing 100-row API."""
    now = now or datetime.now(timezone.utc)
    data = backend.decode(raw, MAX_BYTES)
    backend.keys(data, ('format', 'category', 'package_sha256', 'expires_at', 'source_checked',
                        'runs', 'observations', 'owner', 'app_scope', 'operation_receipts'))
    if (data['format'] != 'a-fixed-handoff-input-1' or category not in CATEGORIES
            or data['category'] != category or data['package_sha256'] != expected_sha256
            or data['source_checked'] is not True):
        fail()
    backend.text(expected_sha256, 64, r'[0-9a-f]{64}')
    expiry = backend.stamp(data['expires_at'])
    if expiry <= now:
        backend.fail('BACKEND_CANDIDATE_EXPIRED', 410)
    for key in ('owner', 'app_scope'):
        backend.scalar(data[key])
        if data[key]['lossless'] is not True or data[key]['type'] == 'null':
            fail()
    backend.count(data['operation_receipts'], MAX_ROWS)
    runs, rows = data['runs'], data['observations']
    if type(runs) is not list or not 1 <= len(runs) <= 100 or type(rows) is not list or not 1 <= len(rows) <= MAX_ROWS:
        fail()
    by_run = {}
    for run in runs:
        backend.validate_run(run, now=now)
        if run['run_id'] in by_run or expiry > backend.stamp(run['expires_at']):
            fail()
        by_run[run['run_id']] = run
    grouped, ids, sequences = {}, set(), set()
    for row in rows:
        if type(row) is not dict or row.get('run_id') not in by_run:
            fail()
        observation = backend.validate_observation(row, by_run[row['run_id']])
        context = observation['context']
        if (observation['domain'] not in CATEGORIES[category]
                or context['owner'] != data['owner'] or context['app_scope'] != data['app_scope']):
            fail()
        if category == 'rank30d' and observation['domain'] == 'rank' and context['period_label'] not in ('score:30d', 'comment_cnt:30d', 'watch_duration:30d'):
            fail()
        identity, sequence = (row['run_id'], row['observation_id']), (row['run_id'], row['sequence'])
        if identity in ids or sequence in sequences:
            backend.fail('BACKEND_OBSERVATION_CONFLICT', 409)
        ids.add(identity); sequences.add(sequence)
        grouped.setdefault(row['run_id'], []).append(row)
    domains, scopes, missing = Counter(), Counter(), Counter()
    rank_counts, rank_dates, directory_dates = Counter(), set(), Counter()
    rank_windows = rank_cutoffs = profile_session_rows = 0
    source_meta = {}
    for run_id, selected in grouped.items():
        for start in range(0, len(selected), backend.MAX_BATCH_ROWS):
            chunk = selected[start:start + backend.MAX_BATCH_ROWS]
            batch = backend.adapt_batch(canonical_json(by_run[run_id]), b'\n'.join(map(canonical_json, chunk)), now=now)
            summary = backend.summarize(batch)
            domains.update(summary['domains']); scopes.update(summary['scopes']); missing.update(summary['missing'])
            for record in batch['records']:
                ob = record['evidence']['observation']; context = ob['context']
                for name, scalar in ob['fields'].items():
                    if name in backend.SOURCE_META_FIELDS:
                        item = source_meta.setdefault(name, {'occurrences': 0, 'non_null': 0, 'types': {}})
                        item['occurrences'] += 1
                        item['non_null'] += scalar['type'] != 'null'
                        item['types'][scalar['type']] = item['types'].get(scalar['type'], 0) + 1
                if ob['domain'] == 'profile' and any(field['scope'] == 'session_profile'
                        and field['scalar']['type'] != 'null' and field['scalar']['lossless'] is True
                        for field in record['fields']):
                    profile_session_rows += 1
                if record['scope'] == 'relative_30d_rank':
                    rank_counts[record['proposed_rank_kind']] += 1
                    rank_dates.add(backend.stamp(ob['observed_at']).astimezone(timezone(timedelta(hours=8))).date().isoformat())
                    rank_windows += bool(context['period_start'] and context['period_end'])
                    rank_cutoffs += bool(context['source_cutoff'])
                if ob['domain'] == 'session_list':
                    label = context['period_label'] or ''
                    if label.startswith('requested_calendar_day:'):
                        day = label.split(':', 1)[1]
                        backend.text(day, 10, r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
                        from datetime import date
                        date.fromisoformat(day)
                        directory_dates[day] += 1
    person_candidates = scopes['session_rank'] + profile_session_rows
    gaps = ['A_SOURCE_APPROVAL_REQUIRED', 'A_IDENTITY_MAPPING_REQUIRED', 'NOT_FORMAL_IMPORT']
    if rank_counts:
        if rank_windows != sum(rank_counts.values()): gaps.append('EXACT_30D_WINDOW_MISSING')
        if rank_cutoffs != sum(rank_counts.values()): gaps.append('SOURCE_CUTOFF_MISSING')
        gaps.append('OBSERVATION_DAY_IS_NOT_RANK_AS_OF_DAY')
    if category in ('session_facts', 'session_directory'):
        gaps.extend(['FORMAL_ENDED_SESSION_REVIEW_REQUIRED', 'PER_PERSON_COVERAGE_UNVERIFIED', 'ABSENCE_IS_NOT_ZERO'])
        if not person_candidates: gaps.append('SESSION_PERSON_FACTS_MISSING')
        if data['operation_receipts']: gaps.append('OPERATION_SEMANTICS_REVIEW_REQUIRED')
    return {'format': FORMAT, 'category': category, 'package_sha256': expected_sha256,
            'received_at': now.isoformat(), 'source_expires_at': data['expires_at'],
            'state': 'candidate_checked', 'selected_rows': len(rows), 'source_runs': len(runs),
            'domains': dict(domains), 'scopes': dict(scopes), 'missing': dict(missing),
            'rank_counts': dict(rank_counts), 'rank_observed_beijing_days': sorted(rank_dates),
            'rank_rows_with_exact_window': rank_windows, 'rank_rows_with_source_cutoff': rank_cutoffs,
            'raw_source_metadata_fields': source_meta,
            'directory_requested_days': dict(sorted(directory_dates.items())),
            'session_base_rows': scopes['session_base'], 'session_rank_rows': scopes['session_rank'],
            'profile_candidate_rows': domains['profile'], 'profile_with_session_metric_rows': profile_session_rows,
            'session_person_candidate_rows': person_candidates,
            'operation_receipts': data['operation_receipts'], 'gaps': gaps,
            'source_check_scope': 'B_reader_selected_ciphertext_and_source_metadata_at_read',
            'formal_acceptance': False, 'platform_complete': False, 'current_tier_eligible': False,
            'business_writes': 0, 'identity_merges': 0, 'chat_bodies_read': 0}
