"""Consume independently enrolled approvals; never approve page metadata.

Registration is internal only; its status has a separate authenticated read.
No approval producer, identity upgrades, message import, or browser activation.
New contracts occupy a reserved source-ID range
so removing the evidence marker cannot fall back to the inherited contract.
"""
import re

from .common import AppError
from .db import digest, stable_key
from .identity import IdentityService
from .local_deletion import require_ready

PREFIX = 'native-reviewed-'
FORMAT = 'native-profile-registration-1'
CAPABILITIES = ('account_identity', 'person_identity', 'conversation_identity', 'native_profile_binding')
REQUEST_FIELDS = {'source_id', 'source_version', 'approval_source_id', 'approval_source_version', 'account_key'}


def _text(value, limit=256):
    return type(value) is str and 1 <= len(value) <= limit and re.fullmatch(r'[A-Za-z0-9_-]+', value) is not None


def _integrity(source):
    fields = ('source_id', 'source_version', 'source_kind', 'capabilities', 'verified', 'evidence')
    return source['verified'] is True and source['registration_digest'] == digest({key: source[key] for key in fields})


def _record(value, account, device):
    from .native_profile import RECORD_FIELDS, checked_request
    from .native_identity_metadata import FORMAT as METADATA_FORMAT, EXTRA_FIELDS, valid_record
    if type(value) is not dict:
        return False
    metadata = value.get('format') == METADATA_FORMAT
    if set(value) != (RECORD_FIELDS | EXTRA_FIELDS if metadata else RECORD_FIELDS):
        return False
    if metadata:
        if not valid_record(value):
            return False
    elif (value['format'] != 'native-profile-binding-1' or not _text(value['observation_key'], 140) or
            not re.fullmatch(r'observation_[A-Za-z0-9_-]{1,128}', value['observation_key'])):
        return False
    if (value['basis'] != 'independent_current_registration' or
        value['account_key'] != account or value['native_device_key'] != device or value['conversation_type'] != 'private' or
        not _text(value['owner_uid']) or not _text(value['peer_uid']) or value['owner_uid'] == value['peer_uid'] or
        account != stable_key('account', 'douyin', value['owner_uid'])):
        return False
    try:
        checked_request({'source_id': 'validation', 'source_version': '1', 'owner_uid': value['owner_uid'],
                         'sec_uid': value['peer_sec_uid'], 'platform_conversation_key': value['platform_conversation_key']})
    except AppError:
        return False
    return True


def _review_basis(value, record, registration=None):
    """Validate a host review's binding, not the truth of its cited evidence."""
    def text(item, limit):
        return (type(item) is str and 1 <= len(item) <= limit and item.strip() == item and
                not any(ord(char) < 32 or ord(char) == 127 for char in item))

    def sha(item):
        return type(item) is str and re.fullmatch(r'[0-9a-f]{64}', item) is not None

    if (type(value) is not dict or set(value) != {'format', 'fact_review', 'evidence_refs', 'execution_review'} or
        value['format'] != 'native-profile-review-basis-1' or
        type(record) is not dict or not all(type(v) is str for v in record.values())):
        return False
    fact, refs, execution = value['fact_review'], value['evidence_refs'], value['execution_review']
    if (type(fact) is not dict or set(fact) != {'review_id', 'sha256'} or
        not text(fact['review_id'], 200) or not sha(fact['sha256']) or
        type(refs) is not list or not 1 <= len(refs) <= 8 or
        type(execution) is not dict or
        set(execution) != {'review_id', 'record_digest', 'operator_key', 'purpose', 'reason', 'registration'}):
        return False
    target = execution['registration']
    if (type(target) is not dict or set(target) != REQUEST_FIELDS or
        any(not _text(target[key], 80 if key.endswith('version') else 256 if key == 'account_key' else 200)
            for key in REQUEST_FIELDS) or not target['source_id'].startswith(PREFIX) or target['source_id'] == PREFIX or
        target['approval_source_id'].startswith(PREFIX) or target['account_key'] != record.get('account_key') or
        registration is not None and target != registration):
        return False
    seen = set()
    for ref in refs:
        if (type(ref) is not dict or set(ref) != {'sha256', 'record_ref'} or
            not sha(ref['sha256']) or not text(ref['record_ref'], 512)):
            return False
        key = (ref['sha256'], ref['record_ref'])
        if key in seen:
            return False
        seen.add(key)
    return bool(text(execution['review_id'], 200) and text(execution['operator_key'], 200) and
                execution['purpose'] == 'approve_current_local_registration' and
                text(execution['reason'], 1200) and sha(execution['record_digest']) and
                execution['record_digest'] == digest(record))


def _approval_item(item):
    if type(item) is not dict:
        return False
    fields = {'format', 'source_id', 'source_version', 'record'}
    if item.get('format') == 'native-profile-approval-1':
        return set(item) == fields
    return (item.get('format') == 'native-profile-approval-2' and set(item) == fields | {'review_basis'} and
            _review_basis(item['review_basis'], item['record']) and
            item['review_basis']['execution_review']['registration']['source_id'] == item['source_id'] and
            item['review_basis']['execution_review']['registration']['source_version'] == item['source_version'])


def _approval(identities, source_mode, ref, source_id, version, account, device):
    if (type(ref) is not dict or set(ref) != {'source_id', 'source_version', 'registration_digest'} or
        not _text(ref['source_id'], 200) or not _text(ref['source_version'], 80) or
        type(ref['registration_digest']) is not str or not re.fullmatch(r'[0-9a-f]{64}', ref['registration_digest']) or
        ref['source_id'] == source_id or ref['source_id'].startswith(PREFIX)):
        return None
    approved = identities.get_source(ref['source_id'], ref['source_version'])
    if (not approved or not _integrity(approved) or approved['registration_digest'] != ref['registration_digest'] or
        approved['source_kind'] not in ('fictional', 'qualified_adapter', 'trusted_local_action') or
        approved['source_kind'] == 'fictional' and source_mode != 'fictional'):
        return None
    caps, evidence = approved['capabilities'], approved['evidence']
    if (type(caps) is not dict or caps.get('native_profile_registration') is not True or
        type(caps.get('account_keys')) is not list or account not in caps['account_keys'] or
        type(evidence) is not dict or set(evidence) != {'native_profile_approvals'} or
        identities.origin_withdrawn(device, account, ref['source_id'], ref['source_version'])):
        return None
    records = evidence['native_profile_approvals']
    if type(records) is not list or not 1 <= len(records) <= 32:
        return None
    if any(not _approval_item(item) for item in records):
        return None
    matches = [item for item in records if item['source_id'] == source_id and item['source_version'] == version]
    if len(matches) != 1 or not _record(matches[0]['record'], account, device):
        return None
    from .native_identity_metadata import FORMAT as METADATA_FORMAT
    if matches[0]['record']['format'] == METADATA_FORMAT and matches[0]['format'] != 'native-profile-approval-2':
        return None
    if matches[0]['format'] == 'native-profile-approval-2':
        registration = matches[0]['review_basis']['execution_review']['registration']
        if registration['approval_source_id'] != ref['source_id'] or registration['approval_source_version'] != ref['source_version']:
            return None
    return {'record': matches[0]['record'],
            'source_kind': 'fictional' if approved['source_kind'] == 'fictional' else 'qualified_adapter'}


def native_approval_current(db, source_mode, native, account, device):
    """Shared registration/lookup check for the reserved new-contract range."""
    try:
        evidence = native['evidence']
        if (not native['source_id'].startswith(PREFIX) or not _integrity(native) or
            native['capabilities'] != {**dict.fromkeys(CAPABILITIES, True), 'account_keys': [account]} or
            type(evidence) is not dict or set(evidence) != {'format', 'approval', 'native_profile_records'} or
            evidence['format'] != FORMAT or type(evidence['native_profile_records']) is not list or
            len(evidence['native_profile_records']) != 1):
            return False
        approved = _approval(IdentityService(db), source_mode, evidence['approval'], native['source_id'],
                             native['source_version'], account, device)
        return bool(approved and native['source_kind'] == approved['source_kind'] and
                    evidence['native_profile_records'][0] == approved['record'])
    except (KeyError, TypeError, ValueError):
        return False


class NativeProfileRegistration:
    def __init__(self, app):
        self.app, self.db = app, app.db
        self.identities = IdentityService(self.db)

    def status(self, value, principal, reauthorize):
        """Read saved registration qualification, never grant page authority."""
        fields = {'source_id', 'source_version', 'account_key'}
        if (type(value) is not dict or set(value) != fields or
            any(not _text(value[key], 80 if key == 'source_version' else 256 if key == 'account_key' else 200) for key in fields) or
            not value['source_id'].startswith(PREFIX) or value['source_id'] == PREFIX or not callable(reauthorize)):
            raise AppError(400, 'NATIVE_REGISTRATION_STATUS_INVALID', '登记状态只接受来源版本和账号定位')
        request = dict(value)
        account = request['account_key']
        principal.require('sidebar:read', account)

        def authorize():
            if reauthorize(principal, account) != principal:
                raise AppError(403, 'NATIVE_REGISTRATION_AUTH_CHANGED', '登记状态授权已变化')
            principal.require('sidebar:read', account)

        from .native_profile import NativeProfileLookup
        lookup = NativeProfileLookup(self.app)

        def read_chain():
            local = self.db.conn.execute("SELECT value FROM app_meta WHERE key='device_key'").fetchone()
            if not local or local[0] != principal.device_key or self.app.config['device_key'] != principal.device_key:
                raise AppError(403, 'NATIVE_REGISTRATION_DEVICE_MISMATCH', '只能核对当前本机设备的登记')
            native = self.identities.get_source(request['source_id'], request['source_version'])
            if not native or not native_approval_current(self.db, self.app.config['source_mode'], native, account, principal.device_key):
                return None
            record = native['evidence']['native_profile_records'][0]
            query = {'source_id': request['source_id'], 'source_version': request['source_version'],
                     'owner_uid': record['owner_uid'], 'sec_uid': record['peer_sec_uid'],
                     'platform_conversation_key': record['platform_conversation_key']}
            chain = lookup._chain(query, principal, account)
            if not chain:
                return None
            return {'signature': chain['signature'], 'registration': {
                'source_id': native['source_id'], 'source_version': native['source_version'],
                'registration_digest': native['registration_digest'], 'approval': dict(native['evidence']['approval']),
                'native_device_key': record['native_device_key'], 'query': query, 'binding': dict(chain['binding'])}}

        unknown = {'format': 'native-registration-status-1', 'state': 'unknown',
                   'reason': 'registration_not_qualified', 'request_digest': digest(request), 'registration': None,
                   'native_page_verification': 'background_required', 'context_authorization': 'not_granted'}
        authorize()
        with lookup._snapshot():
            fence = lookup._fence()
            first = read_chain()
        authorize()
        if not first:
            return unknown
        with lookup._snapshot():
            final = read_chain()
            unchanged = final == first and lookup._fence() == fence
        authorize()
        if not unchanged or lookup._fence() != fence:
            return dict(unknown, reason='registration_changed')
        return dict(unknown, state='registered', reason=None, registration=first['registration'])

    def register(self, value, principal, reauthorize):
        return self._register(value, principal, reauthorize)

    def _register(self, value, principal, reauthorize, *, permit=None):
        if (type(value) is not dict or set(value) != REQUEST_FIELDS or
            any(not _text(value[key], 80 if key.endswith('version') else 256 if key == 'account_key' else 200) for key in REQUEST_FIELDS) or
            not value['source_id'].startswith(PREFIX) or value['source_id'] == PREFIX or not callable(reauthorize)):
            raise AppError(400, 'NATIVE_REGISTRATION_REQUEST_INVALID', '登记只接受批准来源与目标版本定位，不接受页面身份或批准标记')
        account = value['account_key']
        principal.require('source:register', account)
        if principal.role != 'manager':
            raise AppError(403, 'NATIVE_REGISTRATION_FORBIDDEN', '需要指定账号的主管来源登记权限')
        def authorize():
            if reauthorize(principal, account) != principal:
                raise AppError(403, 'NATIVE_REGISTRATION_AUTH_CHANGED', '登记授权已变化')
            principal.require('source:register', account)
            if permit is None:
                if not self.app.writes_enabled:
                    raise AppError(503, 'NATIVE_REGISTRATION_DISABLED', '当前工作区未启用写入')
            else:
                from .local_native_registration_entry import _NativeSubmissionPermit
                if type(permit) is not _NativeSubmissionPermit:
                    raise AppError(403, 'LOCAL_NATIVE_PERMIT_INVALID', '本次有限登记许可无效。')
                permit.check_registration(self.app, value, principal)

        from .native_profile import NativeProfileLookup
        lookup = NativeProfileLookup(self.app)
        authorize()
        with self.db.transaction() as conn:
            require_ready(self.db)
            authorize()
            local = conn.execute("SELECT value FROM app_meta WHERE key='device_key'").fetchone()
            if not local or local[0] != principal.device_key or self.app.config['device_key'] != principal.device_key:
                raise AppError(403, 'NATIVE_REGISTRATION_DEVICE_MISMATCH', '只能登记当前本机设备的批准')
            approval = self.identities.get_source(value['approval_source_id'], value['approval_source_version'])
            ref = {'source_id': value['approval_source_id'], 'source_version': value['approval_source_version'],
                   'registration_digest': approval['registration_digest'] if approval else ''}
            approved = _approval(self.identities, self.app.config['source_mode'], ref, value['source_id'],
                                 value['source_version'], account, principal.device_key)
            if not approved:
                raise AppError(403, 'NATIVE_REGISTRATION_APPROVAL_MISSING', '缺少独立登记的精确批准或批准已撤销')
            record = approved['record']
            old = self.identities.get_source(value['source_id'], value['source_version'])
            # This insert remains inside the outer transaction. Any prerequisite,
            # source withdrawal or authorization failure rolls it back; no owner,
            # alias, conversation or accepted observation is created here.
            self.identities.register_source(value['source_id'], value['source_version'], source_kind=approved['source_kind'],
                capabilities={**dict.fromkeys(CAPABILITIES, True), 'account_keys': [account]}, verified=True,
                evidence={'format': FORMAT, 'approval': ref, 'native_profile_records': [record]})
            query = {'source_id': value['source_id'], 'source_version': value['source_version'], 'owner_uid': record['owner_uid'],
                     'sec_uid': record['peer_sec_uid'], 'platform_conversation_key': record['platform_conversation_key']}
            chain = lookup._chain(query, principal, account)
            if not chain:
                raise AppError(409, 'NATIVE_REGISTRATION_PREREQUISITE_MISSING', '账号、直接身份、私聊会话或已接受观察未满足登记条件')
            authorize()
            final = lookup._chain(query, principal, account)
            if not final or final['signature'] != chain['signature']:
                raise AppError(409, 'NATIVE_REGISTRATION_CHANGED', '登记期间批准或前置证据已变化')
            result = {'format': FORMAT, 'state': 'already_registered' if old else 'registered',
                      'source_id': value['source_id'], 'source_version': value['source_version'],
                      'binding': chain['binding'], 'native_page_verification': 'background_required'}
        return result
