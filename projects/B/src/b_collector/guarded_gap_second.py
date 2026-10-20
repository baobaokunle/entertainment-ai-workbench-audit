"""Second, separately reviewed activation of existing pending only. No replan."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,re,uuid
from .contract import decode,encode
from .security import CollectorError
from .store import uid,now
from .gaps import CODE_HASH,queue_class,verify_code as verify_frozen
from .guarded_gap_prepare import checkpoint,digest_rows,require_lock,future_chain_guard
from .guarded_gap_bridge import GuardedGapBridge,CLIENT,CODE_FILES as PRIOR_FILES,SOURCE_ROOT

FORMAT='b-gap-guarded-second/1'
FILES=PRIOR_FILES+('src/b_collector/guarded_gap_second.py',)

def prior_receipts(store,run):
    result={}
    for table in ('guarded_gap_preparations','guarded_gap_activations'):
        rows=list(store.db.execute(f'SELECT * FROM {table} WHERE run_id=? ORDER BY rowid',(run,)))
        if len(rows)!=1:raise CollectorError('SECOND_PRIOR_RECEIPT_SET')
        result[table]=digest_rows(rows)
    result['reading']=digest_rows(store.db.execute("SELECT id,target,phase,state,cipher FROM gap_operations WHERE run_id=? AND state='reading' ORDER BY rowid",(run,)))
    return result

def validate_packet(data,pin,code,clock=None):
    if not isinstance(pin,str) or not re.fullmatch('[a-f0-9]{64}',pin) or hashlib.sha256(data).hexdigest()!=pin:
        raise CollectorError('SECOND_REVIEW_CHANGED')
    c=decode(data)
    if set(c)!={'format','approved_to_execute','approval_id','checkpoint','prior_receipts','code_sha256','stopped_at','absolute_deadline','reviewed_at','valid_until'} or c['format']!=FORMAT or c['approved_to_execute'] is not True:
        raise CollectorError('SECOND_REVIEW_REQUIRED')
    uid(c['approval_id']);uid(c['checkpoint']['run_id'])
    moment=clock or datetime.now(timezone.utc)
    times=[datetime.fromisoformat(c[k]) for k in ('stopped_at','reviewed_at','valid_until','absolute_deadline')]
    if any(t.utcoffset() is None for t in times) or not times[0]<times[1]<=moment<times[2]<=times[3]:
        raise CollectorError('SECOND_REVIEW_EXPIRED')
    if set(c['code_sha256'])!=set(FILES) or c['code_sha256']!=code:raise CollectorError('VERSION_MISMATCH')
    return c

class SecondGapBridge(GuardedGapBridge):
    def __init__(self,store,lock,packet,pin,*,verify_code,connection):
        self.packet,self.pin=packet,pin
        self.connection=deepcopy(connection)
        approval=validate_packet(packet,pin,verify_code())
        super().__init__(store,lock,approval,verify_code=verify_code)

    def _code(self):
        if validate_packet(self.packet,self.pin,self.verify_code())!=self.approval:raise CollectorError('SECOND_REVIEW_CHANGED')
        verify_frozen(SOURCE_ROOT)

    def _connection(self):
        a=self.approval
        evidence=self.connection
        if set(evidence)!={'format','observed_at','backend_document_verified','connection_attempts','component_sha256','config_sha256'} or evidence['format']!='b-gap-second-connection/1' or evidence['backend_document_verified'] is not True or type(evidence['connection_attempts']) is not int or evidence['connection_attempts']!=1:
            raise CollectorError('SECOND_CONNECTION_REQUIRED')
        observed=datetime.fromisoformat(evidence['observed_at'])
        if observed.utcoffset() is None or not datetime.fromisoformat(a['stopped_at'])<observed<=datetime.now(timezone.utc) or (datetime.now(timezone.utc)-observed).total_seconds()>600:
            raise CollectorError('SECOND_CONNECTION_STALE')
        for field in ('component_sha256','config_sha256'):
            if not isinstance(evidence[field],str) or not re.fullmatch('[a-f0-9]{64}',evidence[field]):raise CollectorError('SECOND_CONNECTION_REQUIRED')
        return evidence

    def open(self,run,client):
        a=self.approval
        if client!=CLIENT or run!=a['checkpoint']['run_id']:raise CollectorError('SECOND_SELECTED_RUN')
        if self.used or self.owned:raise CollectorError('SECOND_ALREADY_USED')
        require_lock(self.s,self.lock)
        evidence=self._connection()
        q=queue_class().__new__(queue_class());q.s,q.db,q.stage=self.s,self.s.db,{}
        with self.s.transaction():
            self._code()
            if checkpoint(self.s,run)!=a['checkpoint'] or prior_receipts(self.s,run)!=a['prior_receipts']:
                raise CollectorError('SECOND_STOP_CHANGED')
            if self.s.db.execute("SELECT 1 FROM runs WHERE state='running'").fetchone():raise CollectorError('TASK_ALREADY_RUNNING')
            row,meta=self.s._run(run);g=meta.get('gap_repair',{})
            if row[1] not in {'failed','disconnected'} or g.get('code_hash')!=CODE_HASH or meta['finished_at']!=a['stopped_at'] or meta['expires_at']!=a['absolute_deadline']:
                raise CollectorError('SECOND_STOP_CHANGED')
            q.parents(meta)
            self.s.db.execute('''CREATE TABLE IF NOT EXISTS guarded_gap_second_activations(
                run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                approval_id TEXT NOT NULL UNIQUE,cipher BLOB NOT NULL)''')
            if self.s.db.execute('SELECT 1 FROM guarded_gap_second_activations WHERE run_id=? OR approval_id=?',(run,a['approval_id'])).fetchone():raise CollectorError('SECOND_ALREADY_USED')
            operations=[{'id':key,'target':target,'phase':phase,'state':state,'operation':self.s._decrypt(cipher)}
                for key,target,phase,state,cipher in self.s.db.execute('SELECT id,target,phase,state,cipher FROM gap_operations WHERE run_id=? ORDER BY rowid',(run,))]
            future_chain_guard(operations)
            pending=[x for x in operations if x['state']=='pending']
            if not pending:raise CollectorError('GAP_NO_UNATTEMPTED')
            accounts=[]
            for x in pending:
                if x['operation']['domain']=='account':
                    if x['target'] is not None or x['phase']!=-1 or x['operation']!={'domain':'account','task':{'op':'account'},'context':{}}:raise CollectorError('ACCOUNT_CHECK_REQUIRED')
                    accounts.append(x['id'])
            if len(accounts)>1 or 'account' not in meta['scope']['domains']:raise CollectorError('ACCOUNT_CHECK_REQUIRED')
            preflight=accounts[0] if accounts else q.enqueue(run,None,-1,{'domain':'account','task':{'op':'account'},'context':{}})
            generation=uuid.uuid4().hex
            receipt={'format':FORMAT,'approval_id':a['approval_id'],'before':a['checkpoint'],'prior_receipts':a['prior_receipts'],
                'connection':evidence,'preflight':preflight,'generation':generation,'started_at':now(),
                'original_scope_complete':False,'business_queue_replanned':False,'reading_preserved':True}
            self.s.db.execute('INSERT INTO guarded_gap_second_activations VALUES(?,?,?)',(run,a['approval_id'],self.s._encrypt(receipt)))
            meta['gap_repair'].setdefault('client_resumes',[]).append({'client':CLIENT,'at':now(),'approval_id':a['approval_id'],'kind':'second_existing_pending_only'})
            meta.update(finished_at=None,stop_reason=None)
            self.s.db.execute("UPDATE runs SET generation=?,state='running',cipher=? WHERE id=?",(generation,self.s._encrypt(meta),run))
            if prior_receipts(self.s,run)!=a['prior_receipts']:raise CollectorError('SECOND_PRIOR_CHANGED')
            require_lock(self.s,self.lock);q.parents(meta);self._code();self._connection()
        self.used=True;self.queue,self.preflight=q,preflight;self.owned={'run_id':run,'generation':generation}
        return {**self.owned,'owner':g['owner'],'app':g['app'],'scope':meta['scope'],'unresolved_reading':a['prior_receipts']['reading']['count']}
