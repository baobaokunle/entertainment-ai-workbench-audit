"""Message-scoped overlap and media completion; no display-name matching."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from .chat_store import message_key, value
from .contract import encode
from .security import CollectorError


def scalar(v):
    return {'type': 'boolean' if type(v) is bool else 'number' if type(v) is int else 'string', 'value': v, 'lossless': True}


def timestamp(fields):
    label = value(fields.get('b_chat.created_at_iso'))
    if not isinstance(label, str): return None
    try:
        d = datetime.fromisoformat(label.replace('Z', '+00:00'))
        if d.utcoffset() is None: return None
        return d.timestamp()
    except ValueError: return None


def native_fields(fields):
    return {p: v for p, v in sorted(fields.items()) if not p.startswith(('b_', 'conversation.'))}


class IncrementalChat:
    def __init__(self, store, run, generation, legacy=None, downloader=None, resume_count=None):
        self.store, self.run, self.generation = store, run, generation
        self.legacy = legacy or {'messages': set(), 'latest': {}, 'rejected': 0}
        self.downloader = downloader
        self.seen = {}; self.old = {}; self.old_media = {}; self.media_attempted = set(); self.owner = None
        self.committed_current = set()
        self.stats = {'messages_new': 0, 'duplicates_skipped': 0, 'legacy_duplicates': 0,
                      'media_saved': 0, 'media_failed': 0, 'media_missing_url': 0,
                      'media_attachment_observations': 0, 'before_boundary': 0,
                      'unknown_message_key': 0}
        # Only independent chat-store records; never open the active backend store.
        for key, state, cipher, count in store.db.execute('SELECT id,state,cipher,count FROM runs'):
            current = key == run
            if current and resume_count is None: continue
            if current and (type(resume_count) is not int or not 0 <= resume_count <= count):
                raise CollectorError('CHAT_RESUME_CHANGED')
            meta = store._decrypt(cipher)
            if (state == 'running' and not current) or datetime.fromisoformat(meta['expires_at']) <= datetime.now(timezone.utc): continue
            limit = resume_count if current else count
            for (body,) in store.db.execute('SELECT cipher FROM observations WHERE run_id=? AND sequence<=? ORDER BY sequence', (key, limit)):
                o = store._decrypt(body)['observation']; f = o['fields']; c = o['context']
                if o['domain'] != 'chat': continue
                k = message_key(c['owner'], c['conversation'], f)
                if k and value(f.get('b_chat.record_role')) != 'media_attachment':
                    self.old[k] = {'fields': f, 'expires_at': meta['expires_at']}
                    if current:
                        self.committed_current.add((k, hashlib.sha256(encode(native_fields(f))).digest()))
                # The resume pass only revisits incomplete conversation history.
                # Already attempted media has its own repair path; never retry it
                # implicitly while deduplicating a committed message.
                if current and k and value(f.get('b_chat.media_status')) is not None:
                    self.media_attempted.add(k)
                asset = value(f.get('b_chat.media_asset_id'))
                if k and asset and value(f.get('b_chat.media_file_saved')) is True:
                    found = store.db.execute('SELECT cipher FROM chat_media WHERE id=? AND run_id=?',(asset,key)).fetchone()
                    if found:
                        import base64
                        manifest = store._decrypt(found[0])
                        if manifest.get('message_key') == base64.b64encode(k).decode('ascii'):
                            self.old_media.setdefault(k,[]).append((manifest.get('url'),meta['expires_at'],key,asset))

    def process(self, owner, conversation, rows, urls):
        self.store._run(self.run, self.generation, active=True)
        if self.owner is None: self.owner = owner
        if self.owner != owner: raise CollectorError('SOURCE_CHANGED')
        conv = conversation.get('id'); conv_value = value(conv)
        legacy_bound = self.legacy.get('owner') == owner
        latest = self.legacy['latest'].get(conv_value) if legacy_bound else None
        # Include the entire last saved local calendar day, not only the last second.
        # Unknown per-conversation boundary remains unknown; no global cutoff assumption.
        boundary = None
        if latest is not None:
            day = datetime.fromtimestamp(latest, timezone(timedelta(hours=8)))
            boundary = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        kept = []; old_times = []; seen_on_page = 0
        for f, candidates in zip(rows, urls, strict=True):
            # Only directly bound messages may carry a stored asset or skip a duplicate.
            bound = f.get('conversationId') == conv or (f.get('conversationShortId') is not None and f.get('conversationShortId') == conversation.get('shortId'))
            k = message_key(owner, conv, f) if bound else None; t = timestamp(f)
            kind = value(f.get('b_chat.media_kind'))
            legacy_id = value(f.get('serverId')); sender = value(f.get('sender') or f.get('senderUserId'))
            # The original exporter's timestamp mapping is int(datetime.timestamp()).
            # Preserve the source ISO timestamp; project seconds only for comparison.
            legacy_hit = bool(legacy_bound and k and isinstance(legacy_id, str) and isinstance(sender, str) and t is not None
                              and (conv_value, 'srv_' + legacy_id, sender, int(t)) in self.legacy['messages'])
            old_times.append(t is not None and boundary is not None and t < boundary)
            # Old rows beyond the requested boundary are neither recollected nor treated as missing.
            if old_times[-1]: self.stats['before_boundary'] += 1; continue
            exact = hashlib.sha256(encode(native_fields(f))).digest()
            previous = self.old.get(k)
            local_hit = bool(previous and datetime.fromisoformat(previous['expires_at']) > datetime.now(timezone.utc)
                             and native_fields(previous['fields']) == native_fields(f))
            duplicate = legacy_hit or local_hit or (k, exact) in self.committed_current or (k is not None and self.seen.get(k) == exact)
            if not k: self.stats['unknown_message_key'] += 1
            if k: self.seen[k] = exact
            previous_media = False
            if duplicate and kind in ('image','sticker'):
                for u,expiry,old_run,old_asset in self.old_media.get(k,[]):
                    if u not in candidates or datetime.fromisoformat(expiry)<=datetime.now(timezone.utc): continue
                    try:
                        self.store.read_media(old_run,old_asset)
                        previous_media=True;break
                    except CollectorError as exc:
                        if not str(exc).startswith('MEDIA_') and str(exc)!='RETENTION_EXPIRED': raise
            needs_media = kind in ('image', 'sticker') and k and k not in self.media_attempted and not previous_media
            if needs_media:
                self.media_attempted.add(k)
                valid = [u for u in candidates if isinstance(u, str)][:3]
                if not valid:
                    self.stats['media_missing_url'] += 1; f['b_chat.media_status'] = scalar('source_url_missing')
                else:
                    saved = None; code = 'MEDIA_DOWNLOAD_FAILED'
                    for url in valid:
                        try:
                            kw = {'downloader': self.downloader} if self.downloader else {}
                            if kind == 'image':
                                try:
                                    payload=json.loads(value(f.get('content')) or '{}')
                                    resource=payload.get('resource_url') if isinstance(payload,dict) else None
                                    if isinstance(resource,dict) and resource.get('skey') is not None:
                                        kw['source_key']=resource['skey']
                                except (ValueError,TypeError): pass
                            saved = self.store.save_media(self.run, self.generation, k, url, kind, **kw); break
                        except CollectorError as exc:
                            if str(exc) == 'MEDIA_RATE_LIMIT': raise
                            if not str(exc).startswith('MEDIA_'): raise
                            code = str(exc)
                            if code == 'MEDIA_ACCESS_DENIED': break
                    if saved:
                        self.stats['media_saved'] += 1
                        f['b_chat.media_file_saved'] = scalar(True)
                        f['b_chat.media_asset_id'] = scalar(saved['asset_id'])
                        f['b_chat.media_bytes'] = scalar(saved['bytes'])
                        f['b_chat.media_mime'] = scalar(saved['mime'])
                        f['b_chat.media_status'] = scalar('encrypted_saved')
                    else:
                        self.stats['media_failed'] += 1; f['b_chat.media_status'] = scalar(code)
                if duplicate:
                    # Attachment evidence references the source message, without copying its body again.
                    f = {p: v for p, v in f.items() if p not in ('content', 'b_chat.source_transcript_text', 'b_chat.source_description_text')}
                    f['b_chat.record_role'] = scalar('media_attachment')
                    self.stats['media_attachment_observations'] += 1; kept.append(f)
            if duplicate:
                self.stats['duplicates_skipped'] += 1
                if legacy_hit: self.stats['legacy_duplicates'] += 1
                continue
            self.stats['messages_new'] += 1
            f['b_chat.record_role'] = scalar('message')
            kept.append(f); seen_on_page += 1
        return {'rows': kept, 'before_boundary': bool(rows) and all(old_times),
                'boundary_known': boundary is not None, 'stats': dict(self.stats)}
