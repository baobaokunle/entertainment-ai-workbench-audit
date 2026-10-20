"""Reviewed recovery of the original saved directory, with durable claims.

Unattributed old failures stay unknown. Completed, touched-but-incomplete, failed
and claimed conversations are excluded rather than silently retried.
"""
from copy import deepcopy
import hashlib
from .chat_resume import ChatResumePlan, DIRECTORY_PATHS, COMPLETE, raw, stable
from .contract import encode
from .security import CollectorError

CLAIM = 'CHAT_DIRECTORY_RECOVERY_ATTEMPT_STARTED'


class ChatReviewedRecoveryPlan(ChatResumePlan):
    client = 'b-chat-reviewed-recovery/1'

    def __init__(self, run, row, meta, approval):
        super().__init__(run, row, meta)
        self.approval = deepcopy(approval)
        self.prefix_count = approval['prefix_count']
        if (run != approval['run_id'] or row[3] < self.prefix_count
                or meta['expires_at'] != approval['expires_at']
                or hashlib.sha256(encode(meta['scope'])).hexdigest() != approval['scope_sha256']):
            raise CollectorError('CHAT_RECOVERY_APPROVAL_MISMATCH')
        self.prefix = hashlib.sha256()
        self.cipher_count = 0
        self.unknown_seen = {}
        self.touched, self.failed = set(), set()

    def observe_cipher(self, sequence, cipher):
        if sequence <= self.prefix_count:
            if sequence != self.cipher_count + 1: raise CollectorError('CHAT_RESUME_RECORD_MISMATCH')
            self.prefix.update(sequence.to_bytes(8, 'big'))
            self.prefix.update(len(cipher).to_bytes(8, 'big'))
            self.prefix.update(cipher)
            self.cipher_count = sequence

    def feed(self, payload, sequence):
        super().feed(payload, sequence)
        item = payload['observation']; f = item['fields']; ctx = item['context']; source = item['source']
        if item['domain'] == 'account': return
        if item['domain'] == 'chat' and source['path'] in DIRECTORY_PATHS:
            if sequence > self.prefix_count: raise CollectorError('CHAT_RECOVERY_DIRECTORY_CHANGED')
            return
        bound = {encode(v) for v in (ctx['conversation'], f.get('b_request.conversation_id'),
                 f.get('conversation.id'), f.get('conversationId')) if stable(v)}
        if item['domain'] == 'chat':
            if not bound: raise CollectorError('CHAT_RECOVERY_BINDING_UNKNOWN')
            self.touched.update(bound)
            return
        status = raw(f.get('status'))
        if (status in {'SOURCE_PERMISSION','HTTP_401','HTTP_403','HTTP_429','SOURCE_RATE_LIMITED','CHAT_NATIVE_RESPONSE_NOT_SUCCESS','CHAT_NATIVE_CONTRACT_CHANGED','SOURCE_CHANGED'}
                or raw(f.get('b_diagnostic.http_status')) in (401,403,429,'401','403','429')
                or set(item.get('issues',[])) & {'SOURCE_PERMISSION','HTTP_401','HTTP_403','HTTP_429','SOURCE_RATE_LIMITED'}):
            raise CollectorError('CHAT_RECOVERY_GLOBAL_FAILURE')
        if not bound:
            expected = self.approval['unattributed'].get(str(sequence))
            if expected and sequence <= self.prefix_count and status == expected and source['path'] == '/adapter-capability':
                self.unknown_seen[str(sequence)] = status
                return
            benign = {'CHAT_LIST_END_UNCONFIRMED', 'CHAT_LIST_LOAD_STALLED', 'CHAT_SCROLL_CONTAINER_UNAVAILABLE', 'CHAT_COVERAGE_PARTIAL'}
            if (status not in benign or source['path'] != '/native-sdk/chat-coverage'
                    or source['transport'] != 'capability' or raw(f.get('domain')) != 'chat'):
                raise CollectorError('CHAT_RECOVERY_GLOBAL_FAILURE')
            return
        if status not in COMPLETE | {CLAIM}:
            self.failed.update(bound)

    def finish(self):
        super().finish()
        if (self.cipher_count != self.prefix_count or self.prefix.hexdigest() != self.approval['prefix_sha256']
                or self.unknown_seen != self.approval['unattributed']):
            raise CollectorError('CHAT_RECOVERY_APPROVAL_MISMATCH')
        excluded = self.completed | self.claimed | self.failed | self.touched
        targets = [deepcopy(v) for k,v in self.conversations.items() if k not in excluded]
        expected=self.approval.get('initial_target_sha256')
        if self.total==self.prefix_count and expected and hashlib.sha256(encode(targets)).hexdigest()!=expected:
            raise CollectorError('CHAT_RECOVERY_APPROVAL_MISMATCH')
        return targets

    def evidence(self):
        targets = self.finish()
        return {'directory_parser':'index_and_load/2', 'recovery_basis':'reviewed_original_uncompleted_directory',
                'original_prefix_count':self.prefix_count, 'original_prefix_sha256':self.prefix.hexdigest(),
                'old_unattributed_failure_count':len(self.unknown_seen),
                'excluded_incomplete_count':len((self.touched | self.claimed | self.failed) - self.completed),
                'unresolved_history_count':len((self.touched | self.claimed | self.failed) - self.completed)+len(self.unknown_seen),
                'target_set_sha256':hashlib.sha256(encode(targets)).hexdigest()}
