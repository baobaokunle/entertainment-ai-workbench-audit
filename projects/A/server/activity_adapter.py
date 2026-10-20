"""Explicit local activity-source approval and signed receive adapter; no enrollment on read."""
import json,re
from .common import AppError,canonical_json
from .auth import Principal
from .activity import ActivityService,validate_event
from .sync.events import validate_event as validate_sync_event

BASE_CAPABILITIES=frozenset({'session_sid','foreground','monotonic_clock','wall_clock','lock_state','power_state','heartbeat'})
CAPABILITIES=BASE_CAPABILITIES|{'editing_binding','browser_binding','active_tab','chat_url','manual_operation'}
APPROVAL_FIELDS={'peer_device','device_key','windows_session_id','user_sid','operator_key','source_id','source_version','source_kind','capabilities','evidence_id','editing_bindings','browser_evidence_ids'}

def install_schema(db):
    with db.transaction() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS activity_source_approval(peer_device TEXT NOT NULL,source_id TEXT NOT NULL,source_version TEXT NOT NULL,approval_json TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN(0,1)),PRIMARY KEY(peer_device,source_id,source_version))')
        conn.execute("INSERT OR IGNORE INTO app_meta VALUES('activity_adapter_schema','1')")

class ActivityApprovals:
    def __init__(self,db):
        self.db=db
        with db.lock:
            if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='activity_source_approval'").fetchone():raise AppError(409,'ACTIVITY_ADAPTER_NOT_INITIALIZED','显式初始化活动来源审批模式后才可使用')
    def approve(self,approval,principal):
        principal.require('activity_sources:approve')
        if principal.role!='manager':raise AppError(403,'forbidden','独立本机来源审批仅限管理')
        if not isinstance(approval,dict) or set(approval)!=APPROVAL_FIELDS:raise AppError(422,'ACTIVITY_APPROVAL_INVALID','活动审批必须为有限完整本机配置')
        for name in ('peer_device','device_key','user_sid','operator_key','source_id','source_version','evidence_id'):
            if not isinstance(approval[name],str) or not approval[name].strip() or len(approval[name])>200 or '\x00' in approval[name]:raise AppError(422,'ACTIVITY_APPROVAL_INVALID','审批标识无效')
        if approval['peer_device']!=approval['device_key'] or type(approval['windows_session_id']) is not int or not 0<=approval['windows_session_id']<=2**32-1 or approval['source_kind'] not in ('fictional','windows_candidate'):
            raise AppError(422,'ACTIVITY_APPROVAL_INVALID','指定来源与Windows会话无效')
        caps=approval['capabilities']
        if not isinstance(caps,list) or len(caps)>len(CAPABILITIES) or any(not isinstance(c,str) or c not in CAPABILITIES for c in caps) or len(set(caps))!=len(caps):raise AppError(422,'ACTIVITY_APPROVAL_INVALID','每项来源能力必须独立明确')
        bindings=approval['editing_bindings'];browser=approval['browser_evidence_ids']
        if not isinstance(bindings,list) or len(bindings)>16 or not isinstance(browser,list) or len(browser)>16:raise AppError(422,'ACTIVITY_APPROVAL_INVALID','入口证据必须有界')
        for binding in bindings:
            if not isinstance(binding,dict) or set(binding)!={'program_basename','program_sha256','evidence_id'} or not isinstance(binding['program_basename'],str) or not binding['program_basename'] or len(binding['program_basename'])>200 or any(c in binding['program_basename'] for c in ('/','\\',':','\x00')) or not isinstance(binding['program_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',binding['program_sha256']) or not isinstance(binding['evidence_id'],str) or not binding['evidence_id'] or len(binding['evidence_id'])>200:
                raise AppError(422,'ACTIVITY_APPROVAL_INVALID','剪辑入口需要已核验程序摘要及窗口绑定证据')
        if any(not isinstance(x,str) or not x or len(x)>200 or '\x00' in x for x in browser):raise AppError(422,'ACTIVITY_APPROVAL_INVALID','浏览器窗口/页签/URL证据无效')
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO activity_source_approval VALUES(?,?,?,?,1) ON CONFLICT(peer_device,source_id,source_version) DO UPDATE SET approval_json=excluded.approval_json,active=1',(approval['peer_device'],approval['source_id'],approval['source_version'],canonical_json(approval).decode()))
        return {'approved':True,'transport_pairing_granted':False,'collector_started':False}
    def revoke(self,peer,source_id,source_version,principal):
        principal.require('activity_sources:approve')
        if principal.role!='manager':raise AppError(403,'forbidden','仅管理可撤销独立来源审批')
        with self.db.transaction() as conn:conn.execute('UPDATE activity_source_approval SET active=0 WHERE peer_device=? AND source_id=? AND source_version=?',(peer,source_id,source_version))
    def get(self,peer,source_id,source_version):
        with self.db.lock:row=self.db.conn.execute('SELECT approval_json FROM activity_source_approval WHERE peer_device=? AND source_id=? AND source_version=? AND active=1',(peer,source_id,source_version)).fetchone()
        return json.loads(row[0]) if row else None

class ActivityReceiveAdapter:
    def __init__(self,db):
        self.db=db;self.approvals=ActivityApprovals(db)
        with db.lock:
            if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='activity_event'").fetchone():raise AppError(409,'ACTIVITY_NOT_INITIALIZED','接收活动前须显式初始化活动模式')
    def _assess(self,event):
        validate_sync_event(event)
        if event['event_type']!='activity.observed' or event['channel']!='activities' or set(event['payload'])!={'event','operator_key'}:raise AppError(422,'ACTIVITY_EVENT_INVALID','仅接受完整活动原始来源事件')
        raw=event['payload']['event'];validate_event(raw,strict_source=True)
        actor=event['actor']
        if not actor or event['account_key']!='device:'+event['device_key'] or event['target_key']!=raw['event_id'] or raw['device_key']!=event['device_key'] or actor['device_key']!=event['device_key'] or event['payload']['operator_key']!=actor['operator_key']:
            raise AppError(403,'ACTIVITY_ORIGIN_INVALID','活动原始设备、对象及操作者必须与签名来源相符')
        approval=self.approvals.get(event['device_key'],raw['source_id'],raw['source_version'])
        if not approval or not BASE_CAPABILITIES<=set(approval['capabilities']):raise AppError(409,'DEPENDENCY_PENDING','活动来源各项能力尚未独立审批')
        if approval['source_kind']!='fictional':raise AppError(409,'DEPENDENCY_PENDING','真实Windows来源尚未通过本轮验收；本机配置不能自报激活')
        if any(raw[k]!=approval[k] for k in ('device_key','windows_session_id','user_sid')) or actor['operator_key']!=approval['operator_key']:raise AppError(403,'ACTIVITY_ORIGIN_INVALID','活动与本机指定会话/SID/操作者审批不同')
        return raw,approval
    def authority(self,event):
        try:self._assess(event);return True
        except AppError as exc:
            if exc.code=='DEPENDENCY_PENDING':raise
            return False
    def register_handlers(self,importer):
        def apply(conn,event):
            raw,approval=self._assess(event)
            # Bindings are separately approved proof, never a program-name guess.
            caps=set(approval['capabilities']);value=dict(raw)
            if value.get('category')=='editing':
                key={k:value.get(k) for k in ('program_basename','program_sha256')};key['evidence_id']=value.get('binding_evidence_id')
                if 'editing_binding' not in caps or key not in approval['editing_bindings']:value.update(category='unknown',binding_verified=False)
            elif value.get('category')=='chat' and (not {'browser_binding','active_tab','chat_url'}<=caps or value.get('binding_evidence_id') not in approval['browser_evidence_ids']):value.update(category='unknown',binding_verified=False,active_tab=None,url_verified=None)
            if value.get('automatic') is False and 'manual_operation' not in caps:value['automatic']=None
            if value.get('lock_state')!='unlocked' or value.get('session_state')!='active' or value.get('power_state')!='awake':value.update(binding_verified=False,focused=False)
            principal=Principal(approval['operator_key'],'baobao',approval['device_key'],event['actor']['session_id'],frozenset({'activities:write','activity_provenance:trusted'}),frozenset())
            service=ActivityService(self.db,device_key=approval['device_key'],windows_session_id=approval['windows_session_id'],user_sid=approval['user_sid'])
            service.ingest(value,principal,emit_event=False)
        importer.register_handler('activity.observed',apply)
