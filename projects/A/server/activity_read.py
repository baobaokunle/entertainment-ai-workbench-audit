"""Read-only activity views. No enrollment, Windows access, collectors or DDL."""
from contextlib import contextmanager
import json
from .auth import Principal
from .activity import estimate, union, indexed_utc, seconds
from .common import AppError, canonical_json
from .db import digest

MAX_WINDOW_SECONDS = 7 * 86400
MAX_RESPONSE_BYTES = 768 * 1024
PUBLIC_EVENT_FIELDS = frozenset({'event_id','device_key','epoch','wall_utc','mono_ms','type','category',
    'operation','binding_verified','visible','focused','active_tab','url_verified','automatic',
    'source_id','source_version','wall_precision','lock_state','power_state','session_state','collector_version'})

class ActivityReadService:
    def __init__(self, db):
        self.db = db

    @contextmanager
    def _snapshot(self):
        # A process lock alone cannot isolate another process's SQLite writes.
        # BEGIN here is a read transaction and works on mode=ro connections.
        with self.db.lock:
            owned = not self.db.conn.in_transaction
            if owned: self.db.conn.execute('BEGIN')
            try: yield
            finally:
                if owned: self.db.conn.execute('ROLLBACK')

    def _schema(self):
        row = self.db.conn.execute("SELECT value FROM app_meta WHERE key='activity_schema'").fetchone()
        if not row or row[0] != '1' or not self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='activity_event'").fetchone():
            raise AppError(503,'ACTIVITY_NOT_INITIALIZED','活动模式尚未显式初始化；未启动采集')

    @staticmethod
    def _authorize(principal):
        if not isinstance(principal, Principal) or principal.role not in ('manager','baobao'):
            raise AppError(403,'ACTIVITY_READ_FORBIDDEN','活动只读视图需要已登记管理或饱饱身份')
        principal.require('activities:read' if principal.role == 'manager' else 'activities:self')

    def _devices(self, principal):
        devices = {}
        if self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='activity_source_approval'").fetchone():
            for row in self.db.conn.execute('SELECT approval_json,active FROM activity_source_approval ORDER BY peer_device,source_id,source_version'):
                approval = json.loads(row['approval_json'])
                device, operator = approval['device_key'], approval['operator_key']
                if principal.role == 'baobao' and (device != principal.device_key or operator != principal.operator_key): continue
                entry = devices.setdefault(device,{'device_key':device,'operator_keys':[],'sources':[],
                    'scope':'device_all_operators' if principal.role=='manager' else 'own_operator',
                    'source_activation':'unverified','collector_started':False})
                if operator not in entry['operator_keys']: entry['operator_keys'].append(operator)
                entry['sources'].append({'source_id':approval['source_id'],'source_version':approval['source_version'],
                    'source_kind':approval['source_kind'],'registration_state':'active' if row['active'] else 'revoked'})
        if principal.role == 'baobao' and principal.device_key not in devices:
            # The authenticated identity registers one's own device, even when
            # no collector/source has been approved on this installation.
            devices[principal.device_key]={'device_key':principal.device_key,'operator_keys':[principal.operator_key],
                'sources':[],'scope':'own_operator','source_activation':'no_approved_source','collector_started':False}
        return sorted(devices.values(),key=lambda item:item['device_key'])

    def devices(self, principal):
        self._authorize(principal)
        with self._snapshot():
            self._schema()
            return {'items':self._devices(principal),'coverage':'registered_devices_only',
                'source_activation':'unverified','collector_started':False,'snapshot_data_version':self.db.health()['data_version']}

    def _timeline(self, principal, device, start, end, tau):
        # A device can contain distinct people/Windows sessions with the same
        # monotonic counters and epoch strings. Their facts cannot supply one
        # another's foreground boundary, heartbeat or manual operation.
        operator_clause='' if principal.role=='manager' else ' AND operator_key=?'
        scope=[device] if principal.role=='manager' else [device,principal.operator_key]
        columns="operator_key,epoch,json_extract(payload_json,'$.windows_session_id') AS session,json_extract(payload_json,'$.user_sid') AS sid,json_extract(payload_json,'$.source_id') AS source,json_extract(payload_json,'$.source_version') AS version"
        streams=self.db.conn.execute('SELECT DISTINCT '+columns+' FROM activity_event WHERE device_key=?'+operator_clause+' LIMIT 1001',scope).fetchall()
        if len(streams)>1000:raise AppError(413,'ACTIVITY_STREAM_LIMIT','活动来源流超过上限，请缩小已登记来源范围')
        lower,upper=seconds(start),seconds(end)
        events=[];estimates=[];used=0;epoch_boundaries={}
        for stream in streams:
            clause="device_key=? AND operator_key=? AND epoch=? AND json_extract(payload_json,'$.windows_session_id') IS ? AND json_extract(payload_json,'$.user_sid') IS ? AND json_extract(payload_json,'$.source_id') IS ? AND json_extract(payload_json,'$.source_version') IS ?"
            values=[device,stream['operator_key'],stream['epoch'],stream['session'],stream['sid'],stream['source'],stream['version']]
            prior=self.db.conn.execute("SELECT wall_utc FROM activity_event WHERE "+clause+" AND wall_utc<? AND event_type IN ('foreground','lock','sleep','stop','gap','clock_jump') ORDER BY wall_utc DESC,event_id DESC LIMIT 1",(*values,start)).fetchone()
            anchor=prior[0] if prior else start
            rows=self.db.conn.execute('SELECT payload_json,wall_utc FROM activity_event WHERE '+clause+' AND wall_utc>=? AND wall_utc<? ORDER BY wall_utc,event_id LIMIT ?',(*values,anchor,end,100001-used)).fetchall()
            used+=len(rows)
            if used>100000:raise AppError(413,'activity_window_limit','活动上下文超过查询上限，请缩短窗口或核验边界')
            after=self.db.conn.execute('SELECT payload_json,wall_utc FROM activity_event WHERE '+clause+' AND wall_utc>=? ORDER BY wall_utc,event_id LIMIT 1',(*values,end)).fetchone()
            context=[json.loads(row['payload_json']) for row in rows]
            if after:context.append(json.loads(after['payload_json']))
            if context:
                ordered=sorted(context,key=lambda event:(seconds(event['wall_utc']),event['event_id']))
                physical=(stream['operator_key'],stream['session'],stream['sid'],stream['source'],stream['version'])
                epoch_boundaries.setdefault(physical,[]).append((ordered[0]['wall_utc'],ordered[-1]['wall_utc'],stream['epoch']))
            value=estimate(context,tau=tau)
            for field,total in [('foreground_intervals','foreground_seconds'),('estimated_active_intervals','estimated_active_seconds'),('restricted_intervals','restricted_seconds')]:
                value[field]=union([(max(a,lower),min(b,upper)) for a,b in value[field] if max(a,lower)<min(b,upper)])
                value[total]=round(sum(b-a for a,b in value[field]),3)
            value['gaps']=[gap for gap in value['gaps'] if seconds(gap['after'])<upper and seconds(gap['before'])>lower]
            estimates.append(value)
            stream_key=digest(values)
            for row in rows:
                if row['wall_utc']>=start:
                    event=json.loads(row['payload_json'])
                    public={key:child for key,child in event.items() if key in PUBLIC_EVENT_FIELDS}
                    public.update(operator_key=stream['operator_key'],stream_key=stream_key)
                    events.append(public)
        combined=estimate([],tau=tau)
        for field,total in [('foreground_intervals','foreground_seconds'),('estimated_active_intervals','estimated_active_seconds'),('restricted_intervals','restricted_seconds')]:
            combined[field]=union([pair for value in estimates for pair in value[field]])
            combined[total]=round(sum(b-a for a,b in combined[field]),3)
        gaps={canonical_json(gap):gap for value in estimates for gap in value['gaps']}
        for ranges in epoch_boundaries.values():
            ranges.sort(key=lambda item:seconds(item[0]))
            for previous,current in zip(ranges,ranges[1:]):
                if previous[2]!=current[2] and seconds(previous[1])<=seconds(current[0]) and seconds(previous[1])<upper and seconds(current[0])>lower:
                    gap={'after':previous[1],'before':current[0],'reason':'epoch_boundary'}
                    gaps[canonical_json(gap)]=gap
        combined['gaps']=sorted(gaps.values(),key=lambda item:(item['after'],item['before']))
        combined['aggregation']='independent_scope_union-v1'
        combined['stream_count']=len(streams)
        events.sort(key=lambda item:(indexed_utc(item['wall_utc']),item['event_id']))
        return {'events':events,'estimate':combined,'coverage':'observed_only','retention':'not_calibrated','automatic_labor_completion':False}

    def timeline(self, principal, *, device_key, start_utc, end_utc, tau=60):
        self._authorize(principal)
        if not isinstance(device_key,str) or not device_key or len(device_key)>200 or '\x00' in device_key:
            raise AppError(400,'INVALID_ACTIVITY_DEVICE','请选择明确已登记设备')
        if type(tau) is not int or tau not in (30,60,90):
            raise AppError(400,'INVALID_ACTIVITY_TAU','仅允许30/60/90秒候选')
        lower, upper = seconds(start_utc), seconds(end_utc)
        if not 0 < upper-lower <= MAX_WINDOW_SECONDS:
            raise AppError(400,'INVALID_ACTIVITY_WINDOW','查询需明确UTC起止，结束不包含，最长七日')
        if principal.role == 'baobao' and device_key != principal.device_key:
            raise AppError(403,'ACTIVITY_DEVICE_FORBIDDEN','饱饱仅能查询自己指定设备的本人活动')
        with self._snapshot():
            self._schema()
            if device_key not in {item['device_key'] for item in self._devices(principal)}:
                raise AppError(403,'ACTIVITY_DEVICE_UNREGISTERED','此设备未在活动来源审批中登记')
            result=self._timeline(principal,device_key,indexed_utc(start_utc),indexed_utc(end_utc),tau)
            result.update(device_key=device_key,start_utc=indexed_utc(start_utc),end_utc=indexed_utc(end_utc),
                scope='device_all_operators' if principal.role=='manager' else 'own_operator',
                operator_key=principal.operator_key if principal.role=='baobao' else None,
                snapshot_data_version=self.db.health()['data_version'],source_activation='unverified')
            if len(canonical_json(result))>MAX_RESPONSE_BYTES:
                raise AppError(413,'ACTIVITY_RESPONSE_LIMIT','完整活动响应超过预算；未截断，请缩短查询窗口')
            return result
