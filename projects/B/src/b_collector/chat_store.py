"""Separate chat store. Source values, media manifests and binary chunks are DPAPI encrypted."""
import base64
from datetime import datetime, timezone
import hashlib
import uuid
import zipfile
from .store import Store, uid, now
from .contract import encode, validate_scope, validate_observation
from .security import CollectorError, check_path
from .chat_download import download_image, image_type, MAX_BYTES
from .chat_resume import ChatResumeMixin

CHUNK = 1024 * 1024
DOMAINS = {'chat', 'account', 'capability'}


def value(field):
    return field.get('value') if isinstance(field, dict) and field.get('lossless') is True else None


def message_key(owner, conversation, fields):
    # No ID conversion or global-ID assumption; all parts retain original scalar types.
    if not isinstance(value(owner), str) or not isinstance(value(conversation), str): return None
    sender = fields.get('sender') or fields.get('senderUserId')
    if not isinstance(value(sender), str): return None
    for name in ('serverId', 'clientId', 'id'):
        if isinstance(value(fields.get(name)), str) and value(fields[name]):
            return encode([owner, conversation, name, fields[name], sender])
    return None


class ChatStore(ChatResumeMixin, Store):
    def __init__(self, base, protector=None):
        super().__init__(base, protector)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS chat_media(id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,cipher BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS chat_media_chunks(asset_id TEXT NOT NULL REFERENCES chat_media(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,cipher BLOB NOT NULL,PRIMARY KEY(asset_id,sequence));
          CREATE TABLE IF NOT EXISTS chat_legacy_binding(slot INTEGER PRIMARY KEY CHECK(slot=1),cipher BLOB NOT NULL);
        ''')

    def bind_legacy(self, owner, fingerprint, allow_initial=False):
        if not isinstance(value(owner), str) or owner.get('type') != 'string': raise CollectorError('OWNER_UNAVAILABLE')
        with self.transaction():
            row = self.db.execute('SELECT cipher FROM chat_legacy_binding WHERE slot=1').fetchone()
            if row:
                binding = self._decrypt(row[0])
                if binding['owner'] != owner or binding['source_sha256'] != fingerprint:
                    raise CollectorError('LEGACY_ACCOUNT_CHANGED')
            else:
                if not allow_initial: raise CollectorError('LEGACY_BINDING_REQUIRED')
                binding = {'owner': owner, 'source_sha256': fingerprint, 'bound_at': now(),
                           'basis': 'user_confirmed_same_current_account_2026_10_18', 'platform_scope_verified': False}
                self.db.execute('INSERT INTO chat_legacy_binding VALUES(1,?)', (self._encrypt(binding),))
        return binding

    def start(self, scope):
        validate_scope(scope)
        if set(scope['domains']) - DOMAINS or 'chat' not in scope['domains']:
            raise CollectorError('CHAT_SCOPE_ONLY')
        if self.db.execute("SELECT 1 FROM runs WHERE state='running'").fetchone():
            raise CollectorError('TASK_ALREADY_RUNNING')
        return super().start(scope)

    def append(self, run, generation, batch_id, items):
        _, meta = self._run(run, generation, active=True)
        owner = meta.get('chat_resume_owner')
        if owner is not None and any(x.get('context', {}).get('owner') != owner
                                     for x in items if x.get('domain') != 'capability' or x.get('context', {}).get('owner') is not None):
            raise CollectorError('SOURCE_CHANGED')
        if any(x.get('domain') not in DOMAINS or
               (x.get('domain') == 'capability' and value(x.get('fields', {}).get('domain')) != 'chat')
               or x.get('source', {}).get('origin') != 'https://www.douyin.com' for x in items):
            raise CollectorError('CHAT_SCOPE_ONLY')
        for item in items:
            asset = value(item.get('fields', {}).get('b_chat.media_asset_id'))
            if asset:
                found = self.db.execute('SELECT cipher FROM chat_media WHERE id=? AND run_id=?', (uid(asset), run)).fetchone()
                if not found: raise CollectorError('MEDIA_NOT_FOUND')
                manifest = self._decrypt(found[0])
                if manifest['message_key'] != base64.b64encode(message_key(item['context']['owner'], item['context']['conversation'], item['fields']) or b'').decode():
                    raise CollectorError('MEDIA_BINDING_MISMATCH')
        return super().append(run, generation, batch_id, items)

    def finish(self, run, generation, reason):
        if reason not in {'completed_requested_scope','cancelled','source_changed','disconnected','failed','budget'}:
            raise CollectorError('STOP_REASON')
        with self.transaction():
            row, meta = self._run(run, generation)
            if row[1] != 'running': return {'state': row[1], 'total': row[3]}
            # IPC is sequential: an in-flight transfer can finish before queued cancel.
            # Its bytes must not survive cancellation unless a message was committed.
            assets = {r[0] for r in self.db.execute('SELECT id FROM chat_media WHERE run_id=?',(run,))}
            if assets:
                attached = set()
                for (cipher,) in self.db.execute('SELECT cipher FROM observations WHERE run_id=?',(run,)):
                    item = self._decrypt(cipher)['observation']
                    key = value(item['fields'].get('b_chat.media_asset_id'))
                    if key in assets: attached.add(key)
                for asset in assets - attached:
                    self.db.execute('DELETE FROM chat_media WHERE id=?',(asset,))
            meta.update(finished_at=now(),stop_reason=reason)
            state = 'finished' if reason == 'completed_requested_scope' else reason
            self.db.execute('UPDATE runs SET generation=?,state=?,cipher=? WHERE id=?',
                            (uuid.uuid4().hex,state,self._encrypt(meta),run))
            return {'state':state,'total':row[3]}

    def save_media(self, run, generation, key, url, kind, downloader=download_image, source_key=None):
        self._guard()
        _, meta = self._run(run, generation, active=True)
        if not meta['scope']['include_body'] or kind not in ('image', 'sticker') or not key:
            raise CollectorError('MEDIA_SCOPE')
        # Network response is held in memory, never in a temporary plaintext file.
        data, mime = downloader(url, source_key=source_key) if source_key is not None else downloader(url)
        with self.transaction():
            self._run(run, generation, active=True)  # Reject cancellation/expiry during transfer.
            return self._insert_media_bytes(run, key, url, kind, data, mime, source_key is not None)

    def _insert_media_bytes(self, run, key, url, kind, data, mime, source_encrypted=False):
        """Caller owns a checked run and transaction; useful for atomic attachment repair."""
        if not self.db.in_transaction: raise CollectorError('MEDIA_TRANSACTION_REQUIRED')
        if kind not in ('image', 'sticker') or not key: raise CollectorError('MEDIA_SCOPE')
        if not data or len(data) > MAX_BYTES or image_type(data) != mime:
            raise CollectorError('MEDIA_NOT_IMAGE')
        asset = uuid.uuid4().hex
        digest = hashlib.sha256(data).hexdigest()
        binding = base64.b64encode(key).decode('ascii')
        chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
        manifest = {'format': 'b-chat-media/1', 'asset_id': asset, 'run_id': run,
                    'message_key': binding, 'url': url, 'kind': kind, 'mime': mime,
                    'bytes': len(data), 'sha256': digest, 'chunks': len(chunks), 'saved_at': now(),
                    'source_encoding': 'message_key_aes_gcm' if source_encrypted else 'plain_image'}
        self.db.execute('INSERT INTO chat_media VALUES(?,?,?)', (asset, run, self._encrypt(manifest)))
        for index, chunk in enumerate(chunks):
            payload = {'asset_id': asset, 'run_id': run, 'message_key': binding,
                       'sequence': index, 'bytes': base64.b64encode(chunk).decode('ascii')}
            self.db.execute('INSERT INTO chat_media_chunks VALUES(?,?,?)', (asset, index, self._encrypt(payload)))
        return {'asset_id': asset, 'bytes': len(data), 'mime': mime}

    def read_media(self, run, asset):
        self._guard()
        _, meta = self._run(run)
        if datetime.fromisoformat(meta['expires_at']) <= datetime.now(timezone.utc):
            raise CollectorError('RETENTION_EXPIRED')
        row = self.db.execute('SELECT cipher FROM chat_media WHERE id=? AND run_id=?', (uid(asset), uid(run))).fetchone()
        if not row: raise CollectorError('MEDIA_NOT_FOUND')
        manifest = self._decrypt(row[0]); chunks = []
        if manifest['asset_id'] != asset or manifest['run_id'] != run: raise CollectorError('MEDIA_BINDING_MISMATCH')
        for index, cipher in self.db.execute('SELECT sequence,cipher FROM chat_media_chunks WHERE asset_id=? ORDER BY sequence', (asset,)):
            block = self._decrypt(cipher)
            if index != len(chunks) or any(block[k] != v for k, v in {'sequence': index, 'asset_id': asset, 'run_id': run, 'message_key': manifest['message_key']}.items()):
                raise CollectorError('MEDIA_BINDING_MISMATCH')
            chunks.append(base64.b64decode(block['bytes'], validate=True))
        data = b''.join(chunks)
        if len(chunks) != manifest['chunks'] or len(data) != manifest['bytes'] or hashlib.sha256(data).hexdigest() != manifest['sha256']:
            raise CollectorError('MEDIA_INTEGRITY')
        return manifest, data

    def export(self, run):
        # Independent package format: A must explicitly review it; no automatic import.
        self._guard(); row, meta = self._run(run)
        if row[1] == 'running': raise CollectorError('STOP_BEFORE_EXPORT')
        if datetime.fromisoformat(meta['expires_at']) <= datetime.now(timezone.utc): raise CollectorError('RETENTION_EXPIRED')
        name = uuid.uuid4().hex + '.bcap.zip'
        path = check_path(self.root / name, self.base)
        temp = check_path(path.with_suffix('.partial'), self.base)
        with self.transaction(): self.db.execute('INSERT INTO packages VALUES(?,?)', (name, run))
        try:
            self.db.execute('BEGIN')
            # Validate every asset before advertising a package containing it.
            assets = self.db.execute('SELECT id,cipher FROM chat_media WHERE run_id=?', (run,)).fetchall()
            for asset, _ in assets: self.read_media(run, asset)
            with zipfile.ZipFile(temp, 'x', compression=zipfile.ZIP_STORED) as z:
                z.writestr('manifest.json', encode({'format': 'b-chat-encrypted-package/1', 'records': row[3], 'assets': len(assets), 'source_complete': False, 'formal_A_acceptance': False}))
                z.writestr('run.dpapi', row[2])
                for seq, cipher in self.db.execute('SELECT sequence,cipher FROM observations WHERE run_id=? ORDER BY sequence', (run,)):
                    z.writestr(f'observations/{seq}.dpapi', cipher)
                for asset, cipher in assets:
                    z.writestr(f'media/{asset}/manifest.dpapi', cipher)
                    for seq, block in self.db.execute('SELECT sequence,cipher FROM chat_media_chunks WHERE asset_id=? ORDER BY sequence', (asset,)):
                        z.writestr(f'media/{asset}/{seq}.dpapi', block)
            if datetime.fromisoformat(meta['expires_at']) <= datetime.now(timezone.utc): raise CollectorError('RETENTION_EXPIRED')
            temp.replace(path)
            return {'path': str(path), 'records': row[3], 'assets': len(assets), 'encrypted': True}
        finally:
            self.db.rollback()
            if temp.exists(): temp.unlink()
