"""Read pinned, pre-existing export aggregates; never read message bodies or DBs."""
import re
from pathlib import Path

from .identity_candidate_files import pinned_bytes, strict_json
from .backup_scratch import reject_links

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20000
MAX_COUNT = 1000000000


def unknown_summary():
    return {'format': 'historical-sender-summary-1', 'state': 'unknown',
            'scope': 'export_sender_all_contexts', 'coverage': 'qualified_root_sender_pairs_only',
            'message_count': None, 'conversation_count': None, 'private_message_count': None,
            'owner_verified': False, 'private_context_verified': False, 'formal_identity_match': False}


def normalize_summary(value, eligible=False):
    expected=unknown_summary()
    if type(value) is not dict or set(value) != set(expected):
        return expected
    if any(value[k] != expected[k] for k in ('format','scope','coverage')) or value['private_message_count'] is not None:
        return expected
    if any(value[k] is not False for k in ('owner_verified','private_context_verified','formal_identity_match')):
        return expected
    if value['state']=='unknown' and value['message_count'] is None and value['conversation_count'] is None:
        return expected
    count, conversations=value['message_count'],value['conversation_count']
    if (eligible and value['state']=='available' and type(count) is int and 1<=count<=MAX_COUNT and
            type(conversations) is int and 1<=conversations<=min(count,100000)):
        expected.update(state='available',message_count=count,conversation_count=conversations)
    return expected


class HistoricalSenderSummary:
    def __init__(self, *, evidence_path, evidence_sha256, snapshot_sha256, account_key, provider_namespace):
        self.path = Path(evidence_path)
        self.pin = evidence_sha256
        self.snapshot = snapshot_sha256
        self.account = account_key
        self.namespace = provider_namespace
        for value in (self.pin, self.snapshot):
            if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
                raise ValueError('history summary pin required')

    def __call__(self, result):
        summary = unknown_summary()
        try:
            if (result['status'] != 'pending' or type(result['candidate_count']) is not int or result['candidate_count'] != 1 or
                    result['account_key'] != self.account or result['provider_namespace'] != self.namespace or
                    any(result[k] is not False for k in ('owner_verified', 'private_context_verified', 'formal_identity_match'))):
                return summary
            candidates = result['candidates']
            if type(candidates) is not list or len(candidates) != 1:
                return summary
            candidate = candidates[0]
            uid, sec = candidate['uid'], candidate['sec_uid']
            if (type(uid) is not str or not re.fullmatch('[0-9]{1,64}', uid) or
                    type(sec) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,256}', sec) or
                    sec != result['sec_uid'] or candidate['state'] != 'pending'):
                return summary
            refs = candidate['source_refs']
            if not refs or any(ref != {'source_id': 'identity-pilot-snapshot',
                    'source_version': '003', 'snapshot_digest': self.snapshot} for ref in refs):
                return summary
            packet = strict_json(pinned_bytes(self.path, self.pin, maximum=MAX_BYTES))
            if (type(packet) is not dict or set(packet) != {'snapshot_sha256', 'account_key', 'provider_namespace', 'pairs'} or
                    packet['snapshot_sha256'] != self.snapshot or packet['account_key'] != self.account or
                    packet['provider_namespace'] != self.namespace or type(packet['pairs']) is not list or
                    len(packet['pairs']) > MAX_ROWS):
                return summary
            seen = set()
            found = None
            for pair in packet['pairs']:
                if type(pair) is not dict or set(pair) != {'uid', 'sec_uid', 'messages', 'conversation_ids'}:
                    return summary
                key = (pair['uid'], pair['sec_uid'])
                if (type(key[0]) is not str or not re.fullmatch('[0-9]{1,64}', key[0]) or
                        type(key[1]) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,256}', key[1]) or key in seen):
                    return summary
                seen.add(key)
                count, conversations = pair['messages'], pair['conversation_ids']
                if type(count) is not int or not 1 <= count <= MAX_COUNT or type(conversations) is not list or not 1 <= len(conversations) <= min(count, 100000):
                    return summary
                if any(not ((type(v) is str and 1 <= len(v) <= 256 and not any(ord(c) < 32 for c in v)) or
                            (type(v) is int and 0 <= v <= 9007199254740991)) for v in conversations):
                    return summary
                if len({(type(v), v) for v in conversations}) != len(conversations):
                    return summary
                if key == (uid, sec):
                    found = (count, len(conversations))
            if found is not None:
                summary.update(state='available', message_count=found[0], conversation_count=found[1])
        except Exception:
            # Never leak provenance, identifiers, source paths, or parser errors.
            return unknown_summary()
        return summary


def load_summary_manifest(path, *, evidence_path, snapshot_sha256, account_key, provider_namespace):
    path = Path(path)
    reject_links(path)
    if not path.is_file() or path.stat().st_size > 2048:
        raise ValueError('history summary configuration unavailable')
    packet = strict_json(path.read_bytes())
    if (type(packet) is not dict or set(packet) != {'format', 'evidence_path', 'evidence_sha256', 'snapshot_sha256', 'account_key', 'provider_namespace'} or
            packet['format'] != 'historical-sender-summary-manifest-1' or
            packet['account_key'] != account_key or packet['provider_namespace'] != provider_namespace or
            packet['snapshot_sha256'] != snapshot_sha256 or type(packet['evidence_path']) is not str or
            Path(packet['evidence_path']) != Path(evidence_path)):
        raise ValueError('history summary configuration does not match the existing export')
    return HistoricalSenderSummary(evidence_path=evidence_path, evidence_sha256=packet['evidence_sha256'],
        snapshot_sha256=snapshot_sha256, account_key=account_key, provider_namespace=provider_namespace)
