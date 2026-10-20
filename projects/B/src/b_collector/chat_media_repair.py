"""One attempt per saved failed image, without a browser or history recollection.

The caller holds the independent chat ExclusiveHost lock. The history run stays
stopped. Media blocks, attachment observation and receipt commit atomically.
"""
import base64
from copy import deepcopy
import hashlib
import json
import re
import uuid

from .chat_resume import ChatResumePlan, eligible
from .chat_store import message_key, value
from .chat_incremental import scalar
from .chat_download import destination, download_image
from .contract import encode, validate_observation
from .security import CollectorError
from .store import now

CLIENT = 'b-chat-media-repair/1'
IDENTITY_FIELDS = ('serverId', 'clientId', 'id', 'conversationId', 'conversationShortId',
                   'sender', 'senderUserId', 'secSender', 'type', 'createTime',
                   'b_chat.created_at_iso', 'b_chat.created_at_basis', 'b_request.conversation_id')
STOP_CODES = {'MEDIA_ACCESS_DENIED', 'MEDIA_RATE_LIMIT'}
RECOVERABLE_CODES = {'MEDIA_HTTP_FAILED', 'MEDIA_TIMEOUT', 'MEDIA_DOWNLOAD_FAILED',
                     'MEDIA_SIZE_LIMIT', 'MEDIA_NOT_IMAGE', 'MEDIA_ENCODING_REJECTED',
                     'MEDIA_TRUNCATED', 'MEDIA_TYPE_MISMATCH', 'MEDIA_REDIRECT_REJECTED',
                     'MEDIA_CIPHER_SIZE', 'MEDIA_DECRYPT_FAILED'}


def original_cipher_digest(store, run, count):
    """Freeze the original prefix, not later attachment records. No decryption."""
    digest = hashlib.sha256(); seen = 0
    for identifier, sequence, cipher in store.db.execute(
            'SELECT id,sequence,cipher FROM observations WHERE run_id=? AND sequence<=? ORDER BY sequence', (run, count)):
        seen += 1
        if sequence != seen: raise CollectorError('MEDIA_REPAIR_SCOPE_CHANGED')
        for part in (run.encode('ascii'), identifier.encode('ascii'), str(sequence).encode('ascii'), cipher):
            digest.update(len(part).to_bytes(8, 'big')); digest.update(part)
    if seen != count: raise CollectorError('MEDIA_REPAIR_SCOPE_CHANGED')
    return digest.hexdigest()


def check_original_binding(store, run, proof):
    if run != proof.get('expected_run_id') or original_cipher_digest(store, run, proof['expected_original_records']) != proof.get('original_cipher_sha256'):
        raise CollectorError('MEDIA_REPAIR_SCOPE_CHANGED')


def image_source(fields):
    """Only the same saved message's known image slots; no avatar/video URLs."""
    if value(fields.get('b_chat.media_kind')) != 'image' or value(fields.get('b_chat.media_status')) != 'MEDIA_NOT_IMAGE':
        return None
    try:
        payload = json.loads(value(fields.get('content')) or '{}')
        if not isinstance(payload, dict) or payload.get('aweType') not in (2702, 2703, 2704): return None
        resource = payload.get('resource_url')
        if not isinstance(resource, dict): return None
        source_key = resource.get('skey')
        if not isinstance(source_key, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', source_key): return None
        urls = []
        for slot in ('origin_url_list', 'large_url_list', 'medium_url_list', 'thumb_url_list'):
            for url in resource.get(slot, []) if isinstance(resource.get(slot), list) else []:
                if isinstance(url, str) and url not in urls and len(urls) < 3:
                    destination(url)  # Syntax/CDN validation only, no network.
                    urls.append(url)
        return {'urls': urls, 'source_key': source_key} if urls else None
    except (ValueError, TypeError):
        return None


class MediaRepair:
    def __init__(self, store, run):
        self.store, self.run = store, run
        self.targets = []; self.existing = {}; self.attempted_keys = set()
        self.blocked_code = None
        self.row, self.meta = store._run(run)
        eligible(self.row, self.meta)
        if not self.meta['scope']['include_body']: raise CollectorError('MEDIA_SCOPE')
        if store.db.execute("SELECT 1 FROM runs WHERE state='running'").fetchone(): raise CollectorError('TASK_ALREADY_RUNNING')
        self.owner = None
        self.stats = {'eligible_images': 0, 'already_attempted': 0, 'saved': 0, 'failed': 0,
                      'interrupted': 0, 'pending': 0, 'new_saved': 0, 'new_failed': 0, 'new_bytes': 0,
                      'unqualified_failure_observations': 0}
        self._scan()

    def _check(self):
        self.store._guard()
        row, meta = self.store._run(self.run)
        eligible(row, meta)
        if row != self.row or meta != self.meta: raise CollectorError('MEDIA_REPAIR_CHANGED')
        if meta.get('chat_resume_owner') is not None and meta['chat_resume_owner'] != self.owner:
            raise CollectorError('SOURCE_CHANGED')
        if self.store.db.execute("SELECT 1 FROM runs WHERE state='running'").fetchone(): raise CollectorError('TASK_ALREADY_RUNNING')
        binding = self.store.db.execute('SELECT cipher FROM chat_legacy_binding WHERE slot=1').fetchone()
        if binding and self.store._decrypt(binding[0])['owner'] != self.owner:
            raise CollectorError('LEGACY_ACCOUNT_CHANGED')

    def _scan(self):
        plan = ChatResumePlan(self.run, self.row, self.meta)
        candidates, saved = {}, {}
        self.store._guard(); self.store.db.execute('BEGIN')
        try:
            for sequence, cipher in self.store.db.execute('SELECT sequence,cipher FROM observations WHERE run_id=? ORDER BY sequence', (self.run,)):
                p = self.store._decrypt(cipher); plan.feed(p, sequence)
                item = p['observation']; fields, context = item['fields'], item['context']
                if item['domain'] != 'chat': continue
                key = message_key(context['owner'], context['conversation'], fields)
                if not key: continue
                if value(fields.get('b_chat.media_file_saved')) is True:
                    saved[key] = value(fields.get('b_chat.media_asset_id')); continue
                source = image_source(fields)
                if source is None or value(fields.get('b_chat.record_role')) != 'message':
                    if value(fields.get('b_chat.media_kind')) == 'image' and value(fields.get('b_chat.media_status')) == 'MEDIA_NOT_IMAGE':
                        self.stats['unqualified_failure_observations'] += 1
                    continue
                conv = plan.conversations.get(encode(context['conversation']))
                if (conv is None or fields.get('b_request.conversation_id') != context['conversation']
                        or not (fields.get('conversationId') == context['conversation']
                        or fields.get('conversationShortId') is not None and fields.get('conversationShortId') == conv.get('shortId'))):
                    raise CollectorError('MEDIA_BINDING_MISMATCH')
                candidates[key] = {'id': p['observation_id'], 'digest': hashlib.sha256(encode(p)).hexdigest(),
                                   'item': item, 'source': source, 'key': key}
            plan.finish(); self.owner = plan.owner
            # Only repaired candidates need a new integrity check here; do not
            # re-audit unrelated, already verified sticker assets on each call.
            for key in candidates.keys() & saved.keys():
                if not saved[key]: raise CollectorError('MEDIA_NOT_FOUND')
                manifest, _ = self.store.read_media(self.run, saved[key])
                if manifest['message_key'] != base64.b64encode(key).decode('ascii'):
                    raise CollectorError('MEDIA_BINDING_MISMATCH')
            exists = self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_media_repair_attempts'").fetchone()
            if exists:
                for state, cipher in self.store.db.execute('SELECT state,cipher FROM chat_media_repair_attempts WHERE run_id=?', (self.run,)):
                    attempt = self.store._decrypt(cipher)
                    if attempt.get('run_id') != self.run or attempt.get('owner') != self.owner or state not in ('reading', 'saved', 'failed'):
                        raise CollectorError('MEDIA_REPAIR_CHANGED')
                    key = base64.b64decode(attempt['message_key'], validate=True)
                    self.attempted_keys.add(key)
                    if attempt.get('result') in STOP_CODES: self.blocked_code = attempt['result']
                    label = 'interrupted' if state == 'reading' else state
                    self.stats[label] += 1
            self.targets = [t for k, t in candidates.items() if k not in saved and k not in self.attempted_keys]
            self.stats.update(eligible_images=len(candidates), already_attempted=len(self.attempted_keys), pending=len(self.targets))
            self._check()
        finally:
            self.store.db.rollback()

    def _original(self, target):
        row = self.store.db.execute('SELECT cipher FROM observations WHERE id=? AND run_id=?', (target['id'], self.run)).fetchone()
        if row is None: raise CollectorError('MEDIA_REPAIR_CHANGED')
        payload = self.store._decrypt(row[0])
        if hashlib.sha256(encode(payload)).hexdigest() != target['digest']: raise CollectorError('MEDIA_REPAIR_CHANGED')
        item = payload['observation']; context, fields = item['context'], item['fields']
        if (payload['run_id'] != self.run or context['owner'] != self.owner
                or item != target['item'] or image_source(fields) != target['source']
                or message_key(context['owner'], context['conversation'], fields) != target['key']):
            raise CollectorError('MEDIA_BINDING_MISMATCH')

    def _claim(self, target):
        if self.blocked_code: raise CollectorError(self.blocked_code)
        with self.store.transaction():
            self._check(); self._original(target)
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS chat_media_repair_attempts(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
              source_observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
              state TEXT NOT NULL, cipher BLOB NOT NULL, UNIQUE(run_id,source_observation_id))''')
            if self.store.db.execute('SELECT 1 FROM chat_media_repair_attempts WHERE run_id=? AND source_observation_id=?', (self.run, target['id'])).fetchone():
                raise CollectorError('MEDIA_REPAIR_ALREADY_ATTEMPTED')
            op = uuid.uuid4().hex
            receipt = {'client': CLIENT, 'run_id': self.run, 'owner': self.owner,
                       'message_key': base64.b64encode(target['key']).decode('ascii'),
                       'source_observation_id': target['id'], 'source_digest': target['digest'],
                       'claimed_at': now(), 'expires_at': self.meta['expires_at'], 'result': None}
            self.store.db.execute('INSERT INTO chat_media_repair_attempts VALUES(?,?,?,?,?)',
                                 (op, self.run, target['id'], 'reading', self.store._encrypt(receipt)))
        return op, receipt

    def _attachment(self, target, asset):
        original = target['item']
        item = deepcopy(original)
        item['observed_at'] = now()
        item['source'].update(path='/local-media-repair/existing-message', adapter=CLIENT,
                              transport='capability', record_ref='/observations/' + target['id'])
        item['fields'] = {k: deepcopy(original['fields'][k]) for k in IDENTITY_FIELDS if k in original['fields']}
        extra = {'b_chat.record_role': 'media_attachment', 'b_chat.media_kind': 'image',
                 'b_chat.media_file_saved': True, 'b_chat.media_asset_id': asset['asset_id'],
                 'b_chat.media_bytes': asset['bytes'], 'b_chat.media_mime': asset['mime'],
                 'b_chat.media_status': 'encrypted_saved', 'b_chat.media_repair_client': CLIENT,
                 'b_chat.media_repair_source_observation_id': target['id'],
                 'b_chat.source_message_observed_at': original['observed_at']}
        item['fields'].update({k: scalar(v) for k, v in extra.items()})
        item['coverage'] = {'status': 'partial', 'basis': 'stored_message_media_attachment', 'received': 1, 'limit': None, 'terminal': 'unknown'}
        item['issues'] = ['EXISTING_MESSAGE_MEDIA_ATTACHMENT', 'NO_NEW_CHAT_SOURCE_READ', 'MEDIA_LABEL_IS_CANDIDATE']
        validate_observation(item, self.meta['scope'])
        return item

    def attempt(self, target, downloader=download_image):
        if not any(t is target for t in self.targets): raise CollectorError('MEDIA_REPAIR_TARGET')
        op, receipt = self._claim(target)
        try:
            # One original URL per target per repair. No URL guessing, refresh,
            # alternate credentials, or retrying a previously claimed failure.
            url = target['source']['urls'][0]
            data, mime = downloader(url, source_key=target['source']['source_key'])
            with self.store.transaction():
                self._check(); self._original(target)
                asset = self.store._insert_media_bytes(self.run, target['key'], url, 'image', data, mime, True)
                item = self._attachment(target, asset)
                if self.row[3] >= 1000000: raise CollectorError('RUN_RECORD_LIMIT')
                identifier, sequence = uuid.uuid4().hex, self.row[3] + 1
                payload = {'observation_id': identifier, 'run_id': self.run, 'sequence': sequence,
                           'ingested_at': now(), 'observation': item}
                self.store.db.execute('INSERT INTO observations VALUES(?,?,?,?)', (identifier, self.run, sequence, self.store._encrypt(payload)))
                self.store.db.execute('UPDATE runs SET count=count+1 WHERE id=?', (self.run,))
                receipt.update(result='saved', asset_id=asset['asset_id'], attachment_id=identifier, finished_at=now())
                self.store.db.execute("UPDATE chat_media_repair_attempts SET state='saved',cipher=? WHERE id=? AND state='reading'",
                                      (self.store._encrypt(receipt), op))
            self.row, self.meta = self.store._run(self.run)
            self.stats['saved'] += 1; self.stats['new_saved'] += 1; self.stats['new_bytes'] += len(data)
            return 'saved'
        except CollectorError as exc:
            code = str(exc)
            if not re.fullmatch(r'[A-Z_]{1,80}', code): code = 'MEDIA_REPAIR_FAILED'
            with self.store.transaction():
                receipt.update(result=code, finished_at=now())
                self.store.db.execute("UPDATE chat_media_repair_attempts SET state='failed',cipher=? WHERE id=? AND state='reading'",
                                      (self.store._encrypt(receipt), op))
            self.stats['failed'] += 1; self.stats['new_failed'] += 1
            if code in STOP_CODES: self.blocked_code = code
            if code not in RECOVERABLE_CODES:
                raise
            return 'failed'
        except BaseException:
            # The durable claim remains reading/unknown. A later invocation
            # must not resend it, including after a crash or KeyboardInterrupt.
            self.stats['interrupted'] += 1
            raise
        finally:
            self.stats['pending'] -= 1
