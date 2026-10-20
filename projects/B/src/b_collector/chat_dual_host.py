"""Single-use two-run host; no network, arbitrary paths, or old resume entry."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,uuid
from .contract import encode,decode,validate_observation
from .security import CollectorError
from .store import now,Store
from .chat_dual_resume import CLIENT,pin,inventory,child_inventory,DualProcessor
from .chat_today_journal import require_lock,digest
from .chat_today_materialize import typed,validate_fields

class DualHost:
    def __init__(self,store,lock,plan,contract,verify_code,verify_native,legacy=None,downloader=None):
        self.store,self.lock,self.plan,self.contract=store,lock,plan,deepcopy(contract)
        self.verify_code,self.verify_native=verify_code,verify_native
        self.legacy,self.downloader=legacy,downloader
        self.run=plan.run;self.generation=None;self.processor=None;self.page=None;self.blocked=False;self.closed=False
        self.targets=plan.finish();self.target_keys={typed(c['id']) for c in self.targets};self.claims=set();self.completed=set()
        self.attestation=None;self.meta_sha=None

    def before_open(self):
        c=self.contract;require_lock(self.store,self.lock);self.verify_code()
        if datetime.now(timezone.utc)>=datetime.fromisoformat(c['valid_until']):raise CollectorError('DUAL_APPROVAL_EXPIRED')
        self.retention()
        if {r[0] for r in self.store.db.execute('SELECT id FROM runs')}!={c['parent']['run_id'],c['child']['run_id']}:raise CollectorError('DUAL_STOP_CHANGED')
        if pin(self.store,self.run)!=c['parent'] or pin(self.store,c['child']['run_id'])!=c['child']:raise CollectorError('DUAL_STOP_CHANGED')
        if inventory(self.store,self.run)!=c['parent_inventory'] or child_inventory(self.store,c['child']['run_id'])!=c['child_inventory']:raise CollectorError('DUAL_STOP_CHANGED')
        if self.plan.evidence()!=c['plan']:raise CollectorError('DUAL_PLAN_CHANGED')

    def open(self,owner):
        if self.generation is not None or self.closed:raise CollectorError('DUAL_ALREADY_USED')
        self.before_open();live=self.verify_native()
        if live.get('owner')!=owner or owner!=self.plan.owner or live.get('contract_verified') is not True:raise CollectorError('SOURCE_CHANGED')
        if digest(live)!=self.contract['native_sha256']:raise CollectorError('SOURCE_CHANGED')
        with self.store.transaction():
            self.before_open()
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS chat_dual_activations(
                approval_id TEXT PRIMARY KEY,run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,cipher BLOB NOT NULL)''')
            if self.store.db.execute('SELECT 1 FROM chat_dual_activations WHERE approval_id=? OR run_id=?',(self.contract['approval_id'],self.run)).fetchone():raise CollectorError('DUAL_ALREADY_USED')
            row,meta=self.store._run(self.run);generation=uuid.uuid4().hex
            evidence=self.plan.evidence()
            meta.setdefault('chat_resumes',[]).append({'client':CLIENT,'at':now(),'prior_state':row[1],
                'prior_finished_at':meta['finished_at'],'prior_stop_reason':meta['stop_reason'],
                'committed_count':row[3],'directory_count':evidence['directory_count'],
                'completed_count':len(self.plan.completed),'pending_count':len(self.targets),
                'child_pin':self.contract['child'],'contract_sha256':digest(self.contract),**evidence})
            meta.update(finished_at=None,stop_reason=None)
            cipher=self.store._encrypt(meta)
            self.store.db.execute("UPDATE runs SET state='running',generation=?,cipher=? WHERE id=?",(generation,cipher,self.run))
            self.store.db.execute('INSERT INTO chat_dual_activations VALUES(?,?,?)',(self.contract['approval_id'],self.run,self.store._encrypt({'contract':self.contract,'generation':generation,'native':live,'started_at':now()})))
            self.verify_code();require_lock(self.store,self.lock)
            self.retention()
            if datetime.now(timezone.utc)>=datetime.fromisoformat(self.contract['valid_until']):raise CollectorError('DUAL_APPROVAL_EXPIRED')
        self.generation=generation;self.meta_sha=hashlib.sha256(cipher).hexdigest();self.attestation=live
        try:
            kwargs={'legacy':self.legacy,'check_local':self.local}
            if self.downloader is not None:kwargs['downloader']=self.downloader
            self.processor=DualProcessor(self.store,self.run,generation,self.plan,self.source,**kwargs)
            with self.store.transaction():
                self.store.db.execute('''CREATE TABLE IF NOT EXISTS chat_dual_pages(
                    id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,position INTEGER NOT NULL,cipher BLOB NOT NULL)''')
                self.store.db.execute('''CREATE TABLE IF NOT EXISTS chat_dual_page_blocks(
                    page_id TEXT NOT NULL REFERENCES chat_dual_pages(id) ON DELETE CASCADE,position INTEGER NOT NULL,
                    cipher BLOB NOT NULL,PRIMARY KEY(page_id,position))''')
        except BaseException:
            self.finish();raise
        return {'run_id':self.run,'generation':generation,'scope':meta['scope'],'expires_at':meta['expires_at'],
                'effective_expires_at':min(datetime.fromisoformat(self.contract[k]['expires_at']) for k in ('parent','child')).isoformat(),
                'owner':owner,'total':row[3],'target_count':len(self.targets),'directory_count':evidence['directory_count'],
                'completed_count':len(self.plan.completed),'unresolved_media_count':len(self.plan.media_gaps),
                'unresolved_history_count':evidence['old_unbound_unknown']+len((self.plan.claimed|self.plan.failed|self.plan.touched)-self.plan.completed),
                'client':CLIENT}

    def local(self):
        require_lock(self.store,self.lock);self.verify_code()
        self.retention()
        if self.closed or self.generation is None:raise CollectorError('TASK_NOT_OWNED')
        row,_=self.store._run(self.run,self.generation,active=True)
        if hashlib.sha256(row[2]).hexdigest()!=self.meta_sha or pin(self.store,self.contract['child']['run_id'])!=self.contract['child']:raise CollectorError('DUAL_ACTIVE_CHANGED')

    def retention(self):
        if datetime.now(timezone.utc)>=min(datetime.fromisoformat(self.contract[k]['expires_at']) for k in ('parent','child')):
            raise CollectorError('RETENTION_EXPIRED')

    def source(self):
        self.local();live=self.verify_native();self.local()
        if digest(live)!=digest(self.attestation):raise CollectorError('SOURCE_CHANGED')

    def append(self,a):
        self.local()
        if set(a)!={'run_id','generation','batch_id','items'} or (a['run_id'],a['generation'])!=(self.run,self.generation):raise CollectorError('TASK_NOT_OWNED')
        newclaims=set();newcomplete=set();failed=False
        for item in a['items']:
            validate_observation(item,self.plan.meta['scope'])
            if item['domain']!='capability':raise CollectorError('DUAL_APPEND_DIAGNOSTIC_ONLY')
            owner=item['context']['owner'];conv=item['context']['conversation'];code=item['fields'].get('status',{}).get('value')
            if owner not in (None,self.plan.owner):raise CollectorError('SOURCE_CHANGED')
            key=typed(conv) if conv is not None else None
            if key is not None and key not in self.target_keys:raise CollectorError('DUAL_TARGET_CHANGED')
            if code=='CHAT_DIRECTORY_RECOVERY_ATTEMPT_STARTED':
                if self.blocked or key is None or key in self.claims or self.page:raise CollectorError('DUAL_CLAIM_REJECTED')
                newclaims.add(key)
            elif code in {'CHAT_INCREMENTAL_BOUNDARY_REACHED','CHAT_HISTORY_SOURCE_NO_MORE'}:
                if self.blocked or self.page or key not in self.claims or key in self.completed:raise CollectorError('DUAL_COMPLETION_REJECTED')
                newcomplete.add(key)
            elif key is not None and key not in self.claims:raise CollectorError('DUAL_CLAIM_REQUIRED')
            if code not in {'CHAT_DIRECTORY_RECOVERY_ATTEMPT_STARTED','CHAT_INCREMENTAL_BOUNDARY_REACHED','CHAT_HISTORY_SOURCE_NO_MORE','CHAT_SOURCE_EMPTY_PAGE_OBSERVED','CHAT_COVERAGE_PARTIAL'}:failed=True
        result=self.store.append(self.run,self.generation,a['batch_id'],a['items'])
        self.claims.update(newclaims);self.completed.update(newcomplete);self.blocked=self.blocked or failed;return result

    def stage(self,a):
        if self.blocked or self.page:raise CollectorError('DUAL_PAGE_BLOCKED')
        self.source()
        if set(a)!={'owner','conversation','rows','op'} or a['owner']!=self.plan.owner or a['conversation'] not in self.targets or typed(a['conversation']['id']) not in self.claims or typed(a['conversation']['id']) in self.completed:raise CollectorError('DUAL_CLAIM_REQUIRED')
        if a['op'] not in ('chat_cache','chat_history') or not isinstance(a['rows'],list) or len(a['rows'])>100:raise CollectorError('DUAL_PAGE_SHAPE')
        for row in a['rows']:validate_fields(row)
        page=uuid.uuid4().hex;body={'run_id':self.run,'page_id':page,**deepcopy(a),'rows':len(a['rows']),'rows_sha256':digest(a['rows']),'saved_at':now()}
        with self.store.transaction():
            self.local()
            self.store.db.execute('INSERT INTO chat_dual_pages VALUES(?,?,?,?,?)',(page,self.run,'saved',0,self.store._encrypt(body)))
            for i,row in enumerate(a['rows']):self.store.db.execute('INSERT INTO chat_dual_page_blocks VALUES(?,?,?)',(page,i,self.store._encrypt({'run_id':self.run,'page_id':page,'position':i,'fields':row})))
        self.page=page
        return {'page_id':page,'rows':len(a['rows'])}

    def step(self,page,expected_position):
        if self.blocked or page!=self.page:raise CollectorError('DUAL_PAGE_BLOCKED')
        self.local();state,position,cipher=self.store.db.execute('SELECT state,position,cipher FROM chat_dual_pages WHERE id=?',(page,)).fetchone();body=self.store._decrypt(cipher)
        if type(expected_position) is not int or expected_position!=position:raise CollectorError('DUAL_STEP_UNKNOWN')
        if state not in ('saved','processing') or body['run_id']!=self.run or body['page_id']!=page:raise CollectorError('DUAL_PAGE_CHANGED')
        if position<body['rows']:
            # Consume the step before local work: a lost response is not retried.
            with self.store.transaction():
                self.local()
                if state=='processing':raise CollectorError('DUAL_STEP_UNKNOWN')
                self.store.db.execute("UPDATE chat_dual_pages SET state='processing' WHERE id=?",(page,))
            block=self.store._decrypt(self.store.db.execute('SELECT cipher FROM chat_dual_page_blocks WHERE page_id=? AND position=?',(page,position)).fetchone()[0])
            if set(block)!={'run_id','page_id','position','fields'} or (block['run_id'],block['page_id'],block['position'])!=(self.run,page,position):raise CollectorError('DUAL_PAGE_CHANGED')
            self.processor.process(body['owner'],body['conversation'],[block['fields']],body['op'])
            with self.store.transaction():
                self.local();self.store.db.execute("UPDATE chat_dual_pages SET state='saved',position=? WHERE id=?",(position+1,page))
            position+=1
        done=position==body['rows']
        if done:
            with self.store.transaction():
                self.local();self.store.db.execute("UPDATE chat_dual_pages SET state='completed' WHERE id=?",(page,))
            self.page=None
        return {'done':done,'position':position,'stats':dict(self.processor.stats),'total':self.store._run(self.run)[0][3]}

    def finish(self):
        if self.closed:return {'closed':True,'coverage':'partial_requested_scope'}
        if self.generation is not None:
            require_lock(self.store,self.lock)
            row,_=self.store._run(self.run,self.generation)
            if hashlib.sha256(row[2]).hexdigest()!=self.meta_sha:raise CollectorError('DUAL_FINISH_REVIEW_REQUIRED')
            # The sealed baseline and this host's atomic asset+attachment writes
            # have no detached assets. Do not decrypt the historical body corpus
            # again during IPC shutdown merely to re-prove that same invariant.
            result=Store.finish(self.store,self.run,self.generation,'disconnected')
        else:result={'state':'not_started'}
        self.closed=True
        return {**result,'coverage':'partial_requested_scope','targets_completed':len(self.completed),'targets_claimed':len(self.claims),'unattempted_this_activation':len(self.targets)-len(self.claims),'stats':dict(self.processor.stats) if self.processor else None}

    def call(self,op,a):
        try:
            if op=='chat_resume_targets':
                self.local();offset=a['offset']
                if (a['run_id'],a['generation'])!=(self.run,self.generation) or type(offset) is not int or not 0<=offset<=len(self.targets):raise CollectorError('DUAL_TARGET_CURSOR')
                rows=self.targets[offset:offset+100];return {'rows':rows,'next_offset':offset+len(rows),'total':len(self.targets)}
            if op=='append':return self.append(a)
            if op=='dual_page':return self.stage(a)
            if op=='dual_step':return self.step(a['page_id'],a['position'])
            if op=='finish':return self.finish()
            raise CollectorError('DUAL_OPERATION_REJECTED')
        except BaseException:
            self.blocked=True;raise
