"""Exact reviewed snapshot identity evidence, never a message/body observation.

This contract consumes an independently registered metadata source. It neither
promotes candidates nor produces identity, conversation, message or coverage rows.
"""
import re
from datetime import datetime

from .common import AppError
from .db import stable_key
from .identity import IdentityService
from .local_deletion import reject_hidden

FORMAT = 'native-profile-metadata-binding-1'
EXTRA_FIELDS = {'metadata_source_id', 'metadata_source_version', 'metadata_source_digest'}
OBSERVATION_FIELDS = {'format', 'observation_id', 'account_key', 'owner_uid', 'peer_uid', 'peer_sec_uid',
                      'platform_conversation_key', 'conversation_type', 'snapshot_sha256', 'snapshot_record_ref',
                      'observed_at', 'fact_review_id', 'fact_review_sha256', 'scope'}


def valid_record(value):
    from .native_registration import _text
    return (value.get('format') == FORMAT and _text(value.get('metadata_source_id'), 200) and
            not value['metadata_source_id'].startswith('native-reviewed-') and
            _text(value.get('metadata_source_version'), 80) and
            type(value.get('metadata_source_digest')) is str and
            re.fullmatch('[0-9a-f]{64}', value['metadata_source_digest']) is not None and
            type(value.get('observation_key')) is str and
            re.fullmatch(r'native_observation_[A-Za-z0-9_-]{1,128}', value['observation_key']) is not None)


def checked_basis(db, source_mode, native, record, account, device):
    """Return exact snapshot provenance or None; current page authority is absent."""
    from .native_registration import PREFIX, _integrity, _text
    from .native_profile import checked_request
    identities = IdentityService(db)
    if not native['source_id'].startswith(PREFIX):
        return None  # New branch must have the independent approval-2 contract.
    ref = native['evidence']['approval']
    approval = identities.get_source(ref['source_id'], ref['source_version'])
    if not approval or not _integrity(approval) or approval['registration_digest'] != ref['registration_digest']:
        return None
    items = approval['evidence']['native_profile_approvals']
    items = [item for item in items if item['source_id'] == native['source_id'] and item['source_version'] == native['source_version']]
    if len(items) != 1 or items[0]['format'] != 'native-profile-approval-2':
        return None
    review = items[0]['review_basis']
    if record['metadata_source_id'] in (native['source_id'], ref['source_id']):
        return None
    source = identities.get_source(record['metadata_source_id'], record['metadata_source_version'])
    if (not source or not _integrity(source) or source['registration_digest'] != record['metadata_source_digest'] or
            source['source_kind'] not in ('qualified_adapter', 'fictional') or
            source['source_kind'] == 'fictional' and source_mode != 'fictional' or
            source['capabilities'] != {'native_identity_metadata': True, 'account_keys': [account]} or
            type(source['evidence']) is not dict or set(source['evidence']) != {'native_identity_observations'} or
            identities.origin_withdrawn(device, account, source['source_id'], source['source_version'])):
        return None
    observations = source['evidence']['native_identity_observations']
    if type(observations) is not list or not 1 <= len(observations) <= 32:
        return None
    keys = set(); matches = []
    for item in observations:
        if (type(item) is not dict or set(item) != OBSERVATION_FIELDS or
                not all(type(v) is str for v in item.values()) or item['format'] != 'native-identity-observation-1' or
                not _text(item['observation_id'], 200) or item['scope'] != 'douyin_web_private_snapshot' or
                item['account_key'] != account or item['conversation_type'] != 'private' or
                stable_key('account', 'douyin', item['owner_uid']) != account or
                not _text(item['peer_uid']) or item['peer_uid'] == item['owner_uid'] or
                not re.fullmatch('[0-9a-f]{64}', item['snapshot_sha256']) or
                not re.fullmatch('[0-9a-f]{64}', item['fact_review_sha256']) or
                not 1 <= len(item['fact_review_id']) <= 200 or not item['snapshot_record_ref'].startswith('/') or
                len(item['snapshot_record_ref']) > 512 or
                any(ord(c) < 32 or ord(c) == 127 for c in item['snapshot_record_ref'] + item['fact_review_id'])):
            return None
        try:
            checked_request({'source_id': source['source_id'], 'source_version': source['source_version'],
                             'owner_uid': item['owner_uid'], 'sec_uid': item['peer_sec_uid'],
                             'platform_conversation_key': item['platform_conversation_key']})
            if len(item['observed_at']) > 40 or datetime.fromisoformat(item['observed_at']).utcoffset() is None:
                return None
        except (ValueError, AppError):
            return None
        key = stable_key('native_observation', source['source_id'], source['source_version'], item['observation_id'])
        if key in keys:
            return None
        keys.add(key)
        if key == record['observation_key']:
            matches.append(item)
        # Contradicting observations in the selected identity/conversation cannot
        # be ignored just because a different observation_id was selected.
        if (item['platform_conversation_key'] == record['platform_conversation_key'] or
                item['peer_sec_uid'] == record['peer_sec_uid']):
            if any(item[k] != record[k] for k in ('owner_uid', 'peer_uid', 'peer_sec_uid', 'platform_conversation_key')):
                return None
    if len(matches) != 1:
        return None
    item = matches[0]
    if (any(item[k] != record[k] for k in ('account_key','owner_uid','peer_uid','peer_sec_uid','platform_conversation_key','conversation_type')) or
            review['fact_review'] != {'review_id':item['fact_review_id'], 'sha256':item['fact_review_sha256']} or
            {'sha256':item['snapshot_sha256'],'record_ref':item['snapshot_record_ref']} not in review['evidence_refs']):
        return None
    reject_hidden(db, account, observation_key=record['observation_key'], source_id=source['source_id'],
                  source_version=source['source_version'], platform_conversation_key=record['platform_conversation_key'])
    return {'observation': item, 'source': source, 'approval': approval}
