"""Explicit first-only normal enrollment and existing-host pairing assembly.

No credential migration, source activation, database/index initialization or UI.
Prepared authority is Windows-user DPAPI, not platform identity evidence.
"""
import secrets
from pathlib import Path

from .auth import AuthStore, EXTENSION_RE, EXTENSION_SCOPES
from .backup_scratch import reject_links
from .common import AppError, canonical_json
from .local_device_pairing import NONCE_RE, _file_stat, _json, _read_protected, _write_new
from .security import DPAPIProtector
from .workbench_pairing import WorkbenchPairingGrant

KEY_NAME='workbench-device.dpapi'
FIELDS={'format','operator_key','role','device_key','extension_id','account_keys','login_credential','device_credential'}

def _selection(paths,config,operator,accounts,extension_id):
    reject_links(paths.root)
    for filename in (paths.settings,paths.db):_file_stat(filename)
    reject_links(paths.root/'keys')
    if paths.root.drive.upper()!='F:' or not paths.root.is_dir() or not (paths.root/'keys').is_dir() or not paths.db.is_file() or not paths.settings.is_file():
        raise AppError(400,'WORKBENCH_PAIRING_WORKSPACE_REQUIRED','需明确已初始化的独立F盘工作区')
    if config.get('source_mode') not in ('unverified','fictional'):
        raise AppError(403,'WORKBENCH_PAIRING_INACTIVE_REQUIRED','配对准备不接管已激活的真实业务')
    AuthStore._validate_identity(operator,config.get('device_role'),config.get('device_key'),accounts)
    if (operator=='identity-pilot' or config['device_key']=='F-identity-pilot' or
        type(extension_id) is not str or not EXTENSION_RE.fullmatch(extension_id) or
        extension_id not in config.get('extension_ids',[]) or 'snapshot-owner-unverified' in accounts):
        raise AppError(403,'WORKBENCH_PAIRING_SELECTION_INVALID','需明确正常工作台与已登记扩展，不能使用试点身份')
    return {'operator_key':operator,'role':config['device_role'],'device_key':config['device_key'],
            'extension_id':extension_id,'account_keys':sorted(set(accounts))}

def _packet(paths,protector):
    _file_stat(paths.auth)
    value=_json(_read_protected(paths.root/'keys'/KEY_NAME,8192),protector)
    if (type(value) is not dict or set(value)!=FIELDS or value['format']!='workbench-device-secrets-1' or
        type(value['account_keys']) is not list or len(value['account_keys'])>1000 or
        type(value['login_credential']) is not str or not NONCE_RE.fullmatch(value['login_credential']) or
        type(value['device_credential']) is not str or not NONCE_RE.fullmatch(value['device_credential']) or
        secrets.compare_digest(value['login_credential'],value['device_credential'])):
        raise AppError(503,'WORKBENCH_PAIRING_PACKET_INVALID','正常配对登记不完整或未通过核验，保留原件')
    return value

def _grant(paths,config,auth,operator,extension_id,duration,*,until_stopped=False):
    if not isinstance(auth,AuthStore) or auth.path!=paths.auth:
        raise AppError(403,'WORKBENCH_PAIRING_APP_MISMATCH','需复用本工作台原认证实例')
    packet=_packet(paths,auth.protector)
    expected=_selection(paths,config,operator,packet['account_keys'],extension_id)
    if any(packet[key]!=value for key,value in expected.items()):
        raise AppError(403,'WORKBENCH_PAIRING_PACKET_BINDING_INVALID','正常配对绑定不一致，不修改登记')
    grant=WorkbenchPairingGrant(auth,packet['login_credential'],packet['device_credential'],extension_id,
        operator_key=operator,device_key=config['device_key'],ttl_seconds=min(duration,300),
        host_ttl_seconds=None if until_stopped else duration,until_stopped=until_stopped)
    wanted={key:expected[key] for key in ('operator_key','device_key','role','account_keys','extension_id')}
    wanted['scopes']=sorted(EXTENSION_SCOPES)
    if grant._authority!=wanted:
        raise AppError(403,'WORKBENCH_PAIRING_PACKET_AUTH_CHANGED','原权限与准备记录不同，不签发配对')
    return grant

class _FirstOnlyAuth(AuthStore):
    def _save(self,records):
        if self.path.exists():
            raise AppError(409,'WORKBENCH_PAIRING_AUTH_EXISTS','原登记已存在，不能覆盖')
        _write_new(self.path,self.protector.protect(canonical_json({'version':1,'records':records})))
        self._store_stamp=self._stamp()

def prepare_workbench_pairing(paths,config,operator,accounts,extension_id,*,protector=None):
    expected=_selection(paths,config,operator,accounts,extension_id)
    key=paths.root/'keys'/KEY_NAME
    auth_exists=_file_stat(paths.auth,missing_ok=True) is not None
    key_exists=_file_stat(key,missing_ok=True) is not None
    protector=protector if protector is not None else DPAPIProtector()
    if auth_exists!=key_exists:
        raise AppError(409,'WORKBENCH_PAIRING_PARTIAL_OR_EXISTING_AUTH','已有登记或缺半套资料均保留，不重设密码或自动补齐')
    if not auth_exists:
        # The separate key's exclusive creation serializes first enrollment.
        login=secrets.token_urlsafe(32);device=secrets.token_urlsafe(32)
        if secrets.compare_digest(login,device):
            raise AppError(503,'WORKBENCH_PAIRING_RANDOM_COLLISION','随机凭据未分离，未写入')
        packet={'format':'workbench-device-secrets-1',**expected,'login_credential':login,'device_credential':device}
        protected=protector.protect(canonical_json(packet))
        if len(protected)>8192:
            raise AppError(413,'WORKBENCH_PAIRING_PACKET_TOO_LARGE','准备范围超过有限保护包预算，未写入')
        _write_new(key,protected)
        auth=_FirstOnlyAuth(paths.auth,protector,allowed_extension_ids=config['extension_ids'])
        auth.enroll(operator,expected['role'],expected['device_key'],login,account_keys=expected['account_keys'])
    else:auth=AuthStore(paths.auth,protector,allowed_extension_ids=config['extension_ids'])
    packet=_packet(paths,protector)
    if any(packet[field]!=value for field,value in expected.items()):
        raise AppError(403,'WORKBENCH_PAIRING_PACKET_BINDING_INVALID','准备范围与现存登记不同，保留原件')
    _grant(paths,config,auth,operator,extension_id,300)
    return {'prepared':True,'existing_registration':auth_exists,'password_to_remember':False,
            'source_activated':False,'native_page_verified':False}

def attach_workbench_pairing(app,operator,extension_id,duration=1800,*,until_stopped=False):
    if type(duration) is not int or not 1<=duration<=1800:
        raise AppError(400,'WORKBENCH_PAIRING_DURATION_INVALID','本次配对窗口须明确为1至1800秒')
    notes=getattr(app,'local_notes',None)
    if type(until_stopped) is not bool or until_stopped and (
            notes is not None or getattr(app,'read_only',False) is not True or
            getattr(getattr(app,'db',None),'read_only',False) is not True):
        raise AppError(403,'WORKBENCH_PAIRING_LIFETIME_INVALID','持续连接仅用于明确的正常只读工作台。')
    if notes is not None:
        from .local_notes import LocalNotesAccess
        if not isinstance(notes,LocalNotesAccess) or notes.app is not app:
            raise AppError(403,'WORKBENCH_PAIRING_APP_MISMATCH','有限配对需复用当前本地工作台。')
        notes.require_login(operator)
    grant=_grant(app.paths,app.config,app.auth,operator,extension_id,duration,until_stopped=until_stopped)
    app.workbench_pairing=grant
    if notes is not None:
        grant._finite_notes=notes
        grant._access_guard=lambda:notes.check_pairing(grant)
        try:grant._access_guard()
        except BaseException:
            app.workbench_pairing=None
            raise
    return grant

def pairing_handoff(app,grant,extension_id):
    grant.require_app(app,extension_id)
    if grant._access_guard is not None:grant._access_guard()
    directory=app.paths.root/'keys'/'workbench-pairing';reject_links(directory)
    directory.mkdir(exist_ok=True)
    notes=grant._finite_notes
    if notes is not None:notes.check_pairing_area()
    target=directory/(secrets.token_hex(16)+'.dpapi')
    packet={'format':'workbench-pairing-1','extension_id':extension_id,'code':grant.code,'expires_at_utc':grant.expires_at_utc}
    _write_new(target,app.auth.protector.protect(canonical_json(packet)))
    if notes is not None:
        notes.check_pairing_area(target)
        notes.pairing_file=target
        grant._access_guard()
    return target
