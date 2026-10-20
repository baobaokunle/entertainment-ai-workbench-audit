"""Caller-driven, disabled-by-default metadata sampler. Does not install or start a loop."""
from dataclasses import dataclass
from datetime import datetime,timezone
import time,uuid

@dataclass(frozen=True)
class CollectorConfig:
    device_key:str
    windows_session_id:int
    user_sid:str
    source_id:str
    source_version:str
    enabled:bool=False
    poll_seconds:int=5
    heartbeat_seconds:int=30
    def __post_init__(self):
        for name in ('device_key','user_sid','source_id','source_version'):
            value=getattr(self,name)
            if not isinstance(value,str) or not value or len(value)>200 or '\x00' in value:raise ValueError('Explicit designated source identity required')
        if type(self.enabled) is not bool or type(self.windows_session_id) is not int or not 0<=self.windows_session_id<=2**32-1 or type(self.poll_seconds) is not int or type(self.heartbeat_seconds) is not int or self.poll_seconds!=5 or self.heartbeat_seconds!=30:raise ValueError('Only documented initial 5-second/30-second candidate cadence is supported')

class SystemClock:
    def read(self):return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z'),time.monotonic_ns()//1_000_000

class ActivityCollector:
    def __init__(self,config,*,api=None,clock=None,binding_probe=None,manual_operation_verified=False):
        self.config=config;self.api=api;self.clock=clock or SystemClock();self.probe=binding_probe
        self.manual=manual_operation_verified is True;self.running=False;self.previous=None;self.last_heartbeat=None;self.signature=None;self.epoch=None;self.suspended=False;self.context=None
        self.event_sequence=0
    def start(self):
        if not self.config.enabled:return {'state':'disabled','events':[]}
        if self.running:return {'state':'running','events':[]}
        if self.api is None:
            from .windows_state import WindowsStateAPI
            self.api=WindowsStateAPI(windows_session_id=self.config.windows_session_id,user_sid=self.config.user_sid)
        try:
            self.api.start_notifications();self.running=True;self.epoch=uuid.uuid4().hex
            return {'state':'running','events':self.tick(force=True)}
        except Exception:
            try:self.api.close()
            finally:self._reset()
            raise
    def _reset(self):
        self.running=False;self.previous=None;self.last_heartbeat=None;self.signature=None;self.epoch=None;self.suspended=False;self.context=None
    def _identity(self,context):return context.get('windows_session_id')==self.config.windows_session_id and context.get('user_sid')==self.config.user_sid
    def _event(self,kind,wall,mono,*,use_binding=True,**changes):
        c=self.context or {};safe=self._identity(c) and c.get('lock_state')=='unlocked' and c.get('session_state')=='active' and not self.suspended
        binding=self.probe(dict(c)) if use_binding and safe and self.probe else None
        allowed={'category','binding_verified','program_sha256','binding_evidence_id','active_tab','url_verified','browser_window_id'}
        if binding is not None and (not isinstance(binding,dict) or set(binding)-allowed):raise ValueError('Finite independently verified binding result required')
        self.event_sequence+=1
        result={'event_id':self.epoch+'_'+format(self.event_sequence,'012d'),'device_key':self.config.device_key,'epoch':self.epoch,'windows_session_id':self.config.windows_session_id,'user_sid':self.config.user_sid,'source_id':self.config.source_id,'source_version':self.config.source_version,'collector_version':'windows-metadata-candidate-v1','wall_utc':wall,'wall_precision':'microsecond','mono_ms':mono,'type':kind,'category':'unknown','operation':None,'foreground_hwnd':c.get('foreground_hwnd') if safe else None,'process_id':c.get('process_id') if safe else None,'program_basename':c.get('program_basename') if safe else None,'binding_verified':False,'visible':c.get('visible') if safe else False,'focused':c.get('focused') if safe else False,'browser_window_id':None,'active_tab':None,'url_verified':None,'automatic':None,'lock_state':c.get('lock_state','unknown'),'session_state':c.get('session_state','unknown'),'power_state':'suspended' if self.suspended else 'awake'}
        if binding:result.update(binding)
        result.update(changes)
        from server.activity import validate_event
        validate_event(result,strict_source=True);return result
    def tick(self,*,force=False):
        if not self.running:return []
        wall,mono=self.clock.read();events=[]
        if self.previous and not force and mono-self.previous[1]<self.config.poll_seconds*1000:return []
        self.context=self.api.read_context()
        for notification in self.api.poll_notifications():
            if notification=='sleep':self.suspended=True;events.append(self._event('sleep',wall,mono))
            elif notification in ('resume','unlock','session_changed'):
                events.append(self._event('gap',wall,mono));self.epoch=uuid.uuid4().hex;self.signature=None;self.suspended=False
            elif notification in ('lock','session_inactive'):events.append(self._event('lock',wall,mono));self.signature=None
        if self.previous:
            old_wall,old_mono=self.previous;delta=mono-old_mono
            drift=abs((datetime.fromisoformat(wall.replace('Z','+00:00'))-datetime.fromisoformat(old_wall.replace('Z','+00:00'))).total_seconds()*1000-delta)
            if delta<0 or drift>2000 or delta>35000:
                events.append(self._event('clock_jump' if delta<0 or drift>2000 else 'gap',wall,mono));self.epoch=uuid.uuid4().hex;self.signature=None
        if not self._identity(self.context):
            events.append(self._event('gap',wall,mono));self.signature=None
        elif self.context.get('lock_state')!='unlocked' or self.context.get('session_state')!='active' or self.suspended:
            signature=('closed',self.context.get('lock_state'),self.context.get('session_state'),self.suspended)
            if signature!=self.signature:events.append(self._event('lock' if self.context.get('lock_state')=='locked' else 'gap',wall,mono))
            self.signature=signature
        else:
            observed=self._event('foreground',wall,mono)
            signature=tuple(observed.get(k) for k in ('foreground_hwnd','process_id','category','binding_verified','visible','focused','active_tab','url_verified','browser_window_id','program_sha256','binding_evidence_id'))
            if signature!=self.signature:events.append(observed);self.signature=signature
        if self.last_heartbeat is None or mono-self.last_heartbeat>=self.config.heartbeat_seconds*1000:
            events.append(self._event('heartbeat',wall,mono));self.last_heartbeat=mono
        self.previous=(wall,mono);return events
    def operation(self,operation,*,automatic=None):
        from server.activity import OPERATIONS
        if not isinstance(operation,str) or operation not in OPERATIONS or automatic is not None and type(automatic) is not bool:raise ValueError('Finite operation type and explicit automation state only')
        if not self.running:return []
        events=self.tick(force=True);wall,mono=self.previous
        if not self._identity(self.context) or self.suspended or self.context.get('lock_state')!='unlocked' or self.context.get('session_state')!='active':return events
        events.append(self._event('operation',wall,mono,operation=operation,automatic=automatic if automatic is not False or self.manual else None));return events
    def stop(self):
        if not self.running:return []
        try:
            wall,mono=self.clock.read()
            return [self._event('stop',wall,mono,use_binding=False,binding_verified=False)]
        finally:
            try:self.api.close()
            finally:self._reset()
