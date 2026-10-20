"""Offline candidate for durable source-page claims and encrypted checkpoints.

No browser, download, run creation, approval issuer or production dispatcher.
This is a staging journal, not delivered observations or saved media. A future
approved host must materialize committed pages without rereading the source.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import uuid

from .chat_today_window import TodayWindow, TodayPageCheckpoint, BJ
from .chat_resume import CONVERSATION_FIELDS
from .contract import encode, scalar, validate_scope, KEY, FORBIDDEN
from .host import ExclusiveHost
from .security import CollectorError
from .store import uid, now

FORMAT = 'b-chat-today-journal/1'


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def require_lock(store, lock):
    if (not isinstance(lock, ExclusiveHost) or not lock.handle or lock.handle.closed
            or Path(lock.handle.name).resolve() != (store.root / 'host.lock').resolve()):
        raise CollectorError('HOST_LOCK_REQUIRED')


class TodayJournal:
    """Caller supplies an independently approved, already created new-day run.

    Metadata pins and parent checks are necessary integrity checks, not proof
    of authorization. Only fictional tests instantiate this candidate today.
    All source identifiers, row projections and cursor values stay encrypted.
    """
    def __init__(self, store, lock, run, generation, expected_meta_sha256):
        self.store, self.lock = store, lock
        self.run, self.generation = uid(run), uid(generation)
        self.expected_meta_sha256 = expected_meta_sha256
        self.writer_instance = uuid.uuid4().hex
        self.attempted_commits = set()
        self.meta, self.policy, self.window = self._check()

    def _check(self):
        require_lock(self.store, self.lock)
        self.store._guard()
        row, meta = self.store._run(self.run, self.generation, active=True)
        if hashlib.sha256(row[2]).hexdigest() != self.expected_meta_sha256:
            raise CollectorError('CHAT_TODAY_META_CHANGED')
        policy = meta.get('chat_today')
        if (not isinstance(policy, dict) or set(policy) - {'entry_kind'} != {
                'format', 'window', 'owner', 'targets_sha256', 'page_budget', 'parent'}
                or policy['format'] != FORMAT):
            raise CollectorError('CHAT_TODAY_POLICY')
        if policy.get('entry_kind','history_only') not in {'history_only','cache_only'}:
            raise CollectorError('CHAT_TODAY_POLICY')
        scalar(policy['owner'])
        if policy['owner']['type'] != 'string' or policy['owner']['lossless'] is not True or not policy['owner']['value']:
            raise CollectorError('OWNER_UNAVAILABLE')
        parent = policy['parent']
        if not isinstance(parent, dict) or set(parent) != {'run_id', 'generation', 'state', 'records', 'meta_sha256'}:
            raise CollectorError('CHAT_TODAY_PARENT_CHANGED')
        if parent['run_id'] == self.run:
            raise CollectorError('CHAT_TODAY_PARENT_CHANGED')
        pr, pm = self.store._run(uid(parent['run_id']))
        if (pr[1] not in {'failed', 'disconnected', 'finished'}
                or (pr[0], pr[1], pr[3], hashlib.sha256(pr[2]).hexdigest()) != (
                    parent['generation'], parent['state'], parent['records'], parent['meta_sha256'])):
            raise CollectorError('CHAT_TODAY_PARENT_CHANGED')
        if pm.get('chat_resume_owner') != policy['owner']:
            raise CollectorError('SOURCE_CHANGED')
        window = TodayWindow.parse(policy['window'], now=datetime.now(timezone.utc), parent_expiry=pm['expires_at'])
        if datetime.fromisoformat(meta['expires_at']) != window.expires:
            raise CollectorError('CHAT_TODAY_EXPIRY_CHANGED')
        scope = meta['scope']; validate_scope(scope)
        if (set(scope['domains']) != {'chat', 'account', 'capability'} or scope['include_body'] is not True
                or scope['start_date'] != window.start.astimezone(BJ).date().isoformat()
                or scope['end_date'] != scope['start_date']
                or type(policy['page_budget']) is not int or not 1 <= policy['page_budget'] <= 100):
            raise CollectorError('CHAT_TODAY_POLICY')
        if self.store.db.execute("SELECT 1 FROM runs WHERE state='running' AND id<>?", (self.run,)).fetchone():
            raise CollectorError('TASK_ALREADY_RUNNING')
        return meta, policy, window

    def install(self, conversations):
        """Persist only a previously reviewed target set; never scan a website."""
        with self.store.transaction():
            return self._install_in_transaction(conversations)

    def _install_in_transaction(self, conversations):
        """Used by the separate reviewed starter for all-or-nothing creation."""
        if not self.store.db.in_transaction:
            raise CollectorError('CHAT_TODAY_TRANSACTION_REQUIRED')
        if not isinstance(conversations, list) or not 1 <= len(conversations) <= 10000:
            raise CollectorError('CHAT_TODAY_TARGETS')
        # Keep the same validation and insertion path for standalone fixtures
        # and the candidate atomic starter; never open a nested transaction.
        if self.store.db.in_transaction:
            _, policy, window = self._check()
            if digest(conversations) != policy['targets_sha256']:
                raise CollectorError('CHAT_TODAY_TARGETS')
            seen = set()
            for conv in conversations:
                if not isinstance(conv, dict) or set(conv) - set(CONVERSATION_FIELDS):
                    raise CollectorError('CHAT_TODAY_TARGETS')
                for value in conv.values(): scalar(value)
                TodayPageCheckpoint(window, policy['owner'], conv, page_budget=policy['page_budget'])
                key = (conv['id']['type'], conv['id']['value'], conv['id']['lossless'])
                if key in seen: raise CollectorError('CHAT_TODAY_TARGETS')
                seen.add(key)
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS chat_today_targets(
                id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                state TEXT NOT NULL,cipher BLOB NOT NULL)''')
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS chat_today_pages(
                id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                target_id TEXT NOT NULL REFERENCES chat_today_targets(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,state TEXT NOT NULL,cipher BLOB NOT NULL,
                UNIQUE(target_id,ordinal))''')
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS chat_today_page_blocks(
                page_id TEXT NOT NULL REFERENCES chat_today_pages(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,cipher BLOB NOT NULL,PRIMARY KEY(page_id,position))''')
            if self.store.db.execute('SELECT 1 FROM chat_today_targets WHERE run_id=?', (self.run,)).fetchone():
                raise CollectorError('CHAT_TODAY_ALREADY_INSTALLED')
            ids = []
            for conv in conversations:
                identifier = uuid.uuid4().hex; ids.append(identifier)
                state = {'format': FORMAT, 'run_id': self.run, 'target_id': identifier,
                         'owner': policy['owner'], 'conversation': deepcopy(conv),
                         'cursor': None, 'cursors': [], 'pages': 0, 'unknown': 0,
                         'source_ended': False, 'materialization_complete': False}
                self.store.db.execute('INSERT INTO chat_today_targets VALUES(?,?,?,?)',
                                      (identifier, self.run, 'pending', self.store._encrypt(state)))
            self._check()
        return ids

    def _target(self, target):
        row = self.store.db.execute('SELECT state,cipher FROM chat_today_targets WHERE id=? AND run_id=?',
                                    (uid(target), self.run)).fetchone()
        if not row: raise CollectorError('CHAT_TODAY_TARGETS')
        body = self.store._decrypt(row[1])
        if (body.get('format') != FORMAT or body.get('run_id') != self.run or body.get('target_id') != target
                or body.get('owner') != self.policy['owner']):
            raise CollectorError('CHAT_TODAY_JOURNAL_CHANGED')
        return row[0], body

    def claim(self, target):
        """Commit before any history request; a surviving reading claim is unknown."""
        with self.store.transaction():
            self._check()
            if self.store.db.execute("SELECT 1 FROM chat_today_targets WHERE run_id=? AND state IN ('reading','failed')",
                                     (self.run,)).fetchone():
                raise CollectorError('CHAT_TODAY_REVIEW_REQUIRED')
            state, body = self._target(target)
            if state != 'pending' or body['pages'] >= self.policy['page_budget'] or body['source_ended']:
                raise CollectorError('CHAT_TODAY_NOT_PENDING')
            op = uuid.uuid4().hex
            request = {'format': FORMAT, 'run_id': self.run, 'target_id': target, 'page_id': op,
                       'writer_instance': self.writer_instance,
                       'owner': self.policy['owner'], 'conversation': body['conversation'],
                       'requested_cursor': body['cursor'], 'entry_kind':self.policy.get('entry_kind','history_only'),
                       'ordinal': body['pages'] + 1, 'claimed_at': now()}
            self.store.db.execute('INSERT INTO chat_today_pages VALUES(?,?,?,?,?,?)',
                                  (op, self.run, target, request['ordinal'], 'reading', self.store._encrypt(request)))
            self.store.db.execute("UPDATE chat_today_targets SET state='reading' WHERE id=?", (target,))
            self._check()
        return deepcopy(request)

    def _consume_commit(self, page_id):
        """Separate durable fence survives rollback of the later page transaction."""
        with self.store.transaction():
            self._check()
            row = self.store.db.execute("SELECT target_id,cipher FROM chat_today_pages WHERE id=? AND run_id=? AND state='reading'",
                                        (uid(page_id), self.run)).fetchone()
            if (not row or self._target(row[0])[0] != 'reading'
                    or self.store._decrypt(row[1]).get('writer_instance') != self.writer_instance):
                raise CollectorError('CHAT_TODAY_CLAIM_REQUIRED')
            self.store.db.execute("UPDATE chat_today_pages SET state='committing' WHERE id=?", (page_id,))
            self._check()

    def commit(self, page_id, response):
        """Atomically save scoped projections, pending-media receipt, and cursor.

        Media has NOT been downloaded here. Its durable pending classification
        is part of the page envelope. Unknown-time rows are quarantined, never
        classified as today's messages. Do not resend after a missing ACK.
        """
        # Even if consuming the durable fence itself fails, this live writer
        # cannot submit again. A new writer cannot take over the old claim.
        uid(page_id)
        if page_id in self.attempted_commits: raise CollectorError('CHAT_TODAY_CLAIM_REQUIRED')
        self.attempted_commits.add(page_id)
        self._consume_commit(page_id)
        with self.store.transaction():
            self._check()
            page = self.store.db.execute('SELECT target_id,ordinal,state,cipher FROM chat_today_pages WHERE id=? AND run_id=?',
                                         (uid(page_id), self.run)).fetchone()
            if not page or page[2] != 'committing': raise CollectorError('CHAT_TODAY_CLAIM_REQUIRED')
            request = self.store._decrypt(page[3]); state, body = self._target(page[0])
            if (state != 'reading' or request.get('page_id') != page_id or request.get('run_id') != self.run
                    or request.get('target_id') != page[0] or request.get('ordinal') != page[1]
                    or request.get('owner') != self.policy['owner'] or request.get('conversation') != body['conversation']
                    or request.get('requested_cursor') != body['cursor'] or page[1] != body['pages'] + 1):
                raise CollectorError('CHAT_TODAY_JOURNAL_CHANGED')
            if request.get('entry_kind') != self.policy.get('entry_kind','history_only'):
                raise CollectorError('CHAT_TODAY_JOURNAL_CHANGED')
            checkpoint = TodayPageCheckpoint(self.window, self.policy['owner'], body['conversation'], page_budget=self.policy['page_budget'],entry_kind=self.policy.get('entry_kind','history_only'))
            checkpoint.cursor, checkpoint.pages = body['cursor'], body['pages']
            checkpoint.cursors, checkpoint.unknown = set(body['cursors']), body['unknown']
            # Only typed source field projections are retained, not raw network responses.
            if not isinstance(response, dict) or len(encode(response)) > 8 * 1024 * 1024:
                raise CollectorError('CHAT_TODAY_PAGE_SIZE')
            if not isinstance(response.get('rows'), list) or len(response['rows']) > 100:
                raise CollectorError('CHAT_TODAY_PAGE_SIZE')
            for fields in response['rows']:
                if not isinstance(fields, dict) or len(fields) > 160: raise CollectorError('FIELDS_SIZE')
                for name, value in fields.items():
                    if not isinstance(name, str) or not KEY.fullmatch(name) or FORBIDDEN.search(name):
                        raise CollectorError('FIELD_REJECTED')
                    scalar(value)
                if len(encode(fields)) > 256 * 1024: raise CollectorError('OBSERVATION_SIZE')
            routed = checkpoint.stage(response, requested_cursor=request['requested_cursor'], now=datetime.now(timezone.utc))
            status = checkpoint.after_commit(routed['checkpoint_after_commit']['token'])
            projections = ([{'kind': 'selected', 'fields': f} for f in routed['selected_rows']]
                           + [{'kind': 'unresolved', 'fields': f} for f in routed['unresolved_rows']])
            # DPAPI caps each protected object at 4 MiB. Each checked row is at
            # most 256 KiB; blocks and the small manifest commit together.
            for position, projection in enumerate(projections):
                block = {'format': FORMAT, 'run_id': self.run, 'page_id': page_id,
                         'position': position, **projection}
                self.store.db.execute('INSERT INTO chat_today_page_blocks VALUES(?,?,?)',
                                      (page_id, position, self.store._encrypt(block)))
            result = {**request, 'committed_at': now(), 'counts': routed['counts'],
                      'projection_blocks': len(projections), 'projection_sha256': digest(projections),
                      'source_evidence_sha256': routed['checkpoint_after_commit']['response_sha256'],
                      'media_receipt': {'state': 'classification_pending', 'selected_rows': len(routed['selected_rows']),
                                        'saved_assets': 0, 'unresolved_rows_download_allowed': False},
                      'materialization_complete': False, 'checkpoint': status}
            body.update(cursor=checkpoint.cursor, cursors=sorted(checkpoint.cursors), pages=checkpoint.pages,
                        unknown=checkpoint.unknown, source_ended=checkpoint.source_ended)
            next_state = 'source_ended' if checkpoint.source_ended else ('budget' if status['budget_partial'] else 'pending')
            self.store.db.execute("UPDATE chat_today_pages SET state='committed',cipher=? WHERE id=?",
                                  (self.store._encrypt(result), page_id))
            self.store.db.execute('UPDATE chat_today_targets SET state=?,cipher=? WHERE id=?',
                                  (next_state, self.store._encrypt(body), page[0]))
            self._check()  # Expiry/parent/generation/lock checked again before commit.
        return {'committed_pages': checkpoint.pages, 'selected': routed['counts']['selected'],
                'unresolved': routed['counts']['unknown_time_or_key'], 'source_ended': checkpoint.source_ended,
                'window_complete': False, 'materialization_complete': False, 'media_saved': 0}

    def record_failure(self, page_id, code):
        """Preserve a fixed diagnostic and stop the whole logical queue."""
        import re
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code):
            raise CollectorError('CHAT_TODAY_DIAGNOSTIC')
        with self.store.transaction():
            self._check()
            row = self.store.db.execute("SELECT target_id,cipher FROM chat_today_pages WHERE id=? AND run_id=? AND state IN ('reading','committing')",
                                        (uid(page_id), self.run)).fetchone()
            if not row: raise CollectorError('CHAT_TODAY_CLAIM_REQUIRED')
            receipt = self.store._decrypt(row[1]); receipt.update(failed_at=now(), code=code)
            self.store.db.execute("UPDATE chat_today_pages SET state='failed',cipher=? WHERE id=?", (self.store._encrypt(receipt), page_id))
            self.store.db.execute("UPDATE chat_today_targets SET state='failed' WHERE id=?", (row[0],))
            self._check()
