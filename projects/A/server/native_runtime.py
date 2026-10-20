"""Reviewed metadata setup and short-lived runtime permission; never chat input.

Setup is called only by the protected local submission entry inside its outer
transaction. A pinned host review is an explicit decision, not a page claim.
Runtime resolution is read-only and additionally checks the whole saved chain.
"""
from datetime import datetime, timezone
import json
import re
from .common import AppError, canonical_json
from .db import digest, stable_key
from .identity import IdentityService
from .native_identity_metadata import OBSERVATION_FIELDS
from .native_profile import NativeProfileLookup, checked_request
from .native_registration import REQUEST_FIELDS, _record, _review_basis, _integrity, _text

READER = 'native-store-metadata-1'
READERS = {READER, 'native-bound-conversation-metadata-1', 'native-bound-context-data-1', 'native-panel-context-data-1'}
SETUP = 'local-native-setup-1'
REVOKE = 'local-native-runtime-revoke-1'
POLICY_FIELDS = {'source_id', 'source_version', 'reader', 'account_namespace',
                 'owner_provider_namespace', 'provider_namespace', 'expires_at'}


def fail():
    raise AppError(400, 'NATIVE_RUNTIME_INVALID', '身份运行登记或核对范围无效。')


def timestamp(value):
    try:
        if type(value) is not str or len(value) > 40: fail()
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.utcoffset() is None: fail()
        return result
    except ValueError:
        fail()


def policy_valid(policy):
    if type(policy) is not dict or set(policy) != POLICY_FIELDS: fail()
    if type(policy['reader']) is not str or policy['reader'] not in READERS: fail()
    for field in POLICY_FIELDS - {'expires_at', 'reader'}:
        if not _text(policy[field], 80 if field.endswith('version') else 200): fail()
    if not policy['source_id'].startswith('native-runtime-'): fail()
    # Explicit null means until revoked, only through the reviewed local setup.
    # Missing/invalid values are still rejected; existing finite grants stay finite.
    if policy['expires_at'] is not None and timestamp(policy['expires_at']) <= datetime.now(timezone.utc):
        raise AppError(410, 'NATIVE_RUNTIME_EXPIRED', '身份运行许可已到期。')


def prepare_setup(app, value, principal):
    """Exact reviewed prerequisite producer. No merging, discovery or defaults."""
    required = REQUEST_FIELDS | {'format', 'record', 'review_basis', 'identity_source', 'observation', 'runtime_policy'}
    if type(value) is not dict or set(value) != required or value['format'] != SETUP: fail()
    account = value['account_key']; principal.require('source:register', account)
    if principal.role != 'manager' or not app.db.conn.in_transaction: fail()
    record = value['record']; review = value['review_basis']; obs = value['observation']
    if (not _record(record, account, principal.device_key) or record['format'] != 'native-profile-metadata-binding-1'
            or not _review_basis(review, record, {k: value[k] for k in REQUEST_FIELDS})
            or review['execution_review']['operator_key'] != principal.operator_key): fail()
    if (type(obs) is not dict or set(obs) != OBSERVATION_FIELDS or not all(type(v) is str for v in obs.values())
            or obs['format'] != 'native-identity-observation-1' or obs['scope'] != 'douyin_web_private_snapshot'
            or obs['conversation_type'] != 'private' or not _text(obs['observation_id'], 200)
            or any(obs[k] != record[k] for k in ('account_key','owner_uid','peer_uid','peer_sec_uid','platform_conversation_key','conversation_type'))
            or not re.fullmatch('[0-9a-f]{64}', obs['snapshot_sha256'])
            or review['fact_review'] != {'review_id': obs['fact_review_id'], 'sha256': obs['fact_review_sha256']}
            or {'sha256': obs['snapshot_sha256'], 'record_ref': obs['snapshot_record_ref']} not in review['evidence_refs']): fail()
    timestamp(obs['observed_at'])
    identity = value['identity_source']
    if type(identity) is not dict or set(identity) != {'source_id', 'source_version'}: fail()
    if not all(_text(identity[k], 80 if k.endswith('version') else 200) for k in identity): fail()
    policy_valid(value['runtime_policy'])
    sources = [(identity['source_id'], identity['source_version']),
        (record['metadata_source_id'], record['metadata_source_version']),
        (value['source_id'], value['source_version']), (value['approval_source_id'], value['approval_source_version']),
        (value['runtime_policy']['source_id'], value['runtime_policy']['source_version'])]
    if len({s[0] for s in sources}) != 5: fail()
    ids = IdentityService(app.db)
    if any(ids.origin_withdrawn(principal.device_key, account, *s) for s in sources):
        raise AppError(403, 'NATIVE_RUNTIME_WITHDRAWN', '已撤销版本不能重新登记。')
    # Conflicting pre-existing account provenance is not silently upgraded.
    own = app.db.conn.execute('SELECT * FROM own_account WHERE account_key=?', (account,)).fetchone()
    if own and (own['platform'] != 'douyin' or own['platform_uid'] != record['owner_uid'] or
                (own['verified'] and (own['source_id'], own['source_version']) != sources[0])):
        raise AppError(409, 'NATIVE_ACCOUNT_REVIEW_REQUIRED', '已有账号来源不同，需保留并核对。')
    kind = 'fictional' if app.config['source_mode'] == 'fictional' else 'qualified_adapter'
    pair = {'account_key': account, 'uid': record['peer_uid'], 'sec_uid': record['peer_sec_uid'], 'observation_id': obs['observation_id']}
    ids.register_source(*sources[0], source_kind=kind, verified=True,
        capabilities={'account_identity': True, 'person_identity': True, 'direct_identity_mapping': True, 'account_keys': [account]},
        evidence={'direct_identity_records': [pair], 'fact_review': review['fact_review'], 'evidence_refs': review['evidence_refs']})
    ids.register_account(record['owner_uid'], source_id=sources[0][0], source_version=sources[0][1], verified=True)
    ids.register_direct_person_mapping(account, record['peer_uid'], record['peer_sec_uid'],
        source_id=sources[0][0], source_version=sources[0][1], observation_id=obs['observation_id'])
    metadata = ids.register_source(*sources[1], source_kind=kind, verified=True,
        capabilities={'native_identity_metadata': True, 'account_keys': [account]},
        evidence={'native_identity_observations': [obs]})
    if (metadata['registration_digest'] != record['metadata_source_digest'] or
            stable_key('native_observation', *sources[1], obs['observation_id']) != record['observation_key']): fail()
    return {k: v for k, v in value.items() if k in REQUEST_FIELDS | {'record','review_basis'}} | {'format': 'local-native-registration-submit-1'}


def install_policy(app, value, principal):
    policy = value['runtime_policy']; policy_valid(policy)
    ids = IdentityService(app.db)
    native = ids.get_source(value['source_id'], value['source_version'])
    return ids.register_source(policy['source_id'], policy['source_version'],
        source_kind='fictional' if app.config['source_mode'] == 'fictional' else 'trusted_local_action', verified=True,
        capabilities={'native_current_context': True, 'account_keys': [value['account_key']]},
        evidence={'format': 'native-runtime-policy-1', 'policy': policy, 'native_device_key': principal.device_key,
            'registration': {k: value[k] for k in REQUEST_FIELDS}, 'native_digest': native['registration_digest'],
            'execution_review_digest': digest(value['review_basis'])})


def revoke_policy(app, value, principal):
    if (type(value) is not dict or set(value) != {'format','account_key','operator_key','source_id','source_version'}
            or value['format'] != REVOKE or value['operator_key'] != principal.operator_key
            or not app.db.conn.in_transaction or principal.role != 'manager'): fail()
    principal.require('source:register', value['account_key'])
    ids = IdentityService(app.db); source = ids.get_source(value['source_id'], value['source_version'])
    if (not source or not source['source_id'].startswith('native-runtime-')
            or source['capabilities'] != {'native_current_context': True, 'account_keys': [value['account_key']]}
            or source['evidence'].get('native_device_key') != principal.device_key): fail()
    ids.withdraw_origin(principal.device_key, value['account_key'], value['source_id'], value['source_version'],
                        reason='native_runtime_withdrawn', emit_event=True)
    return {'state': 'revoked'}


def resolve_runtime(app, value, principal, reauthorize):
    """A read permission for a separately bound page observation, not page proof."""
    fields = {'format','authority','owner_uid','sec_uid','platform_conversation_key'}
    if (type(value) is not dict or set(value) != fields or type(value['format']) is not str or value['format'] not in READERS
            or value['authority'] != 'unverified' or not re.fullmatch('[0-9]{1,256}', str(value['owner_uid']))): fail()
    query_base = checked_request({k: value[k] for k in ('owner_uid','sec_uid','platform_conversation_key')} |
                                 {'source_id': 'validation', 'source_version': '1'})
    account = stable_key('account', 'douyin', value['owner_uid'])
    def authorize():
        principal.require('sidebar:read', account)
        if reauthorize(principal, account) != principal: fail()
    authorize()
    unknown = {'format': 'native-runtime-result-1', 'request_digest': digest(value), 'state': 'unknown', 'grant': None}
    ids = IdentityService(app.db); lookup = NativeProfileLookup(app)
    def read():
        matches = []
        rows = app.db.conn.execute("SELECT source_id,source_version FROM source WHERE source_id GLOB 'native-runtime-*' AND verified=1 LIMIT 129").fetchall()
        if len(rows) > 128: return None
        for row in rows:
            source = ids.get_source(*row)
            if not _integrity(source) or source['capabilities'] != {'native_current_context': True, 'account_keys': [account]}: continue
            if source['source_kind'] not in ('trusted_local_action', 'fictional') or source['source_kind'] == 'fictional' and app.config['source_mode'] != 'fictional': continue
            evidence = source['evidence']
            if type(evidence) is not dict or set(evidence) != {'format','policy','native_device_key','registration','native_digest','execution_review_digest'}: continue
            if evidence['format'] != 'native-runtime-policy-1' or evidence['native_device_key'] != principal.device_key or app.config['device_key'] != principal.device_key: continue
            if ids.origin_withdrawn(principal.device_key, account, *row): continue
            policy = evidence['policy']
            try: policy_valid(policy)
            except AppError: continue
            if policy['reader'] != value['format']: continue
            if (policy['source_id'], policy['source_version']) != tuple(row): continue
            registration = evidence['registration']
            if type(registration) is not dict or set(registration) != REQUEST_FIELDS or registration['account_key'] != account: continue
            native = ids.get_source(registration['source_id'], registration['source_version'])
            if not native or native['registration_digest'] != evidence['native_digest']: continue
            query = query_base | {'source_id': registration['source_id'], 'source_version': registration['source_version']}
            chain = lookup._chain(query, principal, account)
            if chain:
                matches.append({'query': query, 'binding': chain['binding'], 'policy': policy,
                    'native_device_key': principal.device_key, 'fingerprint': digest({'source': source, 'chain': chain['signature']})})
        return matches[0] if len(matches) == 1 else None
    with lookup._snapshot():
        fence = lookup._fence(); first = read()
    authorize()
    with lookup._snapshot():
        final = read()
        unchanged = lookup._fence() == fence
    authorize()
    if first is None or first != final or not unchanged or lookup._fence() != fence: return unknown
    return unknown | {'state': 'permitted', 'grant': final}
