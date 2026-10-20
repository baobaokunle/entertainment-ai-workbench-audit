"""Unwired sequential host coordinator; no browser, CLI or approval issuer.

The future private-pipe adapter supplies reviewed start bytes, actual source
attestation callbacks and one-attempt media downloads. Each call is bounded to
one source page or one media item, never an entire conversation's downloads.
Cache/entry coverage is still unverified; this coordinator cannot claim a
complete day or activate an existing interrupted child run.
"""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import uuid

from .security import CollectorError
from .chat_today_start import ReviewedTodayStart, native_matches
from .chat_today_journal import TodayJournal, require_lock
from .chat_today_materialize import InheritanceIndex, materialize_page
from .chat_today_media import TodayMedia


class TodayRuntime:
    def __init__(self, starter, store, lock, *, verify_code, verify_native, prepared_snapshot=None):
        self.blocked = False; self.closed = False
        self.store, self.lock = store, lock
        self.verify_code, self.verify_native = verify_code, verify_native
        self.phase = 'opening'; self.page = None; self.tokens = set()
        self.started = None
        try:
            if not isinstance(starter, ReviewedTodayStart):
                raise CollectorError('CHAT_TODAY_REVIEW_REQUIRED')
            contract, native, _ = starter._validate()
            self.codes, self.native = deepcopy(contract['code_sha256']), deepcopy(native)
            self.started = starter.start(store, lock, verify_code=verify_code, verify_native=verify_native,
                                         prepared_snapshot=prepared_snapshot)
            self.journal = TodayJournal(store, lock, self.started['run_id'], self.started['generation'],
                                        self.started['meta_sha256'])
            self.index = prepared_snapshot.new_index(self.journal) if prepared_snapshot is not None else InheritanceIndex(self.journal)
            self.media = TodayMedia(self.journal, self.index)
            self.targets = frozenset(self.started['target_ids'])
            self.writer = uuid.uuid4().hex
            with store.transaction():
                self._source_check()
                store.db.execute('''CREATE TABLE IF NOT EXISTS chat_today_runtime(
                    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,cipher BLOB NOT NULL)''')
                body = {'run_id': self.journal.run, 'generation': self.journal.generation,
                        'writer': self.writer, 'state': 'ready', 'page_id': None}
                cipher = store._encrypt(body)
                store.db.execute('INSERT INTO chat_today_runtime VALUES(?,?,?)', (self.journal.run,'ready',cipher))
            self.phase = 'ready'; self.runtime_pin = hashlib.sha256(cipher).hexdigest()
        except Exception:
            self.blocked = True
            # The returned creation receipt is the cleanup handle even when
            # journal/index/runtime initialization never completed.
            if self.started is not None:
                try:
                    require_lock(store,lock)
                    row,_ = store._run(self.started['run_id'],self.started['generation'])
                    if (row[1] != 'running' or row[3] != 0 or
                            hashlib.sha256(row[2]).hexdigest() != self.started['meta_sha256']):
                        raise CollectorError('CHAT_TODAY_META_CHANGED')
                    store.finish(self.started['run_id'],self.started['generation'],'disconnected')
                    self.closed = True
                except Exception:
                    raise CollectorError('CHAT_TODAY_OPEN_CLEANUP_REVIEW_REQUIRED') from None
                raise CollectorError('CHAT_TODAY_OPEN_FAILED_CHILD_CLOSED') from None
            # A failed atomic start has no owned child. Never reuse its approval.
            raise CollectorError('CHAT_TODAY_RUNTIME_OPEN_REVIEW_REQUIRED') from None

    def _source_check(self):
        self.index.check()
        if self.verify_code() != self.codes or not native_matches(self.verify_native(), self.native):
            raise CollectorError('CHAT_TODAY_SOURCE_OR_CODE_CHANGED')
        # Callbacks may take time or reveal changes; recheck the local pins and
        # absolute deadline before handing a request back to the caller.
        self.index.check()

    def _check(self, phase):
        if self.blocked or self.closed or self.phase != phase:
            raise CollectorError('CHAT_TODAY_RUNTIME_REVIEW_REQUIRED')
        self._source_check()
        row = self.store.db.execute('SELECT state,cipher FROM chat_today_runtime WHERE run_id=?',
                                    (self.journal.run,)).fetchone()
        if not row or row[0] != phase or hashlib.sha256(row[1]).hexdigest() != self.runtime_pin:
            raise CollectorError('CHAT_TODAY_RUNTIME_CHANGED')

    def _move(self, phase, page=None):
        body = {'run_id': self.journal.run, 'generation': self.journal.generation,
                'writer': self.writer, 'state': phase, 'page_id': page}
        with self.store.transaction():
            self._source_check()
            row = self.store.db.execute('SELECT cipher FROM chat_today_runtime WHERE run_id=?',
                                        (self.journal.run,)).fetchone()
            if not row or hashlib.sha256(row[0]).hexdigest() != self.runtime_pin:
                raise CollectorError('CHAT_TODAY_RUNTIME_CHANGED')
            cipher = self.store._encrypt(body)
            self.store.db.execute('UPDATE chat_today_runtime SET state=?,cipher=? WHERE run_id=?',
                                  (phase,cipher,self.journal.run))
        self.phase, self.page = phase, page
        self.runtime_pin = hashlib.sha256(cipher).hexdigest()

    @contextmanager
    def _operation(self):
        try:
            yield
        except Exception:
            self.blocked = True
            # Do not copy untrusted SDK, callback or local exception text into logs.
            raise CollectorError('CHAT_TODAY_RUNTIME_REVIEW_REQUIRED') from None

    def claim(self, target):
        """Private request only: durable claim precedes the caller's source read."""
        with self._operation():
            self._check('ready')
            if target not in self.targets: raise CollectorError('CHAT_TODAY_TARGETS')
            self._move('claiming')
            request = self.journal.claim(target)
            self._move('reading', request['page_id'])
            return request  # Private pipe/memory only, never ordinary progress logs.

    def commit_page(self, page_id, response):
        """Commit one page and materialize it; media is processed separately."""
        with self._operation():
            self._check('reading')
            if page_id != self.page: raise CollectorError('CHAT_TODAY_CLAIM_REQUIRED')
            self._move('persisting',page_id)
            self.journal.commit(page_id,response)
            result = materialize_page(self.journal,self.index,page_id)
            tokens = self.media.prepare_page(page_id)
            self.tokens = set(tokens)
            self._move('media',page_id)
            return {**result, 'media_tokens': tokens, 'source_requests': 0,
                    'entry_cache_coverage_verified': False, 'window_complete': False}

    def process_media(self, token, *, downloader):
        """One bounded media item; no following source claim until the page drains."""
        with self._operation():
            self._check('media')
            if token not in self.tokens: raise CollectorError('CHAT_TODAY_MEDIA_TARGET')
            self._move('downloading', self.page)
            # Check source identity before AND after the injected operation.
            # A change after download prevents asset attachment in TodayMedia.
            def checked_download(*args, **kwargs):
                self._source_check()
                try:
                    return downloader(*args, **kwargs)
                finally:
                    self._source_check()
            result = self.media.process(token,downloader=checked_download)
            self._move('media',self.page)
            self.tokens.remove(token)
            return result

    def advance(self):
        with self._operation():
            self._check('media')
            if self.tokens: raise CollectorError('CHAT_TODAY_MEDIA_PENDING')
            self.media.release_page(self.page)
            self._move('ready')
            return {'ready': True, 'window_complete': False, 'entry_cache_coverage_verified': False}

    def source_failure(self, page_id, code):
        """Only fixed transport/status categories, never SDK response bodies."""
        with self._operation():
            self._check('reading')
            if page_id != self.page or code not in {
                    'RPC_TIMEOUT','SOURCE_TIMEOUT','SOURCE_CHANGED','CHAT_NATIVE_STATUS_FAILED',
                    'CHAT_NATIVE_CONTRACT_CHANGED','CHAT_SOURCE_PERMISSION','CHAT_SOURCE_RATE_LIMIT',
                    'CONVERSATION_UNAVAILABLE','CHAT_NATIVE_RESPONSE_NOT_SUCCESS','SOURCE_READ_FAILED'}:
                raise CollectorError('CHAT_TODAY_DIAGNOSTIC')
            self.journal.record_failure(page_id,code)
            self._move('failed',page_id)
            self.blocked = True
            return {'state': 'failed', 'local_verification_required': True, 'window_complete': False}

    def close(self):
        """Stop only this owned child; never complete the full requested day.

        Cleanup intentionally requires lock/generation/meta ownership, but does
        not depend on a live browser or unchanged source code. No lock is
        released here; the future host must keep it until cleanup succeeds.
        """
        if self.closed: raise CollectorError('CHAT_TODAY_RUNTIME_CLOSED')
        self.blocked = True
        try:
            require_lock(self.store,self.lock)
            row,_ = self.store._run(self.journal.run,self.journal.generation)
            if row[1] != 'running' or hashlib.sha256(row[2]).hexdigest() != self.journal.expected_meta_sha256:
                raise CollectorError('CHAT_TODAY_META_CHANGED')
            targets = dict(self.store.db.execute('SELECT state,count(*) FROM chat_today_targets WHERE run_id=? GROUP BY state',
                                                 (self.journal.run,)))
            pages = dict(self.store.db.execute('SELECT state,count(*) FROM chat_today_pages WHERE run_id=? GROUP BY state',
                                               (self.journal.run,)))
            media_unknown = self.store.db.execute("SELECT count(*) FROM chat_today_media_receipts WHERE run_id=? AND state='reading'",
                                                  (self.journal.run,)).fetchone()[0]
            result = self.store.finish(self.journal.run,self.journal.generation,'disconnected')
            self.closed = True
            return {**result,'target_states':targets,'page_states':pages,'media_inflight_unknown':media_unknown,
                    'pipeline_phase_before_close':self.phase,'local_page_unfinished':self.phase != 'ready',
                    'scope_result':'partial_requested_scope','window_complete':False,'media_complete':False,
                    'entry_cache_coverage_verified':False,'local_verification_required':True}
        except Exception:
            raise CollectorError('CHAT_TODAY_RUNTIME_CLOSE_REVIEW_REQUIRED') from None
