"""Minimal designated-device facts and explicitly uncalibrated forward interval estimates."""
from datetime import datetime,timezone
import json
from .common import AppError,canonical_json,utc_now
from .db import digest,stable_key,reject_floats

EVENT_FIELDS={'event_id','device_key','epoch','windows_session_id','user_sid','wall_utc','mono_ms','type','category','operation','foreground_hwnd','browser_window_id','binding_verified','visible','focused','active_tab','url_verified','automatic','source_id','source_version','wall_precision','process_id','program_basename','program_sha256','binding_evidence_id','lock_state','power_state','session_state','collector_version'}
OPERATIONS={'input','scroll','contact_switch','business_control'}
TYPES={'heartbeat','foreground','operation','lock','sleep','stop','gap','clock_jump'}

def install_schema(db):
    with db.transaction() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS activity_event(event_id TEXT PRIMARY KEY,device_key TEXT NOT NULL,operator_key TEXT NOT NULL,epoch TEXT NOT NULL,wall_utc TEXT NOT NULL,mono_ms INTEGER NOT NULL,event_type TEXT NOT NULL,semantic_digest TEXT NOT NULL,payload_json TEXT NOT NULL)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_device_time ON activity_event(device_key,wall_utc,event_id)')
        conn.execute("INSERT INTO app_meta VALUES('activity_schema','1') ON CONFLICT(key) DO NOTHING")
        for operation in ('INSERT','DELETE'):
            conn.execute('CREATE TRIGGER IF NOT EXISTS activity_version_raw_'+operation+' AFTER '+operation+" ON activity_event BEGIN UPDATE app_meta SET value=CAST(value AS INTEGER)+1 WHERE key='data_version'; END")

def seconds(value):
    try:
        if not isinstance(value,str) or len(value)>80:raise ValueError()
        date=datetime.fromisoformat(value.replace('Z','+00:00'))
        if date.tzinfo is None or date.utcoffset().total_seconds()!=0: raise ValueError()
        return date.timestamp()
    except (TypeError,ValueError,AttributeError,OverflowError):
        raise AppError(400,'invalid_activity_time','活动时间必须为明确UTC时间') from None

def indexed_utc(value):
    seconds(value)
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')

def validate_event(event,*,strict_source=False):
    required={'event_id','device_key','epoch','windows_session_id','user_sid','wall_utc','mono_ms','type'}
    if not isinstance(event,dict) or not required<=set(event) or set(event)-EVENT_FIELDS or not isinstance(event.get('type'),str) or event['type'] not in TYPES:
        raise AppError(400,'activity_payload_rejected','活动只能包含有限状态及操作类型')
    reject_floats(event);seconds(event['wall_utc'])
    for name in ('event_id','device_key','epoch','user_sid'):
        if not isinstance(event[name],str) or not event[name].strip() or len(event[name])>200 or '\x00' in event[name]:raise AppError(400,'activity_payload_rejected','活动标识无效')
    if type(event['mono_ms']) is not int or not 0<=event['mono_ms']<=2**63-1 or type(event['windows_session_id']) is not int or not 0<=event['windows_session_id']<=2**32-1:
        raise AppError(400,'activity_payload_rejected','活动标识或单调时间无效')
    if not isinstance(event.get('category','unknown'),str) or event.get('category','unknown') not in ('chat','editing','other','unknown') or event.get('operation') is not None and (not isinstance(event['operation'],str) or event['operation'] not in OPERATIONS):
        raise AppError(400,'activity_payload_rejected','程序类型或操作类型无效')
    for name in ('binding_verified','visible','focused','active_tab','url_verified','automatic'):
        if event.get(name) is not None and type(event[name]) is not bool:raise AppError(400,'activity_payload_rejected','活动状态必须为明确布尔值或未知')
    for name in ('foreground_hwnd','browser_window_id','process_id'):
        if event.get(name) is not None and (type(event[name]) is not int or not 0<=event[name]<=2**64-1):raise AppError(400,'activity_payload_rejected','窗口或进程标识无效')
    for name,choices in [('lock_state',{'locked','unlocked','unknown'}),('power_state',{'awake','suspended','unknown'}),('session_state',{'active','inactive','unknown'}),('wall_precision',{'second','millisecond','microsecond','unknown'})]:
        if name in event and (not isinstance(event[name],str) or event[name] not in choices):raise AppError(400,'activity_payload_rejected','活动状态无效')
    for name in ('source_id','source_version','program_basename','binding_evidence_id','collector_version'):
        if event.get(name) is not None and (not isinstance(event[name],str) or not event[name] or len(event[name])>200 or '\x00' in event[name]):raise AppError(400,'activity_payload_rejected','活动来源字段无效')
    if event.get('program_basename') and any(c in event['program_basename'] for c in ('/','\\',':')):raise AppError(400,'activity_payload_rejected','只允许程序基本名称')
    if event.get('program_sha256') is not None:
        import re
        if not isinstance(event['program_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',event['program_sha256']):raise AppError(400,'activity_payload_rejected','程序摘要无效')
    if strict_source and (not event.get('source_id') or not event.get('source_version') or event.get('wall_precision') not in ('millisecond','microsecond')):
        raise AppError(400,'activity_source_precision_required','跨机活动需要明确来源版本及直接时钟精度')
    return event

def union(intervals):
    merged=[]
    for lower,upper in sorted(intervals):
        if upper<=lower:continue
        if merged and lower<=merged[-1][1]: merged[-1]=(merged[-1][0],max(upper,merged[-1][1]))
        else: merged.append((lower,upper))
    return merged

def qualifies(event):
    if event.get('category')=='editing': return event.get('binding_verified') is True and bool(event.get('foreground_hwnd')) and event.get('visible') is True and event.get('focused') is True
    return event.get('category')=='chat' and all(event.get(x) is True for x in ('binding_verified','visible','focused','active_tab','url_verified')) and bool(event.get('foreground_hwnd')) and bool(event.get('browser_window_id'))

def estimate(events,*,tau=60,heartbeat_seconds=35,calibrated=False):
    if tau not in (30,60,90):raise AppError(400,'invalid_activity_budget','仅允许30/60/90秒校准候选')
    rows=sorted(events,key=lambda x:(x['epoch'],x['mono_ms'],x['event_id']))
    foreground=[];active=[];uncertain=[];gaps=[];segment=None;operations=[];previous=None
    def finish(deadline):
        nonlocal segment,operations
        if segment is None:return
        lower,wall_offset,epoch,last_seen=segment
        upper=min(deadline,last_seen+heartbeat_seconds*1000)
        foreground.append(((lower+wall_offset)/1000,(upper+wall_offset)/1000))
        for operation,certainty in operations:
            target=active if certainty=='manual' else uncertain
            target.append(((operation+wall_offset)/1000,(min(operation+tau*1000,upper)+wall_offset)/1000))
        segment=None;operations=[]
    for event in rows:
        moment=event['mono_ms']; wall=seconds(event['wall_utc'])*1000; epoch=event['epoch']
        if previous and (epoch!=previous['epoch'] or moment-previous['mono_ms']>heartbeat_seconds*1000 or abs((wall-seconds(previous['wall_utc'])*1000)-(moment-previous['mono_ms']))>2000):
            finish(previous['mono_ms'])
            gaps.append({'after':previous['wall_utc'],'before':event['wall_utc'],'reason':'coverage_or_clock_gap'})
        if event['type'] in ('lock','sleep','stop','gap','clock_jump'):
            finish(moment)
        elif event['type']=='foreground':
            finish(moment)
            if qualifies(event): segment=(moment,wall-moment,epoch,moment)
        elif segment:
            if not qualifies(event):
                finish(moment);previous=event;continue
            lower,offset,sepoch,_=segment
            segment=(lower,offset,sepoch,moment)
            if event['type']=='operation' and event.get('operation') in OPERATIONS and qualifies(event):
                if event.get('automatic') is False:operations.append((moment,'manual'))
                elif event.get('automatic') is not True:operations.append((moment,'uncertain'))
        previous=event
    # Never extend beyond the final observed event when the collector stops.
    if previous: finish(previous['mono_ms'])
    foreground=union(foreground);active=union(active);uncertain=union(uncertain)
    for x,y in uncertain:
        clipped=[]
        for a,b in active:
            if b<=x or a>=y:clipped.append((a,b))
            else:
                if a<x:clipped.append((a,x))
                if b>y:clipped.append((y,b))
        active=clipped
    total=lambda ranges:round(sum(b-a for a,b in ranges),3)
    return {'foreground_intervals':foreground,'estimated_active_intervals':active,'foreground_seconds':total(foreground),
            'estimated_active_seconds':total(active),'gaps':gaps,'tau':tau,'algorithm_version':'forward-union-v1',
            'restricted_intervals':uncertain,'restricted_seconds':total(uncertain),
            'estimate_state':'calibrated' if calibrated else 'uncalibrated','labor_score':None,'reading_interpretation':'foreground_fact_only'}

class ActivityService:
    def __init__(self,db,*,device_key,windows_session_id,user_sid):
        self.db=db;self.device=device_key;self.session=windows_session_id;self.sid=user_sid
        with db.lock:
            version=db.conn.execute("SELECT value FROM app_meta WHERE key='activity_schema'").fetchone()
            if not version or version[0]!='1' or not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='activity_event'").fetchone():raise AppError(409,'ACTIVITY_NOT_INITIALIZED','活动模式需要显式初始化或迁移')
    def ingest(self,event,principal,*,emit_event=True):
        if principal is None:raise AppError(401,'ACTIVITY_AUTH_REQUIRED','活动需要认证来源会话')
        if type(emit_event) is not bool:raise AppError(422,'ACTIVITY_EMISSION_INVALID','事件发布必须由服务明确指定')
        principal.require('activities:write')
        principal.require('activity_provenance:trusted')
        if not isinstance(event,dict):raise AppError(400,'activity_payload_rejected','活动请求无效')
        if principal.role!='baobao' or principal.device_key!=self.device or event.get('device_key')!=self.device or event.get('windows_session_id')!=self.session or event.get('user_sid')!=self.sid:
            raise AppError(403,'activity_device_rejected','活动仅限已登记的饱饱Windows设备及会话')
        validate_event(event)
        semantic=digest(event)
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_expiry'").fetchone():
                cutoff=conn.execute('SELECT cutoff_utc FROM activity_expiry WHERE device_key=? AND operator_key=?',(self.device,principal.operator_key)).fetchone()
                if cutoff and indexed_utc(event['wall_utc'])<cutoff[0]:return {'status':'expired','committed':True,'physical_erasure':'copies_pending'}
            prior=conn.execute('SELECT semantic_digest,operator_key FROM activity_event WHERE event_id=?',(event['event_id'],)).fetchone()
            if prior:
                if prior[0]!=semantic or prior[1]!=principal.operator_key:raise AppError(409,'activity_id_reused','同一活动标识的内容或操作者不同')
                return {'status':'duplicate','committed':True}
            conn.execute('INSERT INTO activity_event VALUES(?,?,?,?,?,?,?,?,?)',(event['event_id'],self.device,principal.operator_key,event['epoch'],indexed_utc(event['wall_utc']),event['mono_ms'],event['type'],semantic,canonical_json(event).decode()))
            if emit_event:self.db.append_event(conn,account_key='device:'+self.device,target_key=event['event_id'],event_type='activity.observed',payload={'event':event,'operator_key':principal.operator_key},channel='activities',actor={'operator_key':principal.operator_key,'device_key':principal.device_key,'session_id':principal.session_id})
        return {'status':'accepted','committed':True}
    def timeline(self,principal,*,start_utc,end_utc,tau=60):
        if principal.role=='manager':principal.require('activities:read')
        else:
            principal.require('activities:self')
            if principal.device_key!=self.device:raise AppError(403,'forbidden','仅能查看本人指定设备')
        if seconds(start_utc)>=seconds(end_utc):raise AppError(400,'invalid_activity_window','时间窗口无效')
        start_utc=indexed_utc(start_utc);end_utc=indexed_utc(end_utc)
        # The requested slice needs its preceding foreground/stop boundary and
        # the first following observation. Otherwise a foreground segment begun
        # before the slice disappears, or an unobserved tail is invented.
        operator_clause='' if principal.role=='manager' else ' AND operator_key=?'
        scope=[self.device] if principal.role=='manager' else [self.device,principal.operator_key]
        with self.db.lock:
            prior=self.db.conn.execute("SELECT wall_utc FROM activity_event WHERE device_key=?"+operator_clause+" AND wall_utc<? AND event_type IN ('foreground','lock','sleep','stop','gap','clock_jump') ORDER BY wall_utc DESC,event_id DESC LIMIT 1",(*scope,start_utc)).fetchone()
            anchor=prior[0] if prior else start_utc
            rows=self.db.conn.execute('SELECT payload_json,wall_utc FROM activity_event WHERE device_key=?'+operator_clause+' AND wall_utc>=? AND wall_utc<? ORDER BY wall_utc,event_id LIMIT 100001',(*scope,anchor,end_utc)).fetchall()
            if len(rows)>100000:raise AppError(413,'activity_window_limit','活动上下文超过查询上限，请缩小窗口或核验采集边界')
            after=self.db.conn.execute('SELECT payload_json,wall_utc FROM activity_event WHERE device_key=?'+operator_clause+' AND wall_utc>=? ORDER BY wall_utc,event_id LIMIT 1',(*scope,end_utc)).fetchone()
        events=[json.loads(row[0]) for row in rows if row[1]>=start_utc]
        context=[json.loads(row[0]) for row in rows]
        if after:context.append(json.loads(after[0]))
        result=estimate(context,tau=tau)
        lower,upper=seconds(start_utc),seconds(end_utc)
        for field,total in [('foreground_intervals','foreground_seconds'),('estimated_active_intervals','estimated_active_seconds'),('restricted_intervals','restricted_seconds')]:
            result[field]=union([(max(a,lower),min(b,upper)) for a,b in result[field] if max(a,lower)<min(b,upper)])
            result[total]=round(sum(b-a for a,b in result[field]),3)
        result['gaps']=[gap for gap in result['gaps'] if seconds(gap['after'])<upper and seconds(gap['before'])>lower]
        return {'events':events,'estimate':result,'coverage':'observed_only','retention':'not_calibrated','automatic_labor_completion':False}
