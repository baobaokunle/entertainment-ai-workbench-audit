"""Resume only the committed, encrypted directory of an interrupted chat run.

This module does not contact a source, read the legacy DB, or start a new run.
Plans are bounded scans held in the owning host's memory. Completion evidence
comes only from already committed capability records, never message counts.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re
import uuid

from .contract import encode, validate_observation
from .security import CollectorError
from .store import now

CLIENT = 'b-chat-resume/2'
DIRECTORY_PATHS = {'/native-sdk/chat_index', '/native-sdk/chat_load'}
COMPLETE = {'CHAT_INCREMENTAL_BOUNDARY_REACHED', 'CHAT_HISTORY_SOURCE_NO_MORE'}
CONVERSATION_FIELDS = ('id', 'shortId', 'type', 'bizType', 'participantCount',
                       'toParticipantUserId', 'toParticipantSecUserId')


def raw(v):
    return v.get('value') if isinstance(v, dict) and v.get('lossless') is True else None


def stable(v):
    return isinstance(v, dict) and v.get('type') == 'string' and isinstance(raw(v), str) and bool(raw(v))


def eligible(row, meta, *, directory_repair=False):
    states = ('failed', 'disconnected', 'finished') if directory_repair else ('failed', 'disconnected')
    if row[1] not in states:
        raise CollectorError('CHAT_RESUME_STATE')
    if datetime.fromisoformat(meta['expires_at']) <= datetime.now(timezone.utc):
        raise CollectorError('RETENTION_EXPIRED')
    scope = meta['scope']
    if 'chat' not in scope['domains'] or set(scope['domains']) - {'chat', 'account', 'capability'} or meta.get('base_repair') or meta.get('gap_repair'):
        raise CollectorError('CHAT_SCOPE_ONLY')


class ChatResumePlan:
    client = CLIENT
    validate = staticmethod(eligible)

    def __init__(self, run, row, meta):
        self.validate(row, meta)
        if meta.get('run_id') != run: raise CollectorError('CHAT_RESUME_RECORD_MISMATCH')
        self.run, self.row, self.meta = run, row, deepcopy(meta)
        self.total, self.offset = row[3], 0
        self.owner = None
        self.conversations, self.completed, self.claimed = {}, set(), set()
        self.media_gaps = set()

    def feed(self, payload, sequence):
        if sequence != self.offset + 1 or payload.get('sequence') != sequence or payload.get('run_id') != self.run:
            raise CollectorError('CHAT_RESUME_RECORD_MISMATCH')
        item = payload['observation']
        validate_observation(item, self.meta['scope'])
        if item['source']['origin'] != 'https://www.douyin.com' or item['domain'] not in ('chat', 'account', 'capability'):
            raise CollectorError('CHAT_SCOPE_ONLY')
        context, fields, source = item['context'], item['fields'], item['source']
        owner = context['owner']
        if owner is not None:
            if not stable(owner) or (self.owner is not None and owner != self.owner):
                raise CollectorError('SOURCE_CHANGED')
            self.owner = owner
        elif item['domain'] != 'capability':
            raise CollectorError('OWNER_UNAVAILABLE')
        if item['domain'] == 'chat' and fields.get('b_chat.media_status') is not None:
            from .chat_store import message_key
            key = message_key(owner, context['conversation'], fields)
            if key is not None:
                if raw(fields.get('b_chat.media_file_saved')) is True:
                    self.media_gaps.discard(key)
                else:
                    self.media_gaps.add(key)
        if item['domain'] == 'chat' and source['path'] in DIRECTORY_PATHS:
            conv = context['conversation']
            if not stable(conv) or conv != fields.get('id') or source['transport'] != 'native_sdk':
                raise CollectorError('CHAT_RESUME_RECORD_MISMATCH')
            key = encode(conv)
            meta = {k: fields[k] for k in CONVERSATION_FIELDS if k in fields}
            previous = self.conversations.get(key)
            if previous and any(k in previous and k in meta and previous[k] != meta[k] for k in ('id', 'shortId', 'type')):
                raise CollectorError('SOURCE_CHANGED')
            if previous is None:
                self.conversations[key] = meta
        if item['domain'] == 'capability' and raw(fields.get('status')) in COMPLETE:
            conv = context['conversation']
            if (owner is None or source['path'] != '/native-sdk/chat-coverage'
                    or source['transport'] != 'capability' or raw(fields.get('domain')) != 'chat'
                    or not stable(conv) or encode(conv) not in self.conversations):
                raise CollectorError('CHAT_RESUME_RECORD_MISMATCH')
            self.completed.add(encode(conv))
        if item['domain'] == 'capability' and raw(fields.get('status')) == 'CHAT_DIRECTORY_RECOVERY_ATTEMPT_STARTED':
            conv = context['conversation']
            if (owner is None or source['path'] != '/native-sdk/chat-coverage'
                    or source['transport'] != 'capability' or raw(fields.get('domain')) != 'chat'
                    or not stable(conv) or encode(conv) not in self.conversations):
                raise CollectorError('CHAT_RESUME_RECORD_MISMATCH')
            self.claimed.add(encode(conv))
        self.offset = sequence

    def observe_cipher(self, sequence, cipher):
        pass

    def evidence(self):
        return {'directory_parser': 'index_and_load/2'}

    def finish(self):
        if self.offset != self.total or self.owner is None or not self.conversations:
            raise CollectorError('CHAT_RESUME_NOT_READY')
        return [deepcopy(meta) for key, meta in self.conversations.items()
                if key not in self.completed and key not in self.claimed]


class ChatDirectoryRepairPlan(ChatResumePlan):
    """One-time continuation of directories omitted by a recorded v1 resume.

    A finished run is accepted only here, with a verifiable v1 checkpoint.
    Only load-only directories before that checkpoint, with no committed
    message/diagnostic/attachment evidence at all, can become targets.
    """
    client = 'b-chat-directory-repair/1'

    @staticmethod
    def validate(row, meta):
        eligible(row, meta, directory_repair=True)
        history = meta.get('chat_resumes')
        checkpoint = history[-1] if isinstance(history, list) and history else None
        if (not isinstance(checkpoint, dict) or checkpoint.get('client') != 'b-chat-resume/1'
                or not stable(meta.get('chat_resume_owner'))
                or any(type(checkpoint.get(k)) is not int for k in
                       ('committed_count', 'directory_count', 'completed_count', 'pending_count'))
                or not 0 < checkpoint['committed_count'] <= row[3]
                or not 0 <= checkpoint['completed_count'] < checkpoint['directory_count']
                or checkpoint['pending_count'] != checkpoint['directory_count'] - checkpoint['completed_count']
                or row[1] == 'finished' and meta.get('stop_reason') != 'completed_requested_scope'):
            raise CollectorError('CHAT_DIRECTORY_REPAIR_INELIGIBLE')

    def __init__(self, run, row, meta, prior_timeout=None):
        super().__init__(run, row, meta)
        self.checkpoint = deepcopy(meta['chat_resumes'][-1])
        self.prefix_count = self.checkpoint['committed_count']
        self.prefix_index, self.prefix_load, self.prefix_completed, self.touched = set(), set(), set(), set()
        self.prefix_digest = hashlib.sha256()
        self.cipher_count = 0
        self.coverage_ended = False
        self.prior_timeout = deepcopy(prior_timeout)
        self.prior_timeout_seen = False
        if prior_timeout is not None and (not isinstance(prior_timeout, dict)
                or set(prior_timeout) != {'run_id', 'checkpoint_count', 'exception_sequence', 'prefix_sha256'}
                or prior_timeout['run_id'] != run or prior_timeout['checkpoint_count'] != self.prefix_count
                or type(prior_timeout['exception_sequence']) is not int
                or not 0 < prior_timeout['exception_sequence'] <= self.prefix_count
                or not isinstance(prior_timeout['prefix_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', prior_timeout['prefix_sha256'])):
            raise CollectorError('CHAT_DIRECTORY_REPAIR_MIGRATION_MISMATCH')

    def observe_cipher(self, sequence, cipher):
        if sequence <= self.prefix_count:
            if sequence != self.cipher_count + 1:
                raise CollectorError('CHAT_RESUME_RECORD_MISMATCH')
            self.prefix_digest.update(sequence.to_bytes(8, 'big'))
            self.prefix_digest.update(len(cipher).to_bytes(8, 'big'))
            self.prefix_digest.update(cipher)
            self.cipher_count = sequence

    def feed(self, payload, sequence):
        super().feed(payload, sequence)
        item = payload['observation']; source, context, fields = item['source'], item['context'], item['fields']
        if self.owner != self.meta['chat_resume_owner']:
            raise CollectorError('SOURCE_CHANGED')
        if self.coverage_ended:
            raise CollectorError('CHAT_DIRECTORY_REPAIR_UNRESOLVED')
        directory = item['domain'] == 'chat' and source['path'] in DIRECTORY_PATHS
        if directory:
            if sequence <= self.prefix_count:
                (self.prefix_index if source['path'] == '/native-sdk/chat_index' else self.prefix_load).add(encode(context['conversation']))
            return
        # Treat any native request/message binding as touched, including failed
        # or incomplete histories; this repair is not a retry mechanism.
        bindings = [context['conversation'], fields.get('b_request.conversation_id'),
                    fields.get('conversation.id'), fields.get('conversationId')]
        bound = {encode(v) for v in bindings if stable(v)}
        if item['domain'] == 'chat' and not bound:
            raise CollectorError('CHAT_DIRECTORY_REPAIR_BINDING_UNKNOWN')
        if item['domain'] == 'capability':
            status = raw(fields.get('status'))
            if (status in {'SOURCE_PERMISSION', 'HTTP_401', 'HTTP_403', 'HTTP_429', 'SOURCE_RATE_LIMITED'}
                    or raw(fields.get('b_diagnostic.http_status')) in (401, 403, 429, '401', '403', '429')
                    or {'HTTP_401', 'HTTP_403', 'HTTP_429', 'SOURCE_PERMISSION'} & set(item.get('issues', []))):
                raise CollectorError('CHAT_DIRECTORY_REPAIR_UNRESOLVED')
            if not bound:
                # An explicitly frozen, older transport timeout is retained as
                # unattributed. This is incomplete-history recovery, not proof
                # that the selected targets were never requested previously.
                legacy_timeout = (status == 'RPC_TIMEOUT' and self.prior_timeout is not None
                    and sequence == self.prior_timeout['exception_sequence'] and not self.prior_timeout_seen
                    and source['path'] == '/adapter-capability' and source['transport'] == 'capability'
                    and raw(fields.get('domain')) == 'chat')
                if legacy_timeout:
                    self.prior_timeout_seen = True
                    return
                benign = {'CHAT_LIST_END_UNCONFIRMED', 'CHAT_LIST_LOAD_STALLED',
                          'CHAT_SCROLL_CONTAINER_UNAVAILABLE', 'CHAT_COVERAGE_PARTIAL'}
                if (status not in benign or source['path'] != '/native-sdk/chat-coverage'
                        or source['transport'] != 'capability' or raw(fields.get('domain')) != 'chat'):
                    raise CollectorError('CHAT_DIRECTORY_REPAIR_UNRESOLVED')
                if sequence > self.prefix_count and status == 'CHAT_COVERAGE_PARTIAL':
                    count = self.checkpoint['pending_count']
                    self.coverage_ended = all(type(raw(fields.get(k))) is int and raw(fields[k]) == count
                        for k in ('b_chat.discovered_conversations', 'b_chat.processed_conversations'))
                    if not self.coverage_ended:
                        raise CollectorError('CHAT_DIRECTORY_REPAIR_UNRESOLVED')
        self.touched.update(bound)
        if sequence <= self.prefix_count and raw(fields.get('status')) in COMPLETE:
            self.prefix_completed.add(encode(context['conversation']))

    def finish(self):
        super().finish()
        if not self.coverage_ended:
            raise CollectorError('CHAT_DIRECTORY_REPAIR_UNRESOLVED')
        if self.prior_timeout is not None and (not self.prior_timeout_seen
                or self.prefix_digest.hexdigest() != self.prior_timeout['prefix_sha256']):
            raise CollectorError('CHAT_DIRECTORY_REPAIR_MIGRATION_MISMATCH')
        if (self.cipher_count != self.prefix_count
                or len(self.prefix_index) != self.checkpoint['directory_count']
                or len(self.prefix_completed) != self.checkpoint['completed_count']
                or not self.prefix_completed <= self.prefix_index):
            raise CollectorError('CHAT_DIRECTORY_REPAIR_CHECKPOINT_MISMATCH')
        omitted = self.prefix_load - self.prefix_index
        return [deepcopy(meta) for key, meta in self.conversations.items()
                if key in omitted and key not in self.touched and key not in self.completed]

    def evidence(self):
        targets = self.finish()
        return {**super().evidence(), 'repair_basis': 'v1_omitted_load_directory_only',
                'original_prefix_count': self.prefix_count, 'original_prefix_sha256': self.prefix_digest.hexdigest(),
                'omitted_directory_count': len(self.prefix_load - self.prefix_index),
                'excluded_touched_count': len((self.prefix_load - self.prefix_index) & self.touched),
                'old_inflight_target_unknown': self.prior_timeout_seen,
                'prior_timeout_sequence': self.prior_timeout['exception_sequence'] if self.prior_timeout_seen else None,
                'target_set_sha256': hashlib.sha256(encode(targets)).hexdigest()}


class ChatResumeMixin:
    def resume_scan_page(self, run, plan=None, *, directory_repair=False, prior_timeout=None, reviewed_approval=None):
        self._guard()
        self.db.execute('BEGIN')
        try:
            row, meta = self._run(run)
            from .chat_reviewed_resume import ChatReviewedRecoveryPlan
            if directory_repair and reviewed_approval is not None: raise CollectorError('CHAT_RESUME_CHANGED')
            plan_type = ChatReviewedRecoveryPlan if reviewed_approval is not None else ChatDirectoryRepairPlan if directory_repair else ChatResumePlan
            plan_type.validate(row, meta)
            if plan is None:
                plan = (plan_type(run, row, meta, reviewed_approval) if reviewed_approval is not None else
                        plan_type(run, row, meta, prior_timeout) if directory_repair else plan_type(run, row, meta))
            if type(plan) is not plan_type or plan.run != run or plan.row != row or plan.meta != meta:
                raise CollectorError('CHAT_RESUME_CHANGED')
            if directory_repair and plan.prior_timeout != prior_timeout:
                raise CollectorError('CHAT_RESUME_CHANGED')
            if reviewed_approval is not None and plan.approval != reviewed_approval: raise CollectorError('CHAT_RESUME_CHANGED')
            initial, used = plan.offset, 0
            for sequence, cipher in self.db.execute('SELECT sequence,cipher FROM observations WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT 250', (run, initial)):
                if used >= 8 * 1024 * 1024:
                    break
                plan.feed(self._decrypt(cipher), sequence)
                plan.observe_cipher(sequence, cipher)
                used += len(cipher)
            if plan.offset == initial and initial < plan.total:
                raise CollectorError('CHAT_RESUME_RECORD_MISMATCH')
            plan.validate(row, meta)
            return plan
        finally:
            self.db.rollback()

    def resume_open(self, plan, owner):
        targets = plan.finish()
        if owner != plan.owner or not stable(owner):
            raise CollectorError('SOURCE_CHANGED')
        if not targets:
            raise CollectorError('CHAT_RESUME_NO_TARGETS')
        with self.transaction():
            row, meta = self._run(plan.run)
            plan.validate(row, meta)
            if row != plan.row or meta != plan.meta:
                raise CollectorError('CHAT_RESUME_CHANGED')
            if self.db.execute("SELECT 1 FROM runs WHERE state='running'").fetchone():
                raise CollectorError('TASK_ALREADY_RUNNING')
            evidence = plan.evidence()
            unresolved = evidence.get('unresolved_history_count', len(plan.claimed - plan.completed))
            previous = {'client': plan.client, 'at': now(), 'prior_state': row[1],
                        'prior_finished_at': meta['finished_at'], 'prior_stop_reason': meta['stop_reason'],
                        'committed_count': plan.total, 'directory_count': len(plan.conversations),
                        'completed_count': len(plan.completed), 'pending_count': len(targets),
                        'unresolved_history_count': unresolved, **evidence}
            meta.setdefault('chat_resumes', []).append(previous)
            meta['chat_resume_owner'] = deepcopy(plan.owner)
            # Preserve started_at, scope and the original absolute expires_at.
            meta.update(finished_at=None, stop_reason=None)
            generation = uuid.uuid4().hex
            self.db.execute("UPDATE runs SET generation=?,state='running',cipher=? WHERE id=?",
                            (generation, self._encrypt(meta), plan.run))
        return {'run_id': plan.run, 'generation': generation, 'scope': meta['scope'],
                'expires_at': meta['expires_at'], 'owner': plan.owner, 'total': plan.total,
                'target_count': len(targets), 'completed_count': len(plan.completed),
                'directory_count': len(plan.conversations), 'unresolved_media_count': len(plan.media_gaps),
                'unresolved_history_count': unresolved, 'client': plan.client}
