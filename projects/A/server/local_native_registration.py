"""Host-only submission of an independently reviewed, exact local registration.

The trusted host caller supplies BOTH a fact-review reference and a separate
execution review for this record/device/observation. Their shape and digests
bind a submission; they cannot establish that a review actually took place.
The explicit protected host entry can call the private limited-write path.
Do not pass a historical adoption report as an execution review or fabricate
missing prerequisites. There is no HTTP or page submission route.
"""
import json

from .common import AppError, canonical_json
from .db import digest
from .identity import IdentityService
from .local_deletion import require_ready
from .native_profile import NativeProfileLookup
from .native_registration import (
    NativeProfileRegistration, PREFIX, REQUEST_FIELDS, _record, _review_basis, _text,
)


class LocalNativeRegistration:
    def __init__(self, app):
        self.app, self.db = app, app.db
        self.identities = IdentityService(self.db)

    def submit(self, value, principal, reauthorize):
        return self._submit(value, principal, reauthorize)

    def _submit(self, value, principal, reauthorize, *, permit=None):
        fields = REQUEST_FIELDS | {'format', 'record', 'review_basis'}
        if (type(value) is not dict or set(value) != fields or
            value['format'] != 'local-native-registration-submit-1' or not callable(reauthorize) or
            any(not _text(value[key], 80 if key.endswith('version') else 256 if key == 'account_key' else 200)
                for key in REQUEST_FIELDS) or
            not value['source_id'].startswith(PREFIX) or value['source_id'] == PREFIX or
            value['approval_source_id'].startswith(PREFIX)):
            raise AppError(400, 'LOCAL_NATIVE_SUBMISSION_INVALID', '只接受主机侧精确登记及独立审核依据，不接收页面声明。')
        account = value['account_key']
        principal.require('source:register', account)
        if principal.role != 'manager':
            raise AppError(403, 'LOCAL_NATIVE_SUBMISSION_FORBIDDEN', '需要该账号的主管来源登记权限。')
        if (type(value['record']) is not dict or not all(type(v) is str for v in value['record'].values()) or
            not _record(value['record'], account, principal.device_key) or
            not _review_basis(value['review_basis'], value['record'], {key: value[key] for key in REQUEST_FIELDS}) or
            value['review_basis']['execution_review']['operator_key'] != principal.operator_key):
            raise AppError(400, 'LOCAL_NATIVE_REVIEW_INVALID', '需要绑定本机精确记录及提交者的本次执行审核，历史事实采纳不能代替。')
        # Own the entire input before any reauthorization callback can run.
        request = json.loads(canonical_json(value))
        mode = self.app.config['source_mode']

        def authorize():
            if reauthorize(principal, account) != principal:
                raise AppError(403, 'LOCAL_NATIVE_AUTH_CHANGED', '本机批准提交授权已变化。')
            principal.require('source:register', account)
            if (mode not in ('fictional', 'unverified') or
                self.app.config['source_mode'] != mode):
                raise AppError(503, 'LOCAL_NATIVE_SUBMISSION_DISABLED', '工作区未启用写入或来源模式已变化。')
            if permit is None:
                if not self.app.writes_enabled:
                    raise AppError(503, 'LOCAL_NATIVE_SUBMISSION_DISABLED', '工作区未启用写入。')
            else:
                from .local_native_registration_entry import _NativeSubmissionPermit
                if type(permit) is not _NativeSubmissionPermit:
                    raise AppError(403, 'LOCAL_NATIVE_PERMIT_INVALID', '本次有限登记许可无效。')
                permit.check_submission(self.app, request, principal)
            if self.app.config['device_key'] != principal.device_key:
                raise AppError(403, 'LOCAL_NATIVE_DEVICE_MISMATCH', '只能向本机提交精确登记。')

        authorize()
        lookup = NativeProfileLookup(self.app)

        def registration_authorize(original, requested_account):
            if original != principal or requested_account != account:
                raise AppError(403, 'LOCAL_NATIVE_AUTH_CHANGED', '登记授权范围已变化。')
            authorize()
            return principal

        with self.db.transaction() as conn:
            require_ready(self.db)
            authorize()
            local = conn.execute("SELECT value FROM app_meta WHERE key='device_key'").fetchone()
            if not local or local[0] != principal.device_key:
                raise AppError(403, 'LOCAL_NATIVE_DEVICE_MISMATCH', '安装设备与批准目标不一致。')
            for key, version_key in [('approval_source_id', 'approval_source_version'), ('source_id', 'source_version')]:
                if self.identities.origin_withdrawn(principal.device_key, account, request[key], request[version_key]):
                    raise AppError(403, 'LOCAL_NATIVE_SOURCE_WITHDRAWN', '该来源版本已撤销，不能通过重新提交恢复。')
            approval = self.identities.register_source(
                request['approval_source_id'], request['approval_source_version'],
                source_kind='fictional' if mode == 'fictional' else 'trusted_local_action',
                capabilities={'native_profile_registration': True, 'account_keys': [account]}, verified=True,
                evidence={'native_profile_approvals': [{
                    'format': 'native-profile-approval-2', 'source_id': request['source_id'],
                    'source_version': request['source_version'], 'record': request['record'],
                    'review_basis': request['review_basis'],
                }]})
            result = NativeProfileRegistration(self.app)._register(
                {key: request[key] for key in REQUEST_FIELDS}, principal, registration_authorize, permit=permit)
            record = request['record']
            query = {'source_id': request['source_id'], 'source_version': request['source_version'],
                     'owner_uid': record['owner_uid'], 'sec_uid': record['peer_sec_uid'],
                     'platform_conversation_key': record['platform_conversation_key']}
            first = lookup._chain(query, principal, account)
            authorize()
            final = lookup._chain(query, principal, account)
            if not first or not final or first['signature'] != final['signature']:
                raise AppError(409, 'LOCAL_NATIVE_SUBMISSION_CHANGED', '批准或前置事实在提交期间变化，整次提交回滚。')
            return {'format': 'local-native-registration-submitted-1', 'state': result['state'],
                    'source_id': request['source_id'], 'source_version': request['source_version'],
                    'approval_source_id': request['approval_source_id'],
                    'approval_source_version': request['approval_source_version'],
                    'approval_digest': approval['registration_digest'], 'review_digest': digest(request['review_basis']),
                    'native_page_verification': 'background_required', 'context_authorization': 'not_granted',
                    'identity_or_observation_created': False}
