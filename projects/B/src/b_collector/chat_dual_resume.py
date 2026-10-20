"""Explicit two-run continuation; all source values remain protected or in memory."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,uuid
from .contract import encode,decode,validate_observation
from .security import CollectorError
from .chat_reviewed_resume import ChatReviewedRecoveryPlan
from .chat_today_materialize import identity,native_digest,typed
from .chat_today_start import inventory,native_matches
from .chat_today_journal import require_lock,digest
from .chat_today_media import classify,FATAL,FAILED
from .chat_incremental import scalar,timestamp
from .chat_store import message_key
from .chat_download import download_image
from .store import now
from . import CONTRACT

CLIENT='b-chat-dual-reviewed/1'

def pin(store,run):
    row,meta=store._run(run)
    return {'run_id':run,'generation':row[0],'state':row[1],'records':row[3],
            'meta_sha256':hashlib.sha256(row[2]).hexdigest(),'expires_at':meta['expires_at']}

def child_inventory(store,run):
    result=inventory(store,run)
    for table,order in [('chat_today_targets','id'),('chat_today_pages','id'),('chat_today_materialized','page_id'),
                        ('chat_today_media_receipts','id'),('chat_today_runtime','run_id')]:
        h=hashlib.sha256();count=0
        for row in store.db.execute(f'SELECT * FROM {table} WHERE run_id=? ORDER BY {order}',(run,)):
            count+=1
            for value in row:
                data=value if isinstance(value,bytes) else encode(value)
                h.update(len(data).to_bytes(8,'big'));h.update(data)
        result[table]={'count':count,'sha256':h.hexdigest()}
    h=hashlib.sha256();count=0
    for row in store.db.execute('SELECT b.page_id,b.position,b.cipher FROM chat_today_page_blocks b JOIN chat_today_pages p ON p.id=b.page_id WHERE p.run_id=? ORDER BY b.page_id,b.position',(run,)):
        count+=1
        for value in row:
            data=value if isinstance(value,bytes) else encode(value);h.update(len(data).to_bytes(8,'big'));h.update(data)
    result['chat_today_page_blocks']={'count':count,'sha256':h.hexdigest()}
    h=hashlib.sha256();count=0
    for row in store.db.execute('SELECT approval_id,child_id,parent_id,cipher FROM chat_today_starts WHERE child_id=? ORDER BY approval_id',(run,)):
        count+=1
        for value in row:
            data=value if isinstance(value,bytes) else encode(value);h.update(len(data).to_bytes(8,'big'));h.update(data)
    result['chat_today_starts']={'count':count,'sha256':h.hexdigest()}
    return result

class DualPlan(ChatReviewedRecoveryPlan):
    client=CLIENT
    def __init__(self,run,row,meta,basis):
        super().__init__(run,row,meta,basis)
        self.child_excluded=set();self.versions=set();self.media_attempted=set()
    def index(self,item):
        if item['domain']!='chat':return
        if item['context']['owner']!=self.meta['chat_resume_owner']:raise CollectorError('SOURCE_CHANGED')
        f=item['fields'];key=identity(item['context']['owner'],item['context']['conversation'],f)
        if key is None:return
        if f.get('b_chat.record_role',{}).get('value')!='media_attachment':self.versions.add((key,native_digest(f)))
        status=f.get('b_chat.media_status',{}).get('value')
        if status is not None and status!='classification_pending' or f.get('b_chat.media_file_saved',{}).get('value') is True or 'b_chat.media_asset_id' in f:self.media_attempted.add(key)
    def feed(self,payload,sequence):
        super().feed(payload,sequence);self.index(payload['observation'])
    def finish(self):
        super().finish()
        excluded={typed(decode(v)) for v in self.completed|self.claimed|self.failed|self.touched}|self.child_excluded
        result=[];seen={}
        for conv in self.conversations.values():
            key=typed(conv['id']);sig=digest([[k,typed(v)] for k,v in sorted(conv.items())])
            if key in seen and seen[key]!=sig:raise CollectorError('SOURCE_CHANGED')
            if key not in seen and key not in excluded:result.append(deepcopy(conv))
            seen[key]=sig
        return result
    def evidence(self):
        targets=self.finish()
        return {'directory_count':len({typed(v['id']) for v in self.conversations.values()}),'target_count':len(targets),
                'target_sha256':digest(targets),'completed_excluded':len({typed(decode(v)) for v in self.completed}),
                'child_attempted_excluded':len(self.child_excluded),'old_unbound_unknown':len(self.unknown_seen),
                'old_failed_and_touched_preserved':True,'platform_complete':False}

def prepare(store,lock,contract):
    require_lock(store,lock)
    parent,child=contract['parent']['run_id'],contract['child']['run_id']
    if parent==child:raise CollectorError('DUAL_RUN_SET')
    with store.transaction():
        if {r[0] for r in store.db.execute('SELECT id FROM runs')}!={parent,child}:raise CollectorError('DUAL_RUN_SET')
        if pin(store,parent)!=contract['parent'] or pin(store,child)!=contract['child']:raise CollectorError('DUAL_STOP_CHANGED')
        if inventory(store,parent)!=contract['parent_inventory'] or child_inventory(store,child)!=contract['child_inventory']:raise CollectorError('DUAL_STOP_CHANGED')
        row,meta=store._run(parent);cr,cm=store._run(child)
        if row[1]!='failed' or cr[1]!='disconnected' or cm['chat_resume_owner']!=meta['chat_resume_owner'] or cm['chat_today']['parent']['run_id']!=parent:raise CollectorError('DUAL_STOP_CHANGED')
        if datetime.now(timezone.utc)>=min(datetime.fromisoformat(meta['expires_at']),datetime.fromisoformat(cm['expires_at'])):raise CollectorError('RETENTION_EXPIRED')
        plan=DualPlan(parent,row,meta,contract['basis'])
        for identifier,seq,cipher in store.db.execute('SELECT id,sequence,cipher FROM observations WHERE run_id=? ORDER BY sequence',(parent,)):
            body=store._decrypt(cipher)
            if body.get('observation_id')!=identifier:raise CollectorError('CHAT_RESUME_RECORD_MISMATCH')
            plan.observe_cipher(seq,cipher);plan.feed(body,seq)
        directory={typed(c['id']):c for c in plan.conversations.values()}
        for state,cipher in store.db.execute('SELECT state,cipher FROM chat_today_targets WHERE run_id=?',(child,)):
            body=store._decrypt(cipher);conv=body['conversation'];key=typed(conv['id'])
            if key not in directory or any(typed(conv[k])!=typed(directory[key][k]) for k in ('id','shortId','type')):raise CollectorError('DUAL_TARGET_CHANGED')
            if state!='pending' or body.get('pages')!=0 or body.get('cursor') is not None:plan.child_excluded.add(key)
        for seq,cipher in store.db.execute('SELECT sequence,cipher FROM observations WHERE run_id=? ORDER BY sequence',(child,)):
            body=store._decrypt(cipher);item=body['observation'];validate_observation(item,cm['scope'])
            if body['run_id']!=child or body['sequence']!=seq:raise CollectorError('DUAL_STOP_CHANGED')
            plan.index(item)
        # Explicitly recognize the one reviewed local-cache failure. Any other
        # child failure is a new global unknown and blocks this entire plan.
        failures=[]
        for state,cipher in store.db.execute("SELECT state,cipher FROM chat_today_pages WHERE run_id=? AND state<>'committed'",(child,)):
            body=store._decrypt(cipher)
            failures.append((state,body))
        if len(failures)!=contract['child_local_cache_failure_count']:raise CollectorError('DUAL_FAILURE_CHANGED')
        for state,body in failures:
            if state!='failed' or body.get('code')!='CONVERSATION_UNAVAILABLE' or body.get('entry_kind')!='cache_only':raise CollectorError('DUAL_GLOBAL_UNKNOWN')
        # This reviewed child has only finalized non-download classifications.
        # A wider inheritance inventory needs a different reviewed contract.
        for state,cipher,global_stop in store.db.execute('SELECT state,cipher,global_stop FROM chat_today_media_receipts WHERE run_id=?',(child,)):
            receipt=store._decrypt(cipher)
            if state!='classified' or global_stop or receipt.get('download_attempted') is not False:
                raise CollectorError('DUAL_MEDIA_HISTORY_CHANGED')
        if pin(store,parent)!=contract['parent'] or pin(store,child)!=contract['child']:raise CollectorError('DUAL_STOP_CHANGED')
        require_lock(store,lock);plan.finish()
        return plan

class DualProcessor:
    """Save body first; single URL media claim then atomic attachment/receipt."""
    def __init__(self,store,run,generation,plan,check_source,downloader=download_image,legacy=None,check_local=None):
        self.store,self.run,self.generation=store,run,generation
        self.plan,self.check_source,self.downloader=plan,check_source,downloader
        self.check_local=check_local or check_source
        self.targets={typed(c['id']):c for c in plan.finish()};self.blocked=False
        self.legacy=legacy or {'messages':set()}
        if self.legacy.get('owner',plan.owner)!=plan.owner:raise CollectorError('SOURCE_CHANGED')
        self.stats={'messages_new':0,'duplicates_skipped':0,'legacy_duplicates':0,'media_saved':0,'media_failed':0,'media_inherited_skipped':0,'unknown_key':0}
        with store.transaction():
            store.db.execute('''CREATE TABLE IF NOT EXISTS chat_dual_media_attempts(
                id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                observation_id TEXT NOT NULL,state TEXT NOT NULL,cipher BLOB NOT NULL,
                global_stop INTEGER NOT NULL DEFAULT 0)''')
            if store.db.execute('SELECT 1 FROM chat_dual_media_attempts WHERE run_id=?',(run,)).fetchone():
                raise CollectorError('DUAL_MEDIA_ALREADY_USED')
    def process(self,owner,conversation,rows,op):
        if self.blocked:raise CollectorError('DUAL_PROCESSOR_STOPPED')
        try:return self._process(owner,conversation,rows,op)
        except BaseException:
            self.blocked=True
            raise
    def _process(self,owner,conversation,rows,op):
        if owner!=self.plan.owner or self.targets.get(typed(conversation['id']))!=conversation or op not in ('chat_cache','chat_history') or not isinstance(rows,list) or len(rows)>100:raise CollectorError('SOURCE_CHANGED')
        self.check_local();conv=conversation['id']
        for source in rows:
            if self.store.db.execute("SELECT 1 FROM chat_dual_media_attempts WHERE run_id=? AND (state='reading' OR global_stop=1)",(self.run,)).fetchone():raise CollectorError('DUAL_MEDIA_REVIEW_REQUIRED')
            fields=deepcopy(source);key=identity(owner,conv,fields)
            bound=(fields.get('conversationId') is not None and typed(fields['conversationId'])==typed(conv)) or (fields.get('conversationShortId') is not None and conversation.get('shortId') is not None and typed(fields['conversationShortId'])==typed(conversation['shortId']))
            for field,expected in [('conversationId',conv),('conversationShortId',conversation.get('shortId'))]:
                if field in fields and expected is not None and typed(fields[field])!=typed(expected):raise CollectorError('SOURCE_CHANGED')
            if not bound:key=None
            version=(key,native_digest(fields)) if key else None
            if version is not None and version in self.plan.versions:self.stats['duplicates_skipped']+=1;continue
            server=fields.get('serverId',{}).get('value');sender=(fields.get('sender') or fields.get('senderUserId') or {}).get('value');at=timestamp(fields)
            if key and type(server) is str and type(sender) is str and at is not None and (conv['value'],'srv_'+server,sender,int(at)) in self.legacy['messages']:
                self.stats['duplicates_skipped']+=1;self.stats['legacy_duplicates']+=1;continue
            kind=classify(fields);old=key in self.plan.media_attempted if key else False
            fields.update({'b_chat.record_role':scalar('message'),'b_chat.media_file_saved':scalar(False),
                           'b_chat.media_kind':scalar(kind['kind'])})
            fields.pop('b_chat.media_status',None)
            if kind['kind'] in ('image','sticker'):
                fields['b_chat.media_status']=scalar('inherited_attempt_not_retried' if old else 'message_key_missing' if not key else 'classification_pending')
            item={'contract':CONTRACT,'domain':'chat','source':{'origin':'https://www.douyin.com','path':'/native-sdk/'+op,
                  'adapter':CLIENT,'transport':'native_sdk','record_ref':'/rows','raw_response_saved':False},
                  'context':{'owner':owner,'app_scope':None,'session':None,'conversation':conv if bound else None,
                  'period_label':None,'period_start':None,'period_end':None,'source_timezone':None,'source_cutoff':None},
                  'observed_at':now(),'fields':fields,'coverage':{'status':'partial','basis':'native_history_continuation',
                  'received':1,'limit':None,'terminal':'unknown'},'issues':['CHAT_DATE_RANGE_NOT_FILTERED','SENDER_DIRECTION_NOT_SEND_RECEIPT','PLATFORM_COVERAGE_UNKNOWN']}
            saved=self.store.append(self.run,self.generation,uuid.uuid4().hex,[item]);self.stats['messages_new']+=1
            identifier,cipher=self.store.db.execute('SELECT id,cipher FROM observations WHERE run_id=? AND sequence=?',(self.run,saved['total'])).fetchone()
            if version:self.plan.versions.add(version)
            else:self.stats['unknown_key']+=1
            if old:self.stats['media_inherited_skipped']+=1
            if not key or old or kind['kind'] not in ('image','sticker'):continue
            attempt=uuid.uuid4().hex
            body={'run_id':self.run,'observation_id':identifier,'source_sha256':hashlib.sha256(cipher).hexdigest(),
                  'message_identity':decode(key),'kind':kind['kind'],'url':kind['url'],'state':'reading'}
            with self.store.transaction():
                self.store._run(self.run,self.generation,active=True)
                self.store.db.execute('INSERT INTO chat_dual_media_attempts VALUES(?,?,?,?,?,0)',(attempt,self.run,identifier,'reading',self.store._encrypt(body)))
            self.plan.media_attempted.add(key)
            data=mime=None;failure=kind['preflight_failure'] or ('source_url_missing' if kind['url'] is None else None)
            if failure is None:
                self.check_source()
                try:data,mime=self.downloader(kind['url'],source_key=kind['source_key'])
                except CollectorError as e:
                    if str(e) not in FAILED|FATAL:raise
                    failure=str(e)
                finally:self.check_source()
            with self.store.transaction():
                self.store._run(self.run,self.generation,active=True)
                f={k:v for k,v in fields.items() if k not in ('content','b_chat.source_transcript_text','b_chat.source_description_text')}
                f['b_chat.record_role']=scalar('media_attachment');f['b_chat.media_kind']=scalar(kind['kind'])
                if failure is None:
                    asset=self.store._insert_media_bytes(self.run,message_key(owner,conv,fields),kind['url'],kind['kind'],data,mime,kind['source_key'] is not None)
                    f.update({'b_chat.media_asset_id':scalar(asset['asset_id']),'b_chat.media_file_saved':scalar(True),
                              'b_chat.media_bytes':scalar(asset['bytes']),'b_chat.media_mime':scalar(asset['mime'])})
                f['b_chat.media_status']=scalar(failure or 'encrypted_saved')
                attachment={**deepcopy(item),'fields':f,'observed_at':now()}
                # Same transaction as asset and receipt; no body copied twice.
                self.check_local()
                row,_=self.store._run(self.run,self.generation,active=True);aid=uuid.uuid4().hex;seq=row[3]+1
                if seq>1000000:raise CollectorError('RUN_RECORD_LIMIT')
                validate_observation(attachment,self.plan.meta['scope'])
                payload={'observation_id':aid,'run_id':self.run,'sequence':seq,'ingested_at':now(),'observation':attachment}
                self.store.db.execute('INSERT INTO observations VALUES(?,?,?,?)',(aid,self.run,seq,self.store._encrypt(payload)))
                self.store.db.execute('UPDATE runs SET count=count+1 WHERE id=?',(self.run,))
                body.update(state='failed' if failure else 'saved',result=failure or 'encrypted_saved',attachment=aid)
                self.store.db.execute('UPDATE chat_dual_media_attempts SET state=?,cipher=?,global_stop=? WHERE id=?',(body['state'],self.store._encrypt(body),int(failure in FATAL),attempt))
            self.stats['media_failed' if failure else 'media_saved']+=1
            if failure in FATAL:raise CollectorError('CHAT_MEDIA_GLOBAL_STOP')
        return {'rows':[],'beforeBoundary':False,'stats':dict(self.stats),'persisted':True}
