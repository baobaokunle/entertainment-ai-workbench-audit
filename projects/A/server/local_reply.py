"""Finite on-demand reply authority over the existing AI queue and source rows.

No model/worker startup or data promotion. Requests grant only this live Web
session and a captured execution generation; persisted queued jobs grant nothing.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import secrets

from .common import AppError
from .db import digest
from .derived_import import check_message
from .local_notes import LocalNotesAccess


@dataclass(eq=False)
class ReplyTicket:
    job_key:str
    token:str
    principal:object
    context_digest:str
    execution_generation:int
    nonce:str
    lifecycle:int=0


class LocalReplyAccess:
    def __init__(self,app):
        notes=getattr(app,'local_notes',None)
        if (not isinstance(notes,LocalNotesAccess) or notes.app is not app or app.ai is None or
                notes.selection.get('format')!='dfa-local-access-1' or notes.selection.get('reply_on_demand') is not True or
                app.ai.execution_policy is not None or app.ai.source_guard is not None):
            raise AppError(403,'LOCAL_REPLY_SELECTION_REQUIRED','需要明确本地按需回复、原备注保护和已有模型表，不自动初始化。')
        self.app,self.notes,self.service,self.db,self.auth=app,notes,app.ai,app.db,app.auth
        self.account=notes.account;self.tickets={};self.host_session=None;self.worker=None
        self.lifecycle=0;self.latest_requests={};self.read_grants={};self.engine=None
        self.service.source_guard=self.check_sources
        self.service.execution_policy=self

    def check_sources(self,rows,conn):
        """Formal revision -> independent fact -> unchanged approved pending row."""
        binding=self.notes.binding
        receipts=[]
        for row in rows:
            try:receipt=check_message(self.db,row)
            except AppError:self._source_error()
            if receipt is None or row['account_key']!=self.account:self._source_error()
            reference=receipt['lineage']['candidate']
            origin=conn.execute('SELECT device_key FROM message_observation WHERE observation_key=?',
                (receipt['observation_key'],)).fetchone()
            if (reference['binding']!=binding or origin is None or origin[0]!=self.notes.wanted['device_key'] or
                    any(reference[k]!=self.notes.selection['selection'][k] for k in ('snapshot_sha256','preview_sha256'))):self._source_error()
            receipts.append({'message_key':row['message_key'],'revision':row['active_body_revision'],
                'receipt':receipt})
        return digest({'binding':binding,'selection':self.notes.selection['selection'],'observations':receipts})

    @staticmethod
    def _source_error():
        raise AppError(409,'LOCAL_REPLY_SOURCE_UNVERIFIED','回复上下文未能唯一对应获准来源，保留未知，不处理其他资料。')

    @contextmanager
    def authorized(self,ticket):
        # Caller owns DB lock before entering; lock remains through COMMIT.
        with self.auth.authorized(ticket.token,scope='model:request',account_key=self.account) as principal:
            if principal!=ticket.principal:raise AppError(403,'LOCAL_REPLY_AUTH_CHANGED','回复请求会话已变化。')
            self.notes.check(principal)
            if self.engine is not None:self.engine.require_private_runtime()
            yield principal

    def validate_ticket(self,ticket,conn):
        if self.tickets.get(ticket.job_key) is not ticket:
            raise AppError(409,'LOCAL_REPLY_INTERRUPTED','本次主动请求已停止，不采用迟到结果。')
        self._validate_binding(ticket,conn)

    def _validate_binding(self,ticket,conn):
        if (not self.worker_active(ticket.lifecycle) or
                self.latest_requests.get(ticket.job_key)!=ticket.nonce):
            raise AppError(409,'LOCAL_REPLY_INTERRUPTED','本次主动请求已停止，不显示旧结果。')
        principal=self.auth.authenticate(ticket.token,scope='model:request',account_key=self.account)
        if principal!=ticket.principal:raise AppError(403,'LOCAL_REPLY_AUTH_CHANGED','回复请求会话已变化。')
        self.notes.check(principal)
        if self.engine is not None:self.engine.require_private_runtime()
        job=self.service.store.raw_job(ticket.job_key,conn)
        visible=self.service.store.read(ticket.job_key,conn)
        if job['context_digest']!=ticket.context_digest or visible['execution']['generation']!=ticket.execution_generation:
            raise AppError(409,'LOCAL_REPLY_INTERRUPTED','本次回复执行版本已变化。')
        self._job(job,principal,conn)

    def _job(self,job,principal,conn):
        event=conn.execute('SELECT event_json FROM domain_event WHERE event_id=?',(job['request_event_id'],)).fetchone()
        event=json.loads(event[0]) if event else {}
        payload=event.get('payload',{});actor=event.get('actor',{})
        if (job['account_key']!=self.account or job['purpose']!='reply' or payload.get('on_demand') is not True or
                payload.get('purpose')!='reply' or payload.get('job_key')!=job['job_key'] or
                payload.get('context_digest')!=job['context_digest'] or
                actor.get('operator_key')!=principal.operator_key or actor.get('device_key')!=principal.device_key):
            raise AppError(403,'LOCAL_REPLY_REQUEST_REQUIRED','只处理原操作者明确请求的同账号回复。')
        context=json.loads(job['context_json']);self.service.store.validate_context_current(context,conn)
        current,_=self.service._context(self.account,job['person_key'],'reply')
        if digest(current)!=job['context_digest']:raise AppError(409,'AI_SOURCE_STALE','本次回复上下文已变化。')

    def bind_host_session(self,session):
        principal=self.auth.authenticate(session['token'],scope='model:request',account_key=self.account)
        if principal!=session['principal']:raise AppError(403,'LOCAL_REPLY_AUTH_CHANGED','本机处理会话不一致。')
        self.notes.check(principal)
        self.host_session={'token':session['token'],'principal':principal}

    def host_active(self):
        if self.host_session is None or self.notes.blocked or self.app.read_only or self.app.writes_enabled:return False
        if self.engine is not None and (self.engine.closed or self.engine.protection_failed):return False
        try:
            current=self.auth.authenticate(self.host_session['token'],scope='model:request',account_key=self.account)
            return current==self.host_session['principal'] and self.service.execution_policy is self and self.app.auth is self.auth
        except AppError:return False

    def require_worker(self,principal,account):
        if not self.host_active() or principal!=self.host_session['principal'] or account!=self.account:
            raise AppError(403,'LOCAL_REPLY_AUTH_CHANGED','需要当前本机管理会话和唯一账号。')
        self.notes.check(principal)

    def require_source(self,principal):
        """Preflight a reliable derived message before constructing the engine.

        Actual per-person tokenized context is checked again at each request.
        No profile, source enrollment, data promotion or model IO is done here.
        """
        with self.db.lock:
            self.require_worker(principal,self.account)
            # Bound the approved derived set, not the newest account-wide rows:
            # unrelated ordinary messages must not crowd out this source.
            rows=self.db.conn.execute('''SELECT m.* FROM message m
                JOIN message_revision r ON r.message_key=m.message_key AND r.revision=m.active_body_revision
                JOIN message_observation o ON o.observation_key=r.observation_key
                WHERE m.account_key=? AND m.kind='text' AND m.identity_state='exact' AND m.direction IN ('in','out')
                AND json_extract(o.payload_json,'$.local_import_derivation.candidate.binding.source_id')=?
                AND json_extract(o.payload_json,'$.local_import_derivation.candidate.binding.source_version')=?
                AND json_extract(o.payload_json,'$.local_import_derivation.candidate.binding.account_namespace')=?
                ORDER BY m.occurred_lower_utc DESC,m.message_key DESC LIMIT 101''',
                (self.account,self.notes.binding['source_id'],self.notes.binding['source_version'],self.notes.binding['account_namespace'])).fetchall()
            if len(rows)>100:raise AppError(409,'LOCAL_REPLY_CONTEXT_REQUIRED','本次回复依据超出获准首批范围，未启动模型。')
            for row in rows:
                try:
                    self.service._identity(self.account,row['person_key'])
                    self.check_sources([dict(row)],self.db.conn)
                except AppError:continue
                return
            raise AppError(409,'LOCAL_REPLY_CONTEXT_REQUIRED','尚无可核对的同人聊天依据，未启动模型。')

    def bind_engine(self,engine):
        from .local_model_runtime import OwnedLocalModel
        if (not isinstance(engine,OwnedLocalModel) or self.engine is not None or
                engine.data!=self.app.paths.root or engine.protection_shell is None):
            raise AppError(403,'LOCAL_REPLY_OWNED_ENGINE_REQUIRED','有限回复只使用本次受保护的固定本机模型。')
        engine.require_private_runtime();self.engine=engine

    def start_worker(self,worker):
        with self.db.lock:
            self.require_worker(worker.principal,worker.account)
            if self.worker is not None and self.worker is not worker and self.worker.enabled:
                raise AppError(409,'AI_WORKER_BUSY','已有本机回复处理器。')
            self.lifecycle+=1;self.tickets.clear();self.read_grants.clear();self.latest_requests.clear();self.worker=worker

    def worker_active(self,lifecycle):
        return (lifecycle==self.lifecycle and self.worker is not None and self.worker.enabled and
                self.worker.policy is self and self.host_active())

    def request(self,token,principal,data):
        if (data['account_key']!=self.account or data['purpose']!='reply' or data['on_demand'] is not True or
                not self.host_active() or self.worker is None or not self.worker.enabled or
                not self.service.client.status()['available']):
            raise AppError(403,'LOCAL_REPLY_REQUEST_REQUIRED','仅允许已启用本机处理中的主动回复请求。')
        temporary=ReplyTicket('',token,principal,'',0,secrets.token_hex(16))
        with self.db.lock,self.authorized(temporary):
            lifecycle=self.lifecycle
            if not self.worker_active(lifecycle):raise AppError(409,'LOCAL_REPLY_INTERRUPTED','本机处理已停止。')
            with self.db.transaction() as conn:
                result=self.service.request(self.account,data['person_key'],'reply',principal,on_demand=True)
                if result.get('job_key') is None:return result
                job=self.service.store.raw_job(result['job_key'],conn);self._job(job,principal,conn)
                # An explicit new request revokes the captured old ticket before
                # any recovery. Old inference can never adopt the new session.
                self.tickets.pop(job['job_key'],None)
                self.read_grants.pop(job['job_key'],None)
                request_nonce=secrets.token_hex(16)
                self.latest_requests[job['job_key']]=request_nonce
                if result['state']=='running':
                    self.service.interrupt_running(self.account,job_key=job['job_key'])
                    result=self.service.store.read(job['job_key'],conn)
                if result['state'] in ('failed','timed_out','unavailable'):
                    result=self.service.retry(job['job_key'],result['execution']['generation'],result['revision'],principal)
                self.notes.check(principal)
                if not self.worker_active(lifecycle):raise AppError(409,'LOCAL_REPLY_INTERRUPTED','本次处理生命周期已变化。')
                if self.auth.authenticate(token,scope='model:request',account_key=self.account)!=principal:
                    raise AppError(403,'LOCAL_REPLY_AUTH_CHANGED','请求提交前会话已变化。')
        # Publishing is separate from the durable COMMIT; failure leaves an
        # ungranted job, never a compensating write to an unprotected database.
        if result['state']=='queued':self.publish(token,principal,result,request_nonce,lifecycle)
        else:self.grant_read(token,principal,result,request_nonce,lifecycle)
        return self.read(token,principal,result['job_key'])

    def publish(self,token,principal,result,request_nonce,lifecycle):
        ticket=ReplyTicket(result['job_key'],token,principal,result['context_digest'],result['execution']['generation'],request_nonce,lifecycle)
        with self.db.lock,self.authorized(ticket):
            if (not self.worker_active(lifecycle) or self.latest_requests.get(ticket.job_key)!=request_nonce):
                raise AppError(409,'LOCAL_REPLY_INTERRUPTED','本机处理或最新请求已变化，保留未执行请求。')
            job=self.service.store.raw_job(ticket.job_key,self.db.conn);self._job(job,principal,self.db.conn)
            visible=self.service.store.read(ticket.job_key,self.db.conn)
            if visible['state']!='queued' or visible['execution']['generation']!=ticket.execution_generation:
                raise AppError(409,'LOCAL_REPLY_INTERRUPTED','请求版本已变化，未授予处理权限。')
            if self.auth.authenticate(token,scope='model:request',account_key=self.account)!=principal:
                raise AppError(403,'LOCAL_REPLY_AUTH_CHANGED','发布处理许可前会话已变化。')
            self.tickets[ticket.job_key]=ticket
            # The same captured generation advances only when this ticket is
            # claimed. Finishing execution removes the ticket, not read access.
            self.read_grants[ticket.job_key]=ticket
        return ticket

    def grant_read(self,token,principal,result,request_nonce,lifecycle):
        grant=ReplyTicket(result['job_key'],token,principal,result['context_digest'],
            result['execution']['generation'],request_nonce,lifecycle)
        with self.db.lock,self.authorized(grant):
            self._validate_binding(grant,self.db.conn)
            self.read_grants[grant.job_key]=grant

    def read(self,token,principal,job_key):
        # A login by the same operator/device is not the same Web session.
        # Never look up a persisted job before checking its in-memory grant.
        with self.db.lock:
            grant=self.read_grants.get(job_key)
            if grant is None or grant.token!=token or grant.principal!=principal:
                raise AppError(403,'LOCAL_REPLY_REQUEST_REQUIRED','只能查看本次登录中主动请求的回复。')
            try:
                with self.authorized(grant):
                    self._validate_binding(grant,self.db.conn)
                    result=self.service.get(job_key,principal)
                    self._validate_binding(grant,self.db.conn)
                    return result
            except Exception:
                self.read_grants.pop(job_key,None)
                raise

    def status(self,token,principal,person):
        result={'available':False,'request_enabled':False,'allowed_purposes':['reply'],
            'controls_enabled':False,'preferences':{},'processing':'explicit_local_reply',
            'scheduler':'not_enabled','reason':'local_reply_not_running','real_quality':'NOT_RUN'}
        with self.db.lock,self.authorized(ReplyTicket('',token,principal,'',0,'')):
            if not self.worker_active(self.lifecycle):return result
            result['scheduler']='explicit_local_reply'
            if not self.service.client.status()['available']:
                result['reason']='model_unavailable';return result
            if not person:
                result['reason']='person_required';return result
            try:self.service._context(self.account,person,'reply')
            except AppError:
                result['reason']='reliable_chat_context_unavailable';return result
            result.update(available=True,request_enabled=True,reason=None)
            return result

    def next_ticket(self):
        with self.db.lock:
            for ticket in list(self.tickets.values()):
                row=self.db.conn.execute('SELECT state FROM ai_job WHERE job_key=?',(ticket.job_key,)).fetchone()
                if row is not None and row[0]=='queued':return ticket
        return None

    def before_inference(self,ticket):
        with self.db.lock,self.authorized(ticket):self.validate_ticket(ticket,self.db.conn)

    def discard(self,ticket):
        with self.db.lock:
            if self.tickets.get(ticket.job_key) is ticket:self.tickets.pop(ticket.job_key,None)

    def stop(self,worker):
        with self.db.lock:
            if self.worker is worker:
                self.lifecycle+=1;self.tickets.clear();self.read_grants.clear();self.latest_requests.clear()


def attach_local_reply(app):
    if getattr(app,'local_reply',None) is not None:raise AppError(409,'LOCAL_REPLY_ALREADY_ATTACHED','不能重复接管本机回复。')
    grant=LocalReplyAccess(app);app.local_reply=grant;return grant
