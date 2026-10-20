"""Unwired media completion for already materialized, fixed-window pages.

No browser, source rescan, approval loader or default downloader. The future
reviewed caller supplies a downloader. Claims precede that single call; a lost
result is never retried or replaced with another URL. All source fields, keys,
URLs, receipts and binary chunks remain in memory or the protected store.
"""
import base64
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import math
import re
import uuid

from .contract import decode, encode, validate_observation
from .security import CollectorError
from .store import now, uid
from .chat_store import message_key, value
from .chat_download import destination, image_type, MAX_BYTES
from .chat_today_materialize import InheritanceIndex, identity, read_verified_page, verify_receipt

CLIENT = 'b-chat-today-media/1'
IDENTITY = ('serverId', 'clientId', 'id', 'conversationId', 'conversationShortId', 'sender',
            'senderUserId', 'secSender', 'type', 'createTime', 'b_chat.created_at_iso',
            'b_chat.created_at_basis', 'b_request.conversation_id')
FATAL = {'MEDIA_ACCESS_DENIED', 'MEDIA_RATE_LIMIT'}
FAILED = {'MEDIA_HTTP_FAILED', 'MEDIA_TIMEOUT', 'MEDIA_DOWNLOAD_FAILED', 'MEDIA_SIZE_LIMIT',
          'MEDIA_NOT_IMAGE', 'MEDIA_ENCODING_REJECTED', 'MEDIA_TRUNCATED', 'MEDIA_TYPE_MISMATCH',
          'MEDIA_REDIRECT_REJECTED', 'MEDIA_CIPHER_SIZE', 'MEDIA_DECRYPT_FAILED',
          'MEDIA_URL_REJECTED', 'MEDIA_ADDRESS_REJECTED', 'MEDIA_SOURCE_KEY_INVALID'}


def scalar(v):
    return {'type': 'boolean' if type(v) is bool else 'number' if type(v) is int else 'string',
            'value': v, 'lossless': True}


def sha(data): return hashlib.sha256(data).hexdigest()


def classify(fields):
    """Mirror pinned exporter labels; neither image semantics nor user intent.

    Only known attachment slots are eligible. Never recurse for arbitrary URLs
    or use avatars/video posters. The first original candidate is final, even
    if invalid; no alternate candidate is tried after a rejection or failure.
    """
    result = {'kind': 'unknown', 'basis': 'unavailable', 'url': None, 'source_key': None,
              'preflight_failure': None, 'description': None, 'transcript_status': None}
    body = value(fields.get('content'))
    try:
        p = decode(body.encode('utf-8')) if isinstance(body, str) else None
    except Exception: p = None
    if not isinstance(p, dict): return result
    t = p.get('aweType'); t = t if type(t) is int else None
    resource = p.get('resource_url'); resource = resource if isinstance(resource, dict) else {}
    duration = p.get('duration', resource.get('duration'))
    voice = isinstance(p.get('resource_url'), dict) and (
        type(duration) in (int, float) and math.isfinite(duration) and duration >= 0
        or isinstance(duration, str) and re.fullmatch(r'\d+(?:\.\d+)?', duration) is not None)
    if isinstance(p.get('video'), dict) or t in (11054,11055,11063,11066,11067,11069,11070): kind = 'video'
    elif t in (2702,2703,2704): kind = 'image'
    elif t in (500,501,507,508,510,514,516): kind = 'sticker'
    elif voice: kind = 'voice'
    elif t in (0,700,701,703) and isinstance(p.get('text'), str): kind = 'text'
    else: kind = 'unknown'
    result.update(kind=kind, basis='unrecognized_payload' if kind == 'unknown' else 'pinned_exporter_payload_rules')
    if kind == 'voice':
        transcript = value(fields.get('b_chat.source_transcript_text'))
        result['transcript_status'] = 'source_text_unreviewed' if isinstance(transcript,str) and transcript.strip() else 'pending'
    if kind not in ('image','sticker'): return result
    description = p.get('display_name' if kind == 'sticker' else 'description')
    if isinstance(description, str) and len(description) <= 32768: result['description'] = description
    if kind == 'image':
        source_key = resource.get('skey')
        if source_key is not None:
            if not isinstance(source_key,str) or not re.fullmatch('[0-9a-fA-F]{64}',source_key):
                result['preflight_failure'] = 'MEDIA_SOURCE_KEY_INVALID'; return result
            result['source_key'] = source_key
        lists = [resource.get(name) for name in ('origin_url_list','large_url_list','medium_url_list','thumb_url_list')]
    else:
        url = p.get('url'); lists = [url.get('url_list')] if isinstance(url, dict) else []
    first = next((url for values in lists if isinstance(values,list) for url in values if isinstance(url,str)), None)
    if first is None: return result
    try: destination(first)
    except CollectorError:
        result['preflight_failure'] = 'MEDIA_URL_REJECTED'; return result
    result['url'] = first
    return result


class TodayMedia:
    def __init__(self, journal, index):
        if not isinstance(index, InheritanceIndex) or index.journal is not journal:
            raise CollectorError('CHAT_TODAY_INDEX_CHANGED')
        self.journal, self.index, self.store = journal, index, journal.store
        self.targets = {}; self.attempted = set(index.media_attempted)
        self.invoked = set(); self.writer = uuid.uuid4().hex; self.blocked = False; self.receipt_watermark=0
        with self.store.transaction():
            index.check()
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS chat_today_media_receipts(
                id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                page_id TEXT NOT NULL REFERENCES chat_today_pages(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,state TEXT NOT NULL,cipher BLOB NOT NULL,
                global_stop INTEGER NOT NULL DEFAULT 0,
                UNIQUE(page_id,position))''')
            self.store.db.execute('CREATE INDEX IF NOT EXISTS chat_today_media_fence ON chat_today_media_receipts(run_id,state,global_stop)')
            self._refresh_fences()
            index.check()

    def _refresh_fences(self):
        """Transaction-local persistent gate, including other live instances.

        New receipts are decrypted once per instance, not the whole growing
        ledger per image. Pending/global-stop states are queried every time.
        A state transition cannot make an old claim eligible again.
        """
        if not self.store.db.in_transaction: raise CollectorError('CHAT_TODAY_TRANSACTION_REQUIRED')
        runs=(self.journal.policy['parent']['run_id'],self.journal.run)
        self.attempted.update(self.index.media_attempted)
        for rowid,identifier,run,page,position,state,cipher,global_stop in self.store.db.execute(
                'SELECT rowid,id,run_id,page_id,position,state,cipher,global_stop FROM chat_today_media_receipts '
                'WHERE run_id IN (?,?) AND rowid>? ORDER BY rowid',(*runs,self.receipt_watermark)):
            r=self.store._decrypt(cipher)
            if (r.get('client')!=CLIENT or r.get('id')!=identifier or r.get('run_id')!=run
                    or r.get('page_id')!=page or r.get('position')!=position
                    or r.get('owner')!=self.journal.policy['owner'] or state not in {'reading','saved','failed','classified','excluded'}
                    or type(global_stop) is not int or global_stop not in (0,1)
                    or r.get('global_stop') is not bool(global_stop)):
                raise CollectorError('CHAT_TODAY_MEDIA_CHANGED')
            if r.get('attempt_exclusion'):
                self.attempted.add(base64.b64decode(r['semantic_key'],validate=True))
            self.receipt_watermark=rowid
        if self.store.db.execute('SELECT 1 FROM chat_today_media_receipts WHERE run_id IN (?,?) '
                                 "AND (state='reading' OR global_stop=1) LIMIT 1",runs).fetchone():
            self.blocked=True

    def prepare_page(self, page_id):
        """One bounded page verification; later per-row checks use cipher pins."""
        with self.store.transaction():
            self.index.check()
            manifest, projections, source_sha = read_verified_page(self.journal,page_id)
            row = self.store.db.execute('SELECT cipher FROM chat_today_materialized WHERE page_id=? AND run_id=?',
                                        (uid(page_id),self.journal.run)).fetchone()
            if not row: raise CollectorError('CHAT_TODAY_PAGE_NOT_MATERIALIZED')
            materialized = self.store._decrypt(row[0])
            verify_receipt(self.journal,materialized,manifest,projections,source_sha)
            pending = {}
            for position, (projection, disposition) in enumerate(zip(projections,materialized['dispositions'],strict=True)):
                token = uuid.uuid4().hex
                block = self.store.db.execute('SELECT cipher FROM chat_today_page_blocks WHERE page_id=? AND position=?',
                                              (page_id,position)).fetchone()[0]
                fields = projection['fields']; key = identity(self.journal.policy['owner'],manifest['conversation']['id'],fields)
                target = {'page_id':page_id,'position':position,'manifest_sha256':source_sha,
                          'materialized_sha256':sha(row[0]),'block_sha256':sha(block),
                          'disposition':deepcopy(disposition),'fields':deepcopy(fields),'conversation':manifest['conversation'],
                          'semantic_key':key,'classification':classify(fields),'original':None}
                if disposition['kind'] == 'inserted':
                    link = disposition['evidence']
                    target['original'] = self.store._decrypt(self.store.db.execute(
                        'SELECT cipher FROM observations WHERE id=? AND run_id=?',(link['observation_id'],link['run_id'])).fetchone()[0])
                pending[token] = target
            self.index.check()
        self.targets.update(pending)
        return list(pending)  # Local random tokens only, never source fields.

    def _check(self, target):
        self.index.check(); s=self.store; run=self.journal.run
        for sql,args,expected in (
            ('SELECT cipher FROM chat_today_pages WHERE id=? AND run_id=?',(target['page_id'],run),target['manifest_sha256']),
            ('SELECT cipher FROM chat_today_materialized WHERE page_id=? AND run_id=?',(target['page_id'],run),target['materialized_sha256']),
            ('SELECT cipher FROM chat_today_page_blocks WHERE page_id=? AND position=?',(target['page_id'],target['position']),target['block_sha256'])):
            row=s.db.execute(sql,args).fetchone()
            if not row or sha(row[0])!=expected: raise CollectorError('CHAT_TODAY_MEDIA_CHANGED')
        if target['original'] is not None:
            link=target['disposition']['evidence']
            row=s.db.execute('SELECT cipher FROM observations WHERE id=? AND run_id=?',(link['observation_id'],run)).fetchone()
            if not row or sha(row[0])!=link['cipher_sha256']: raise CollectorError('CHAT_TODAY_MEDIA_CHANGED')

    def release_page(self,page_id):
        """Drop private page projections after durable processing, without source I/O.

        The production loop must release completed pages rather than retaining
        all history bodies in this object's prepared-target cache. Preparing
        that local page again is permitted only as receipt verification.
        """
        tokens=[token for token,t in self.targets.items() if t['page_id']==page_id]
        with self.store.transaction():
            self.index.check()
            for token in tokens:
                target=self.targets[token];self._check(target)
                row=self.store.db.execute('SELECT state,global_stop FROM chat_today_media_receipts WHERE page_id=? AND position=?',
                                          (page_id,target['position'])).fetchone()
                if not row or row[0]=='reading' or row[1]:raise CollectorError('CHAT_TODAY_MEDIA_REVIEW_REQUIRED')
        for token in tokens:self.targets.pop(token);self.invoked.discard(token)
        return {'released_targets':len(tokens)}

    def _annotation(self,target,result,finished,asset=None):
        original=target['original']['observation']; c=target['classification']; item=deepcopy(original)
        item['fields']={k:deepcopy(original['fields'][k]) for k in IDENTITY if k in original['fields']}
        extra={'b_chat.record_role':'media_attachment','b_chat.media_kind':c['kind'],
               'b_chat.media_basis':c['basis'],'b_chat.media_verified':False,'b_chat.intent_status':'not_inferred',
               'b_chat.media_file_saved':asset is not None,'b_chat.media_resolution_status':result,
               'b_chat.media_source_observation_id':target['original']['observation_id']}
        if c['kind'] in ('image','sticker'):
            extra['b_chat.media_status']=result
        if c['description'] is not None: extra['b_chat.source_description_text']=c['description']
        if c['transcript_status'] is not None: extra['b_chat.transcript_status']=c['transcript_status']
        if asset:
            extra.update({'b_chat.media_asset_id':asset['asset_id'],'b_chat.media_bytes':asset['bytes'],'b_chat.media_mime':asset['mime']})
        item['fields'].update({k:scalar(v) for k,v in extra.items()})
        item['source'].update(path='/local-media/today-message',adapter=CLIENT,transport='capability',
                              record_ref='/observations/'+target['original']['observation_id'])
        item['observed_at']=finished
        item['coverage']={'status':'partial','basis':'stored_today_message_attachment','received':1,'limit':None,'terminal':'unknown'}
        item['issues']=['MEDIA_LABEL_IS_CANDIDATE','NO_NEW_CHAT_HISTORY_READ','INTENT_NOT_INFERRED','FIXED_WINDOW_PARTIAL']
        validate_observation(item,self.journal.meta['scope']); return item

    def _finish(self,target,receipt,state,result,data=None,mime=None):
        self._check(target)
        finished=now(); asset=None; c=target['classification']; original=target['original']
        if data is not None:
            key=message_key(self.journal.policy['owner'],target['conversation']['id'],target['fields'])
            if not key: raise CollectorError('MEDIA_BINDING_MISMATCH')
            asset=self.store._insert_media_bytes(self.journal.run,key,c['url'],c['kind'],data,mime,c['source_key'] is not None)
        receipt.update(result=result,finished_at=finished,global_stop=result in FATAL,asset=asset,annotation=None)
        added=0
        if original is not None:
            item=self._annotation(target,result,finished,asset)
            identifier=uuid.uuid4().hex; sequence=self.index.current_count+1
            if sequence>1000000: raise CollectorError('RUN_RECORD_LIMIT')
            payload={'observation_id':identifier,'run_id':self.journal.run,'sequence':sequence,'ingested_at':now(),'observation':item}
            cipher=self.store._encrypt(payload)
            self.store.db.execute('INSERT INTO observations VALUES(?,?,?,?)',(identifier,self.journal.run,sequence,cipher))
            self.store.db.execute('UPDATE runs SET count=count+1 WHERE id=?',(self.journal.run,))
            receipt['annotation']={'id':identifier,'sequence':sequence,'cipher_sha256':sha(cipher)}; added=1
        self.store.db.execute('UPDATE chat_today_media_receipts SET state=?,cipher=?,global_stop=? WHERE id=?',
                              (state,self.store._encrypt(receipt),int(receipt['global_stop']),receipt['id']))
        self.journal._check()  # index count intentionally advances only after commit.
        return added

    def _replay(self,target,row):
        state,cipher=row; r=self.store._decrypt(cipher)
        if (r.get('run_id')!=self.journal.run or r.get('page_id')!=target['page_id'] or r.get('position')!=target['position']
                or r.get('owner')!=self.journal.policy['owner'] or r.get('source_pins')!=self._pins(target)
                or r.get('semantic_key')!=(base64.b64encode(target['semantic_key']).decode('ascii') if target['semantic_key'] else None)
                or state not in {'reading','saved','failed','classified','excluded'}):
            raise CollectorError('CHAT_TODAY_MEDIA_CHANGED')
        if state=='reading' or r.get('global_stop'): raise CollectorError('CHAT_TODAY_MEDIA_REVIEW_REQUIRED')
        annotation=r.get('annotation')
        if (bool(annotation)!=(target['original'] is not None)
                or bool(r.get('asset'))!=(state=='saved')
                or state=='saved' and (r.get('result')!='encrypted_saved' or r.get('download_attempted') is not True
                    or target['classification']['kind'] not in ('image','sticker') or not annotation)
                or state!='saved' and r.get('result')=='encrypted_saved'):
            raise CollectorError('CHAT_TODAY_MEDIA_CHANGED')
        if annotation:
            row=self.store.db.execute('SELECT cipher FROM observations WHERE id=? AND run_id=?',
                                      (annotation['id'],self.journal.run)).fetchone()
            if not row or sha(row[0])!=annotation['cipher_sha256']: raise CollectorError('CHAT_TODAY_MEDIA_CHANGED')
            payload=self.store._decrypt(row[0])
            if (payload['sequence']!=annotation['sequence'] or payload['observation_id']!=annotation['id']
                    or payload['run_id']!=self.journal.run
                    or payload['observation']!=self._annotation(target,r['result'],r['finished_at'],r.get('asset'))):
                raise CollectorError('CHAT_TODAY_MEDIA_CHANGED')
        if r.get('asset'):
            asset=r['asset']
            if (not isinstance(asset,dict) or set(asset)!={'asset_id','bytes','mime'}
                    or type(asset['bytes']) is not int or not 0<asset['bytes']<=MAX_BYTES
                    or asset['mime'] not in {'image/png','image/jpeg','image/gif','image/webp','image/avif'}):
                raise CollectorError('MEDIA_BINDING_MISMATCH')
            manifest,_=self.store.read_media(self.journal.run,uid(asset['asset_id']))
            expected=message_key(self.journal.policy['owner'],target['conversation']['id'],target['fields'])
            if (manifest['message_key']!=base64.b64encode(expected).decode('ascii')
                    or manifest['url']!=target['classification']['url'] or manifest['kind']!=target['classification']['kind']
                    or manifest['bytes']!=asset['bytes'] or manifest['mime']!=asset['mime']):
                raise CollectorError('MEDIA_BINDING_MISMATCH')
        return {'state':state,'replayed':True,'saved_assets':int(state=='saved'),'media_complete':False,'window_complete':False}

    def _pins(self,t):
        return {k:t[k] for k in ('manifest_sha256','materialized_sha256','block_sha256')} | {'disposition':t['disposition']}

    @contextmanager
    def _claim_transaction(self):
        try:
            with self.store.transaction(): yield
        except BaseException:
            # An uncertain claim transaction must also stop other targets in
            # this live writer, not only the target whose local latch was set.
            self.blocked=True
            raise

    def process(self, token, *, downloader):
        if token not in self.targets: raise CollectorError('CHAT_TODAY_MEDIA_TARGET')
        target=self.targets[token]; c=target['classification']; key=target['semantic_key']; added=0
        with self._claim_transaction():
            self._check(target)
            self._refresh_fences()
            existing=self.store.db.execute('SELECT state,cipher FROM chat_today_media_receipts WHERE page_id=? AND position=?',
                                           (target['page_id'],target['position'])).fetchone()
            if existing: return self._replay(target,existing)
            if self.blocked: raise CollectorError('CHAT_TODAY_MEDIA_REVIEW_REQUIRED')
            disposition=target['disposition']['kind']
            excluded=key is not None and key in self.attempted
            eligible=disposition=='inserted' and not excluded and c['kind'] in ('image','sticker')
            attempt=eligible and c['url'] is not None and c['preflight_failure'] is None
            if token in self.invoked: raise CollectorError('CHAT_TODAY_MEDIA_REVIEW_REQUIRED')
            # Latch before the claim transaction: a lost transaction response
            # must never become a second source attempt by this writer.
            self.invoked.add(token)
            receipt={'client':CLIENT,'id':uuid.uuid4().hex,'run_id':self.journal.run,'page_id':target['page_id'],
                     'position':target['position'],'owner':self.journal.policy['owner'],'writer':self.writer,
                     'source_pins':self._pins(target),'semantic_key':base64.b64encode(key).decode('ascii') if key else None,
                     'attempt_exclusion':eligible or excluded,'download_attempted':attempt,'claimed_at':now(),
                     'global_stop':False,'result':None}
            self.store.db.execute('INSERT INTO chat_today_media_receipts(id,run_id,page_id,position,state,cipher) VALUES(?,?,?,?,?,?)',
                                  (receipt['id'],self.journal.run,target['page_id'],target['position'],'reading',self.store._encrypt(receipt)))
            if not attempt:
                result=('quarantined_not_downloaded' if disposition=='quarantined' else 'duplicate_not_downloaded' if disposition=='duplicate'
                        else 'inherited_attempt_not_retried' if excluded else c['preflight_failure'] or
                        ('source_url_missing' if eligible else 'video_not_downloaded' if c['kind']=='video' else 'classified_only'))
                state='failed' if eligible else 'excluded' if disposition!='inserted' or excluded else 'classified'
                added=self._finish(target,receipt,state,result)
            self.journal._check()
        if not attempt:
            self.index.current_count+=added
            if receipt['attempt_exclusion'] and key: self.attempted.add(key); self.index.media_attempted.add(key)
            return {'state':state,'replayed':False,'saved_assets':0,'media_complete':False,'window_complete':False}
        # A durable reading claim blocks all further work until committed.
        self.blocked=True; self.attempted.add(key); self.index.media_attempted.add(key)
        failure=None; data=mime=None
        try:
            data,mime=downloader(c['url'],source_key=c['source_key'])
            if type(data) is not bytes or not data or len(data)>MAX_BYTES:
                raise CollectorError('MEDIA_SIZE_LIMIT')
            if image_type(data)!=mime: raise CollectorError('MEDIA_TYPE_MISMATCH')
        except CollectorError as exc:
            if str(exc) not in FAILED | FATAL:
                raise CollectorError('CHAT_TODAY_MEDIA_INFLIGHT_UNKNOWN') from None
            failure=str(exc)
        except BaseException:
            raise CollectorError('CHAT_TODAY_MEDIA_INFLIGHT_UNKNOWN') from None
        try:
            with self.store.transaction():
                self._check(target)
                found=self.store.db.execute('SELECT state,cipher FROM chat_today_media_receipts WHERE id=?',(receipt['id'],)).fetchone()
                if not found or found[0]!='reading' or self.store._decrypt(found[1])!=receipt:
                    raise CollectorError('CHAT_TODAY_MEDIA_CHANGED')
                state='failed' if failure else 'saved'; result=failure or 'encrypted_saved'
                added=self._finish(target,receipt,state,result,data if not failure else None,mime)
            self.index.current_count+=added; self.blocked=failure in FATAL
        except BaseException:
            # Binary data/annotation/receipt must all roll back together. Leave
            # the original claim unknown, including failed local persistence.
            raise CollectorError('CHAT_TODAY_MEDIA_INFLIGHT_UNKNOWN') from None
        if self.blocked: raise CollectorError(failure)
        return {'state':state,'replayed':False,'saved_assets':int(state=='saved'),'media_complete':False,'window_complete':False}
