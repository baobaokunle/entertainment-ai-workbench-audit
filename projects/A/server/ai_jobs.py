"""Explicit durable single-worker local annotations; no import-time work or model activation."""
import json
import threading
import time
from contextlib import contextmanager,nullcontext
from .common import AppError,canonical_json,utc_now
from .db import digest,stable_key
from .local_ai import LocalModelClient,make_prompt,PROMPT_VERSION
from .ai_annotations import AnnotationStore,validate_result
from .local_deletion import guarded,require_ready,sql_visible,sql_visible_job,is_hidden_job,event_requires_hold

PURPOSES=('emotion','topics','reply')
REQUEST_FIELDS={'job_key','purpose','context','context_digest','model_digest','prompt_version','input_tokens','state','on_demand'}

class AiService:
    def __init__(self,db,client=None,*,approved_model_digests=(),monotonic=time.monotonic):
        self.db=db;self.store=AnnotationStore(db);self.client=client or LocalModelClient()
        self.approved_model_digests=set(approved_model_digests);self.monotonic=monotonic;self.worker_lock=threading.Lock()
        self.scheduler_state='not_enabled'
        self.execution_policy=None;self.source_guard=None  # Explicit local host attachment only.
        registered=self.client.status().get('digest')
        if registered:self.approved_model_digests.add(registered)
    @guarded
    def status(self,account_key=None):
        with self.db.lock:
            clause=' AND account_key=?' if account_key is not None else ''
            values=(account_key,) if account_key is not None else ()
            queued=self.db.conn.execute("SELECT COUNT(*) FROM ai_job j WHERE state='queued' AND "+sql_visible_job(self.db)+clause,values).fetchone()[0]
            running=self.db.conn.execute("SELECT COUNT(*) FROM ai_job j WHERE state='running' AND "+sql_visible_job(self.db)+clause,values).fetchone()[0]
        return {**self.client.status(),'queue':{'waiting':queued,'running':running,'capacity':20,'concurrency':1},
                'budget':{'messages':24,'context_tokens':4096,'input_tokens':3072,'output_tokens':512,'timeout_seconds':90},
                'real_quality':'NOT_RUN','native_dom':'unverified','scheduler':self.scheduler_state,'processing':'explicit_fictional_test_only'}
    @guarded
    def preferences(self,account_key,person_key,principal):
        principal.require('model:read',account_key);self._identity(account_key,person_key)
        with self.db.lock:return {purpose:self.store.preference(account_key,person_key,purpose,self.db.conn) for purpose in PURPOSES}
    def _actor(self,principal):
        return {'operator_key':principal.operator_key,'device_key':principal.device_key,'session_id':principal.session_id}
    def _identity(self,account,person,conn=None):
        if conn is None:
            with self.db.lock:return self._identity(account,person,self.db.conn)
        row=conn.execute('SELECT object_kind,verified FROM contact WHERE account_key=? AND person_key=?',(account,person)).fetchone()
        if row is None or row['object_kind']!='person' or not row['verified']:
            raise AppError(409,'AI_IDENTITY_UNVERIFIED','A verified personal object is required for annotations.')
    @guarded
    def _source_rows(self,account,person):
        with self.db.lock:
            self._identity(account,person,self.db.conn)
            predicate="m.account_key=? AND m.person_key=? AND m.kind='text' AND m.identity_state='exact' AND m.direction IN ('in','out') AND json_extract(m.quality_json,'$.identity')='verified' AND json_extract(m.quality_json,'$.body')='verified' AND json_extract(m.quality_json,'$.direction')='verified'"
            predicate+=' AND '+sql_visible(self.db)
            total=self.db.conn.execute('SELECT COUNT(*) FROM message m WHERE account_key=? AND person_key=? AND '+sql_visible(self.db),(account,person)).fetchone()[0]
            reliable_count=self.db.conn.execute('SELECT COUNT(*) FROM message m WHERE '+predicate,(account,person)).fetchone()[0]
            rows=[dict(row) for row in self.db.conn.execute('SELECT m.*,r.body,r.body_sha256 FROM message m JOIN message_revision r ON r.message_key=m.message_key AND r.revision=m.active_body_revision WHERE '+predicate+' ORDER BY m.occurred_lower_utc DESC,m.message_key DESC LIMIT 24',(account,person))][::-1]
            source_scope=self.source_guard(rows,self.db.conn) if self.source_guard is not None else None
            from .derived_import import check_message
            derived=[receipt for row in rows if (receipt:=check_message(self.db,row)) is not None]
        generation_input={'count':reliable_count,'sources':[{key:row[key] for key in ('message_key','active_body_revision','body_sha256','direction','occurred_lower_utc','occurred_upper_utc','time_precision','quality_json')} for row in rows]}
        if source_scope is not None:generation_input['source_scope']=source_scope
        if derived:generation_input['derived_imports']=derived
        generation=digest(generation_input)
        return rows,total-reliable_count,max(0,reliable_count-len(rows)),generation
    @guarded
    def _context(self,account,person,purpose):
        rows,skipped,dropped,generation=self._source_rows(account,person)
        messages=[]
        for row in rows:
            quality=json.loads(row['quality_json'])
            messages.append({'message_key':row['message_key'],'body_revision':row['active_body_revision'],'body_sha256':row['body_sha256'],
                             'body':row['body'],'direction':row['direction'],'occurred_lower_utc':row['occurred_lower_utc'],
                             'occurred_upper_utc':row['occurred_upper_utc'],'time_precision':row['time_precision'],
                             'time_verified':quality.get('time')=='verified'})
        if not messages:raise AppError(409,'AI_CONTEXT_UNAVAILABLE','No reliable same-person text context is available.')
        while messages:
            times=[message['occurred_lower_utc'] for message in messages]
            ordered=all(message['time_verified'] and message['time_precision']=='second' and message['occurred_lower_utc'] and message['occurred_lower_utc']==message['occurred_upper_utc'] for message in messages) and len(set(times))==len(times)
            context={'account_key':account,'person_key':person,'messages':messages,'dropped_messages':dropped,'skipped_unreliable':skipped,'order_verified':bool(ordered),'source_generation':generation}
            count=self.client.count(make_prompt(context,purpose))
            if count<=3072:return context,count
            if len(messages)==1:raise AppError(422,'AI_CONTEXT_TOO_LARGE','Latest reliable message exceeds the registered tokenizer budget; text was not silently truncated.')
            messages=messages[1:];dropped+=1
        raise AppError(409,'AI_CONTEXT_UNAVAILABLE','Reliable text context is unavailable.')
    @guarded
    def request(self,account_key,person_key,purpose,principal,*,on_demand=False):
        principal.require('model:request',account_key);self._identity(account_key,person_key)
        if purpose not in PURPOSES or type(on_demand)is not bool:raise AppError(422,'AI_PURPOSE_INVALID','Finite read-only purpose required.')
        if purpose=='reply' and not on_demand:raise AppError(422,'AI_REPLY_NOT_REQUESTED','Short references require an explicit human request.')
        with self.db.lock:enabled=self.store.preference(account_key,person_key,purpose,self.db.conn)['enabled']
        if not enabled:return {'state':'disabled','job_key':None,'result':None}
        status=self.client.status();model_digest=status.get('digest') or 'unregistered'
        if status['available']:context,count=self._context(account_key,person_key,purpose);state='queued'
        else:context={'account_key':account_key,'person_key':person_key,'messages':[],'dropped_messages':0,'skipped_unreliable':0,'order_verified':False,'source_generation':'unregistered'};count=0;state='unavailable'
        context_digest=digest(context);job_key=stable_key('ai',account_key,person_key,purpose,context_digest,model_digest,PROMPT_VERSION)
        with self.db.transaction() as conn:
            if conn.execute('SELECT 1 FROM ai_job WHERE job_key=?',(job_key,)).fetchone():return self.store.read(job_key,conn)
            if state=='queued' and conn.execute("SELECT COUNT(*) FROM ai_job WHERE state='queued'").fetchone()[0]>=20:raise AppError(429,'AI_QUEUE_FULL','Annotation queue is full; message persistence remains available.')
            payload={'job_key':job_key,'purpose':purpose,'context':context,'context_digest':context_digest,'model_digest':model_digest,
                     'prompt_version':PROMPT_VERSION,'input_tokens':count,'state':state,'on_demand':on_demand}
            event_id=self.db.append_event(conn,account_key=account_key,target_key=person_key,event_type='ai.request',payload=payload,channel='model',actor=self._actor(principal))
            event=json.loads(conn.execute('SELECT event_json FROM domain_event WHERE event_id=?',(event_id,)).fetchone()[0]);self.apply_request(conn,event)
            return self.store.read(job_key,conn)
    @guarded
    def get(self,job_key,principal):
        job=self.store.raw_job(job_key);principal.require('model:read',job['account_key'])
        result=self.store.read(job_key)
        if result['state'] in ('ready','unknown','corrected'):
            try:
                model_status=self.client.status()
                if job['prompt_version']!=PROMPT_VERSION or (model_status['available'] and model_status['digest']!=job['model_digest']):raise AppError(409,'AI_SOURCE_STALE','Registered model or prompt version changed.')
                self.store.validate_context_current(json.loads(job['context_json']))
                if self._source_rows(job['account_key'],job['person_key'])[3]!=json.loads(job['context_json']).get('source_generation'):raise AppError(409,'AI_SOURCE_STALE','Reliable source generation changed.')
                if self.client.status()['available']:
                    current,_=self._context(job['account_key'],job['person_key'],job['purpose'])
                    if digest(current)!=job['context_digest']:raise AppError(409,'AI_SOURCE_STALE','Context changed.')
            except AppError:result['state']='stale';result['result']=None
        return result
    def process_next(self,account_key=None,*,active_guard=None,commit_guard=None,ticket=None):
        policy=self.execution_policy
        if policy is not None and ticket is None:return {'state':'idle'}
        @contextmanager
        def transaction(*,result=False):
            # In limited mode every write phase, including error recovery, owns
            # authorization through COMMIT. The policy supplies no model IO.
            with self.db.lock:
                with (policy.authorized(ticket) if policy is not None else nullcontext()):
                    with (commit_guard() if result and commit_guard is not None else nullcontext()):
                        with self.db.transaction() as conn:
                            if policy is not None:policy.validate_ticket(ticket,conn)
                            yield conn
                            if policy is not None:policy.validate_ticket(ticket,conn)
        if not self.worker_lock.acquire(blocking=False):return {'state':'busy'}
        try:
            with transaction() as conn:
                require_ready(self.db)
                if active_guard is not None and not active_guard():return {'state':'stopped'}
                clause=' AND account_key=?' if account_key is not None else ''
                values=(account_key,) if account_key is not None else ()
                if policy is not None:clause+=' AND job_key=? AND purpose=\'reply\'';values+=(ticket.job_key,)
                row=conn.execute("SELECT j.* FROM ai_job j WHERE state='queued' AND "+sql_visible_job(self.db)+clause+" ORDER BY CASE purpose WHEN 'reply' THEN 0 ELSE 1 END,created_at_utc,job_key LIMIT 1",values).fetchone()
                if row is None:return {'state':'idle'}
                job=dict(row);job_key=job['job_key'];visible=self.store.read(job_key,conn)
                if visible['state'] in ('hidden','canceled','corrected','correction_conflict'):return visible
                if job['prompt_version']!=PROMPT_VERSION:
                    conn.execute("UPDATE ai_job SET state='stale',error_code='AI_SOURCE_STALE',updated_at_utc=? WHERE job_key=?",(utc_now(),job_key));return self.store.read(job_key,conn)
                if not self.client.status()['available'] or self.client.status()['digest']!=job['model_digest']:
                    conn.execute("UPDATE ai_job SET state='unavailable',error_code='AI_MODEL_UNREGISTERED',updated_at_utc=? WHERE job_key=?",(utc_now(),job_key));return self.store.read(job_key,conn)
                conn.execute('INSERT INTO ai_execution VALUES(?,0,0) ON CONFLICT(job_key) DO NOTHING',(job_key,))
                execution=conn.execute('SELECT * FROM ai_execution WHERE job_key=?',(job_key,)).fetchone()
                if execution['attempts']>=3:
                    conn.execute("UPDATE ai_job SET state='failed',error_code='AI_RETRY_LIMIT' WHERE job_key=?",(job_key,));return self.store.read(job_key,conn)
                generation=execution['generation']+1
                conn.execute('UPDATE ai_execution SET attempts=attempts+1,generation=? WHERE job_key=?',(generation,job_key))
                conn.execute("UPDATE ai_job SET state='running',updated_at_utc=? WHERE job_key=?",(utc_now(),job_key))
                if policy is not None:ticket.execution_generation=generation
            started=self.monotonic()
            try:
                context=json.loads(job['context_json']);self.store.validate_context_current(context)
                current,_=self._context(job['account_key'],job['person_key'],job['purpose'])
                if digest(current)!=job['context_digest']:raise AppError(409,'AI_SOURCE_STALE','Source context changed before inference.')
                with self.db.lock:
                    require_ready(self.db)
                    if is_hidden_job(self.db,job_key,job['account_key']):return {'job_key':job_key,'state':'suppressed','result':None,'copies_state':'copies_pending'}
                    self.store.validate_context_current(context,self.db.conn)
                    prompt=make_prompt(context,job['purpose'])
                if active_guard is not None and not active_guard():raise AppError(409,'AI_INTERRUPTED','Worker authorization or lifecycle ended before inference.')
                if policy is not None:policy.before_inference(ticket)
                raw=self.client.generate(prompt,timeout=90)
                if active_guard is not None and not active_guard():raise AppError(409,'AI_INTERRUPTED','Worker authorization or lifecycle ended during inference.')
                if self.monotonic()-started>90:raise AppError(504,'AI_TIMEOUT','Local annotation exceeded its time budget.')
                result=validate_result(raw,context,job['purpose'])
                # DB -> Auth -> short transaction. Hold authorization through COMMIT,
                # never through model inference; same-instance revocation is ordered.
                with transaction(result=True) as conn:
                    require_ready(self.db)
                    if is_hidden_job(self.db,job_key,job['account_key']):return {'job_key':job_key,'state':'suppressed','result':None,'copies_state':'copies_pending'}
                    current_state=self.store.read(job_key,conn)
                    if current_state['state']!='running' or current_state['execution']['generation']!=generation:return current_state
                    self.store.validate_context_current(context,conn)
                    current,_=self._context(job['account_key'],job['person_key'],job['purpose'])
                    if digest(current)!=job['context_digest']:raise AppError(409,'AI_SOURCE_STALE','Source context changed during inference.')
                    if active_guard is not None and not active_guard():raise AppError(409,'AI_INTERRUPTED','Worker authorization ended before result commit.')
                    payload={'job_key':job_key,'context_digest':job['context_digest'],'model_digest':job['model_digest'],'prompt_version':job['prompt_version'],'result':result}
                    event_id=self.db.append_event(conn,account_key=job['account_key'],target_key=job['person_key'],event_type='ai.result',payload=payload,parents=[job['request_event_id']],channel='model')
                    event=json.loads(conn.execute('SELECT event_json FROM domain_event WHERE event_id=?',(event_id,)).fetchone()[0]);self.apply_result(conn,event)
                    visible=self.store.read(job_key,conn)
                    if active_guard is not None and not active_guard():raise AppError(409,'AI_INTERRUPTED','Worker authorization ended while writing the result.')
                    return visible
            except AppError as error:
                state='queued' if error.code=='AI_INTERRUPTED' else 'stale' if error.code=='AI_SOURCE_STALE' else 'timed_out' if error.code=='AI_TIMEOUT' else 'unavailable' if error.status==503 else 'failed'
                with transaction() as conn:
                    require_ready(self.db)
                    if is_hidden_job(self.db,job_key,job['account_key']):return {'job_key':job_key,'state':'suppressed','result':None,'copies_state':'copies_pending'}
                    visible=self.store.read(job_key,conn)
                    if visible['state']=='running' and visible['execution']['generation']==generation:conn.execute('UPDATE ai_job SET state=?,error_code=?,updated_at_utc=? WHERE job_key=?',(state,error.code,utc_now(),job_key))
                    return self.store.read(job_key,conn)
            except Exception:
                with transaction() as conn:
                    require_ready(self.db)
                    if is_hidden_job(self.db,job_key,job['account_key']):return {'job_key':job_key,'state':'suppressed','result':None,'copies_state':'copies_pending'}
                    visible=self.store.read(job_key,conn)
                    if visible['state']=='running' and visible['execution']['generation']==generation:conn.execute("UPDATE ai_job SET state='failed',error_code='AI_INTERNAL_ERROR',updated_at_utc=? WHERE job_key=?",(utc_now(),job_key))
                    return self.store.read(job_key,conn)
        except Exception:
            if policy is None:raise
            policy.discard(ticket)
            return {'job_key':ticket.job_key,'state':'interrupted','result':None}
        finally:
            if policy is not None:policy.discard(ticket)
            self.worker_lock.release()
    @guarded
    def interrupt_running(self,account_key,*,job_key=None):
        """Local lifecycle recovery; generation invalidates an already-running late response."""
        with self.db.transaction() as conn:
            clause=' AND job_key=?' if job_key is not None else ''
            rows=conn.execute("SELECT job_key FROM ai_job WHERE account_key=? AND state='running'"+clause,(account_key,job_key) if job_key is not None else (account_key,)).fetchall()
            for row in rows:
                visible=self.store.read(row[0],conn)
                if visible['state']!='running':continue
                conn.execute('INSERT INTO ai_execution VALUES(?,0,1) ON CONFLICT(job_key) DO UPDATE SET generation=generation+1',(row[0],))
                conn.execute("UPDATE ai_job SET state='queued',error_code='AI_INTERRUPTED',updated_at_utc=? WHERE job_key=?",(utc_now(),row[0]))
            return {'state':'recovered','jobs':len(rows)}
    @guarded
    def retry(self,job_key,expected_generation,expected_revision,principal):
        """Explicit bounded retry of the exact cached context; never substitutes new messages."""
        with self.db.transaction() as conn:
            job=self.store.raw_job(job_key,conn);principal.require('model:request',job['account_key']);visible=self.store.read(job_key,conn)
            if type(expected_generation)is not int or expected_generation!=visible['execution']['generation'] or not isinstance(expected_revision,list) or expected_revision!=visible['revision']:raise AppError(409,'AI_RETRY_CONFLICT','Retry execution or control revision changed.')
            if visible['state'] not in ('failed','timed_out','unavailable'):raise AppError(409,'AI_RETRY_STATE','Only an explicit failed task may be retried.')
            if visible['execution']['attempts']>=3:raise AppError(429,'AI_RETRY_LIMIT','Three attempts per exact context is the maximum.')
            if not self.store.preference(job['account_key'],job['person_key'],job['purpose'],conn)['enabled']:raise AppError(409,'AI_RETRY_DISABLED','Annotation preference is disabled.')
            status=self.client.status()
            if job['prompt_version']!=PROMPT_VERSION:raise AppError(409,'AI_SOURCE_STALE','Previous prompt contract cannot be retried with a new contract.')
            if not status['available'] or status['digest']!=job['model_digest']:raise AppError(503,'AI_MODEL_UNREGISTERED','Exact registered model is required; create a new request after explicit registration.')
            self.store.validate_context_current(json.loads(job['context_json']),conn)
            context,_=self._context(job['account_key'],job['person_key'],job['purpose'])
            if digest(context)!=job['context_digest']:raise AppError(409,'AI_SOURCE_STALE','Retry must preserve the original source generation.')
            if conn.execute("SELECT COUNT(*) FROM ai_job WHERE state='queued'").fetchone()[0]>=20:raise AppError(429,'AI_QUEUE_FULL','Queue capacity exceeded.')
            conn.execute('INSERT INTO ai_execution VALUES(?,0,1) ON CONFLICT(job_key) DO UPDATE SET generation=generation+1',(job_key,))
            conn.execute("UPDATE ai_job SET state='queued',error_code=NULL,updated_at_utc=? WHERE job_key=?",(utc_now(),job_key));return self.store.read(job_key,conn)
    @guarded
    def control(self,job_key,operation,value,expected_revision,principal):
        with self.db.transaction() as conn:
            job=self.store.raw_job(job_key,conn);principal.require('model:correct',job['account_key']);current=self.store.read(job_key,conn)
            if not isinstance(expected_revision,list) or expected_revision!=current['revision']:raise AppError(409,'AI_REVISION_CONFLICT','Annotation control revision changed.')
            if operation not in ('cancel','hide','correct'):raise AppError(422,'AI_CONTROL_INVALID','Finite control required.')
            if operation=='correct':self.store.validate_context_current(json.loads(job['context_json']),conn);value=validate_result(value,json.loads(job['context_json']),job['purpose'])
            elif value is not None:raise AppError(422,'AI_CONTROL_INVALID','Cancel and hide carry no content.')
            event_id=self.db.append_event(conn,account_key=job['account_key'],target_key=job['person_key'],event_type='ai.control',payload={'job_key':job_key,'operation':operation,'value':value},parents=[job['request_event_id'],*current['revision']],channel='model',actor=self._actor(principal))
            event=json.loads(conn.execute('SELECT event_json FROM domain_event WHERE event_id=?',(event_id,)).fetchone()[0]);self.store.apply_control(conn,event);return self.store.read(job_key,conn)
    @guarded
    def set_enabled(self,account_key,person_key,purpose,enabled,expected_revision,principal):
        principal.require('model:correct',account_key);self._identity(account_key,person_key)
        if purpose not in PURPOSES or type(enabled)is not bool:raise AppError(422,'AI_PREFERENCE_INVALID','Finite annotation preference required.')
        with self.db.transaction() as conn:
            current=self.store.preference(account_key,person_key,purpose,conn)
            if expected_revision!=current['revision']:raise AppError(409,'AI_REVISION_CONFLICT','Preference revision changed.')
            event_id=self.db.append_event(conn,account_key=account_key,target_key=person_key,event_type='ai.preference',payload={'purpose':purpose,'enabled':enabled},parents=current['revision'],channel='model',actor=self._actor(principal))
            event=json.loads(conn.execute('SELECT event_json FROM domain_event WHERE event_id=?',(event_id,)).fetchone()[0]);self.store.apply_preference(conn,event);return self.store.preference(account_key,person_key,purpose,conn)
    @guarded
    def apply_request(self,conn,event):
        if event_requires_hold(self.db,event):raise AppError(410,'LOCAL_DELETION_SUPPRESSED','Request references a suppressed object.')
        p=event['payload'];account,person=event['account_key'],event['target_key']
        if event['channel']!='model' or not isinstance(p,dict) or set(p)!=REQUEST_FIELDS or not isinstance(p['context'],dict) or any(not isinstance(p[k],str) for k in ('job_key','context_digest','model_digest','prompt_version')) or p['purpose'] not in PURPOSES or p['prompt_version']!=PROMPT_VERSION or type(p['input_tokens'])is not int or not 0<=p['input_tokens']<=3072 or p['state'] not in ('queued','unavailable') or type(p['on_demand'])is not bool or (p['purpose']=='reply' and not p['on_demand']) or not event.get('actor'):
            raise AppError(422,'AI_REQUEST_INVALID','Bounded authenticated annotation request required.')
        self._identity(account,person,conn)
        if p['context'].get('account_key')!=account or p['context'].get('person_key')!=person or digest(p['context'])!=p['context_digest'] or p['job_key']!=stable_key('ai',account,person,p['purpose'],p['context_digest'],p['model_digest'],p['prompt_version']):raise AppError(422,'AI_CONTEXT_INVALID','Context digest must bind same-account person and complete source versions.')
        self.store.validate_context_current(p['context'],conn)
        if p['state']=='queued' and not p['context']['messages']:raise AppError(422,'AI_CONTEXT_INVALID','Queued annotation requires reliable source text.')
        if p['state']=='queued' and self._source_rows(account,person)[3]!=p['context']['source_generation']:raise AppError(409,'AI_SOURCE_STALE','Request source generation does not match current facts.')
        old=conn.execute('SELECT context_digest FROM ai_job WHERE job_key=?',(p['job_key'],)).fetchone()
        if old:return
        if p['state']=='queued' and conn.execute("SELECT COUNT(*) FROM ai_job WHERE state='queued'").fetchone()[0]>=20:raise AppError(429,'AI_QUEUE_FULL','Annotation queue is full.')
        if p['state']=='queued' and self.client.status()['available'] and self.client.status()['digest']==p['model_digest'] and self.client.count(make_prompt(p['context'],p['purpose']))!=p['input_tokens']:raise AppError(422,'AI_TOKEN_COUNT_INVALID','Input budget must match independently registered tokenizer measurement.')
        now=utc_now()
        conn.execute('INSERT INTO ai_job VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)',(p['job_key'],account,person,p['purpose'],p['state'],p['context_digest'],canonical_json(p['context']).decode(),p['model_digest'],p['prompt_version'],p['input_tokens'],event['event_id'],event['created_at_utc'],now))
    @guarded
    def apply_result(self,conn,event):
        if event_requires_hold(self.db,event):raise AppError(410,'LOCAL_DELETION_SUPPRESSED','Result references a suppressed object.')
        p=event['payload']
        if event['channel']!='model' or not isinstance(p,dict) or set(p)!={'job_key','context_digest','model_digest','prompt_version','result'} or any(not isinstance(p[k],str) for k in ('job_key','context_digest','model_digest','prompt_version')):raise AppError(422,'AI_RESULT_INVALID','Bounded exact result required.')
        try:job=self.store.raw_job(p['job_key'],conn)
        except AppError as error:
            if error.status==404:raise AppError(409,'DEPENDENCY_PENDING','Annotation request must precede result.') from None
            raise
        if (event['account_key'],event['target_key'],p['context_digest'],p['model_digest'],p['prompt_version'])!=(job['account_key'],job['person_key'],job['context_digest'],job['model_digest'],job['prompt_version']) or job['request_event_id'] not in event['parents']:raise AppError(422,'AI_RESULT_INVALID','Result must bind request account, person, context, model and prompt version.')
        if p['model_digest'] not in self.approved_model_digests:raise AppError(409,'DEPENDENCY_PENDING','Result model digest has not been independently approved.')
        context=json.loads(job['context_json']);self.store.validate_context_current(context,conn);result=validate_result(p['result'],context,job['purpose'])
        if self._source_rows(job['account_key'],job['person_key'])[3]!=context['source_generation']:raise AppError(409,'AI_SOURCE_STALE','Result source generation changed.')
        visible=self.store.read(p['job_key'],conn)
        if visible['state'] in ('hidden','canceled','disabled','corrected','correction_conflict'):return
        if not self.store.preference(job['account_key'],job['person_key'],job['purpose'],conn)['enabled']:return
        state='ready' if result['emotion'] or result['topics'] or result['replies'] else 'unknown'
        if job['result_json'] and job['result_json']!=canonical_json(result).decode():raise AppError(409,'AI_RESULT_CONFLICT','Another result exists for this exact context.')
        conn.execute('UPDATE ai_job SET state=?,result_json=?,error_code=NULL,updated_at_utc=? WHERE job_key=?',(state,canonical_json(result).decode(),utc_now(),p['job_key']))
    def register_handlers(self,importer):
        for event_type,handler in [('ai.request',self.apply_request),('ai.result',self.apply_result),('ai.control',self.store.apply_control),('ai.preference',self.store.apply_preference)]:importer.register_handler(event_type,handler)
