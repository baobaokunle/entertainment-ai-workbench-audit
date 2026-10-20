"""All-or-nothing, in-memory delta receipts. Private membership never leaves here."""
from collections import Counter
from datetime import datetime, timezone

from . import backend_candidates as backend
from .common import canonical_json
from .fixed_handoff_candidates import inspect_payload, MAX_BYTES


def fail():
    backend.fail('BACKEND_HANDOFF_DELTA_INVALID')


class DeltaInspection:
    def __init__(self, prior_receipt, prior_sha256, part_count):
        if (prior_receipt.get('format') != 'a-fixed-handoff-receipt-1'
                or prior_receipt.get('category') != 'session_facts'
                or prior_receipt.get('package_sha256') != prior_sha256
                or prior_receipt.get('state') != 'candidate_checked'
                or prior_receipt.get('formal_acceptance') is not False
                or prior_receipt.get('business_writes') != 0
                or type(part_count) is not int or not 1 <= part_count <= 16): fail()
        self.prior, self.prior_sha, self.count = prior_receipt, prior_sha256, part_count
        self.sequences, self.ids, self.operations = set(), set(), set()
        self.parts, self.digests = {}, set()
        self.scope = None
        self.failed = False

    def add(self, raw, expected_sha256, *, now=None):
        if self.failed: fail()
        try:
            return self._add(raw, expected_sha256, now=now)
        except Exception:
            self.failed = True
            raise

    def _add(self, raw, expected_sha256, *, now=None):
        value = backend.decode(raw, MAX_BYTES)
        delta = value.pop('delta')
        backend.keys(delta, ('prior_package_sha256', 'part_number', 'part_count', 'shared_context_rows',
                             'prior_rows', 'prior_operation_receipts', 'operation_keys', 'prior_field_checks_reused'))
        number = delta['part_number']
        shared = sum(self.prior['domains'].get(domain, 0) for domain in ('account', 'session_list', 'session'))
        if (delta['prior_package_sha256'] != self.prior_sha or delta['part_count'] != self.count
                or type(number) is not int or not 1 <= number <= self.count or number in self.parts
                or expected_sha256 in self.digests or delta['prior_rows'] != self.prior['selected_rows']
                or delta['shared_context_rows'] != shared or delta['prior_field_checks_reused'] is not True
                or delta['prior_operation_receipts'] != self.prior['operation_receipts']): fail()
        summary = inspect_payload(canonical_json(value), expected_sha256, 'session_facts', now=now)
        scope = canonical_json([value['owner'], value['app_scope'], value['expires_at']])
        if self.scope is not None and self.scope != scope: fail()
        new_sequences, new_ids = set(), set()
        for row in value['observations']:
            if row['observation']['domain'] not in ('rank', 'profile'): fail()
            new_sequences.add((row['run_id'], row['sequence']))
            new_ids.add((row['run_id'], row['observation_id']))
        if new_sequences & self.sequences or new_ids & self.ids: fail()
        keys = delta['operation_keys']
        if type(keys) is not list or len(keys) != summary['operation_receipts']: fail()
        for key in keys:
            if type(key) is not list or len(key) != 2: fail()
            for item in key: backend.key(item)
        ops = set(map(tuple, keys))
        if len(ops) != len(keys) or ops & self.operations: fail()
        self.scope = scope
        self.ids.update(new_ids); self.sequences.update(new_sequences); self.operations.update(ops)
        self.digests.add(expected_sha256)
        self.parts[number] = {**summary, 'part_number': number, 'shared_context_rows_reused': shared}

    def finish(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        if self.failed or set(self.parts) != set(range(1, self.count + 1)): fail()
        if any(backend.stamp(p['source_expires_at']) <= now for p in self.parts.values()): fail()
        domains = Counter(self.prior['domains'])
        for part in self.parts.values(): domains.update(part['domains'])
        return {'format': 'a-fixed-handoff-delta-receipt-1', 'state': 'candidate_checked',
                'category': 'session_delta', 'received_at': now.isoformat(),
                'prior_package_sha256': self.prior_sha, 'parts': [self.parts[i] for i in sorted(self.parts)],
                'new_observations': len(self.sequences), 'shared_context_rows_per_part': next(iter(self.parts.values()))['shared_context_rows_reused'],
                'combined_unique_observations': self.prior['selected_rows'] + len(self.sequences),
                'combined_domains': dict(domains), 'new_operation_references': len(self.operations),
                'combined_operation_references': self.prior['operation_receipts'] + len(self.operations),
                'directory_requested_days': self.prior['directory_requested_days'],
                'prior_field_checks_reused': True, 'platform_complete': False,
                'formal_acceptance': False, 'business_writes': 0, 'identity_merges': 0, 'chat_bodies_read': 0,
                'gaps': ['A_SOURCE_APPROVAL_REQUIRED', 'A_IDENTITY_MAPPING_REQUIRED', 'FORMAL_ENDED_SESSION_REVIEW_REQUIRED',
                         'PER_PERSON_COVERAGE_UNVERIFIED', 'SOURCE_UNIT_TIME_BASIS_UNVERIFIED', 'ABSENCE_IS_NOT_ZERO',
                         'OPERATION_SEMANTICS_REVIEW_REQUIRED', 'NOT_FORMAL_IMPORT']}
