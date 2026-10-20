"""Opt-in worker lifecycle; the finite local policy consumes live tickets only.

Imports/constructors never start a thread or model. Existing fictional workers
retain their original queue recovery; finite local workers never recover it.
"""
import threading,time
from contextlib import ExitStack,contextmanager
from .common import AppError

class AiWorker:
    def __init__(self,service,*,fictional=False,clock=time.monotonic,reauthorize=None,commit_guard=None):
        if reauthorize is not None and not callable(reauthorize):raise AppError(400,'AI_WORKER_AUTH_INVALID','Worker reauthorization must be callable.')
        if commit_guard is not None and not callable(commit_guard):raise AppError(400,'AI_WORKER_AUTH_INVALID','Worker commit guard must be callable.')
        self.commit_guard=commit_guard
        self.policy=getattr(service,'execution_policy',None)
        self.reauthorize=reauthorize;self.principal=None
        self.service=service;self.fictional=fictional;self.clock=clock;self.account=None
        self.enabled=False;self.next_batch=None;self.thread=None;self.stop_event=threading.Event();self.pump_lock=threading.Lock()
    def start(self,principal,account_key,*,background=False,daemon=True):
        if type(daemon) is not bool:raise AppError(400,'AI_WORKER_LIFECYCLE_INVALID','Worker daemon setting must be explicit boolean.')
        principal.require('model:request',account_key);principal.require('identities:write',account_key)
        if self.policy is not None:
            from .local_reply import LocalReplyAccess
            if not isinstance(self.policy,LocalReplyAccess) or self.policy.service is not self.service:
                raise AppError(403,'AI_WORKER_AUTH_INVALID','Limited worker requires the existing protected local reply policy.')
            self.policy.require_worker(principal,account_key)
        elif not self.fictional or principal.role!='manager':raise AppError(403,'AI_WORKER_TEST_ONLY','Worker activation is restricted to authenticated fictional manager tests.')
        if self.reauthorize is not None and self.reauthorize()!=principal:raise AppError(403,'AI_WORKER_AUTH_CHANGED','Worker authorization changed before start.')
        if self.thread and self.thread.is_alive():raise AppError(409,'AI_WORKER_BUSY','Previous worker is still stopping.')
        if self.enabled:raise AppError(409,'AI_WORKER_BUSY','Worker is already enabled.')
        if not self.service.client.status()['available']:return {'state':'unavailable','reason':'model_unregistered','started':False}
        if not self.service.worker_lock.acquire(blocking=False):raise AppError(409,'AI_WORKER_BUSY','Another inference is active.')
        try:
            if self.policy is None:self.service.interrupt_running(account_key)
        finally:self.service.worker_lock.release()
        launch_gate=threading.Event() if background else None
        launch_allowed=False
        def enter_after_start():
            launch_gate.wait()
            # A canceled target may arrive after cleanup or a later start.
            # It must return without touching worker, policy or database state.
            if launch_allowed:self._loop()
        try:
            self.principal=principal;self.account=account_key;self.enabled=True;self.next_batch=self.clock()+0.8;self.stop_event.clear();self.service.scheduler_state='explicit_local_reply' if self.policy else 'explicit_fictional_worker'
            if self.policy is not None:self.policy.start_worker(self)
            if background:
                self.thread=threading.Thread(target=enter_after_start,name='DSH-local-reply' if self.policy else 'DSH-fictional-ai',daemon=daemon);self.thread.start()
        except BaseException:
            launch_allowed=False
            self.enabled=False;self.stop_event.set();self.service.scheduler_state='stopped';self.thread=None
            try:
                if self.policy is not None:self.policy.stop(self)
            except BaseException:pass  # Preserve the original startup failure.
            finally:
                if launch_gate is not None:launch_gate.set()
            raise
        if launch_gate is not None:
            # Once the gate can open, retain the original thread even if the
            # caller is interrupted: serving cleanup must wait for its work.
            try:launch_allowed=True
            finally:launch_gate.set()
        return self.status()
    def status(self):return {'state':'running' if self.enabled else 'stopped','background':bool(self.thread and self.thread.is_alive()),'batch_seconds':0.8,'account_key':self.account}
    def _active(self):
        if not self.enabled:return False
        try:
            if self.reauthorize is not None and self.reauthorize()!=self.principal:raise AppError(403,'AI_WORKER_AUTH_CHANGED','Worker authorization changed.')
            if self.policy is not None and not self.policy.host_active():raise AppError(403,'AI_WORKER_AUTH_CHANGED','Local reply host authorization ended.')
            return True
        except Exception:
            # No nested database recovery here: this may run inside a result transaction.
            self.enabled=False;self.stop_event.set();self.service.scheduler_state='stopped';return False
    @contextmanager
    def _commit(self):
        with ExitStack() as scope:
            if self.commit_guard is not None:
                try:scope.enter_context(self.commit_guard())
                except Exception as error:
                    self.enabled=False;self.stop_event.set();self.service.scheduler_state='stopped'
                    raise AppError(409,'AI_INTERRUPTED','Worker authorization ended before result commit.') from error
            if not self._active():raise AppError(409,'AI_INTERRUPTED','Worker lifecycle ended before result commit.')
            yield
    def pump(self):
        if not self._active():return {'state':'stopped'}
        if not self.pump_lock.acquire(blocking=False):return {'state':'busy'}
        try:
            if self.policy is not None:
                ticket=self.policy.next_ticket()
                if ticket is None:return {'state':'idle'}
                return self.service.process_next(account_key=self.account,active_guard=self._active,ticket=ticket)
            now=self.clock()
            with self.service.db.lock:
                reply=self.service.db.conn.execute("SELECT 1 FROM ai_job WHERE account_key=? AND state='queued' AND purpose='reply' LIMIT 1",(self.account,)).fetchone()
            if not reply and now<self.next_batch:return {'state':'batch_wait'}
            self.next_batch=now+0.8
            return self.service.process_next(account_key=self.account,active_guard=self._active,commit_guard=self._commit)
        finally:self.pump_lock.release()
    def _loop(self):
        try:
            while not self.stop_event.wait(0.1):self.pump()
        finally:
            self.enabled=False;self.service.scheduler_state='stopped'
            if self.policy is not None:self.policy.stop(self)
    def stop(self):
        self.enabled=False;self.stop_event.set();self.service.scheduler_state='stopped'
        if self.policy is not None:self.policy.stop(self)
        elif self.account:self.service.interrupt_running(self.account)
        # Do not block the UI or a basic fact write waiting for a 90-second transport timeout.
        return {'state':'stopping' if self.thread and self.thread.is_alive() else 'stopped','late_result':'discarded','queued_recovery':self.policy is None}
