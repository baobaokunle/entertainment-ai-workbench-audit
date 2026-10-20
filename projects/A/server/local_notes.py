"""Explicit protected local notes only; no identity, import or AI activation."""
import copy
import json
import re

from .auth import ROLE_SCOPES
from .common import AppError
from .db import stable_key
from .import_binding import ImportBindingService
from .local_pending_import import check_import_storage, check_import_workspace, _login_area_paths
from .local_workspace_preparation import local_f_path


def check_notes_workspace(paths, powershell_exe):
    config=check_import_workspace(paths,powershell_exe,prepared_login=True)
    selection=config['local_access']
    if selection is None or local_f_path(selection['workspace_root'])!=paths.root:
        raise AppError(403,'LOCAL_NOTES_SELECTION_REQUIRED','尚未明确配置此工作区的有限本地备注。')
    return config


class LocalNotesAccess:
    def __init__(self,app,operator,powershell_exe):
        from .workbench_pairing_entry import _packet,_selection
        self.app,self.auth,self.db=app,app.auth,app.db
        self.shell,self.operator=powershell_exe,operator
        self.blocked=False
        self.login_file=None
        self.pairing_file=None
        config=check_notes_workspace(app.paths,powershell_exe)
        if config!=app.config:
            raise AppError(409,'LOCAL_NOTES_CONFIGURATION_CHANGED','本地备注配置已变化。')
        check_import_storage(app,powershell_exe,prepared_login=True)
        self.selection=copy.deepcopy(config['local_access'])
        self.binding=self.selection['binding']
        self.account=self.binding['account_key']
        packet=_packet(app.paths,app.auth.protector)
        expected=_selection(app.paths,config,operator,[self.account],packet['extension_id'])
        if any(packet[key]!=value for key,value in expected.items()):
            raise AppError(403,'LOCAL_NOTES_AUTH_CHANGED','原登记必须只包含本次明确的账号和设备。')
        self.wanted={key:expected[key] for key in ('operator_key','device_key','role','account_keys')}
        self.wanted['scopes']=sorted(ROLE_SCOPES['manager'])
        session=self.auth.login(operator,packet['login_credential'],device_key=config['device_key'],client_kind='workbench')
        try:
            principal=self.auth.authenticate(session['token'],scope='profile:write',account_key=self.account)
            self._authority(principal)
            self.registry=self._target(principal)['registration_fingerprint']
            self.check(principal)
        finally:
            self.auth.logout(session['token'])

    def _authority(self,principal):
        actual={key:value for key,value in principal.as_dict().items() if key!='session_id'}
        if actual!=self.wanted:
            raise AppError(403,'LOCAL_NOTES_AUTH_CHANGED','有限备注与原操作者、设备或唯一账号权限不一致。')

    def _target(self,principal):
        target=ImportBindingService(self.app.file_import).check_target(self.binding,principal)
        owner=target['account']
        if (owner['platform']!='douyin' or not re.fullmatch('[0-9]{1,128}',owner['platform_uid']) or
                stable_key('account','douyin',owner['platform_uid'])!=self.account):
            raise AppError(409,'LOCAL_NOTES_OWNER_UID_REQUIRED','缺少实际本人UID，不能用占位身份打开备注。')
        if json.loads(target['source']['evidence_json']).get('local_import')!=self.selection['selection']:
            raise AppError(409,'LOCAL_NOTES_SOURCE_CHANGED','有限备注与原批准的来源登记不一致。')
        return target

    def check(self,principal):
        if self.blocked:
            raise AppError(503,'LOCAL_NOTES_STOPPED','有限备注已停止，保留输入，修复后重新启动。')
        # A denied other session must not disable the correctly registered user.
        self._authority(principal)
        try:
            if (self.app.auth is not self.auth or self.app.db is not self.db or
                    self.app.config['local_access']!=self.selection):
                raise AppError(409,'LOCAL_NOTES_CONFIGURATION_CHANGED','有限备注运行绑定已变化。')
            check_import_storage(self.app,self.shell,prepared_login=True,
                include_login_area=True,login_file=self.login_file)
            if self._target(principal)['registration_fingerprint']!=self.registry:
                raise AppError(409,'LOCAL_NOTES_SOURCE_CHANGED','账号或来源登记已变化。')
        except Exception:
            self.blocked=True
            raise

    def require_login(self,operator):
        if operator!=self.operator or self.blocked:
            raise AppError(403,'LOCAL_NOTES_AUTH_CHANGED','有限备注登录与原操作者不一致或已停止。')
        check_import_storage(self.app,self.shell,prepared_login=True)

    def check_login_area(self,target=None):
        from .local_workspace_preparation import _acl,_shell_path
        items=_login_area_paths(self.app.paths,target)
        if items:_acl(_shell_path(self.shell),items,timeout_seconds=5)

    def check_pairing_area(self,target=None):
        from .local_workspace_preparation import _acl,_shell_path
        items=_login_area_paths(self.app.paths,target,area='workbench-pairing')
        if items:_acl(_shell_path(self.shell),items,timeout_seconds=5)

    def check_pairing(self,grant):
        """Verify the host registration; do not turn an extension into a manager."""
        from .auth import EXTENSION_SCOPES
        from .workbench_pairing_entry import _packet,_selection
        if (self.app.local_notes is not self or self.app.workbench_pairing is not grant or
                grant._auth is not self.auth):
            raise AppError(403,'LOCAL_NOTES_AUTH_CHANGED','有限配对与当前工作台不同。')
        self.require_login(self.operator)  # Before reading the protected packet.
        packet=_packet(self.app.paths,self.auth.protector)
        expected=_selection(self.app.paths,self.app.config,self.operator,[self.account],grant._extension_id)
        if any(packet[key]!=value for key,value in expected.items()):
            raise AppError(403,'LOCAL_NOTES_AUTH_CHANGED','有限配对必须沿用原操作者、设备、唯一账号及扩展。')
        wanted=expected|{'scopes':sorted(EXTENSION_SCOPES)}
        if grant._authority!=wanted:
            raise AppError(403,'LOCAL_NOTES_AUTH_CHANGED','有限配对不得改变扩展权限。')
        session=self.auth.login(self.operator,packet['login_credential'],
            device_key=self.app.config['device_key'],client_kind='workbench')
        try:
            principal=self.auth.authenticate(session['token'],scope='profile:write',account_key=self.account)
            self.check(principal)
            self.check_pairing_area(self.pairing_file)
        finally:self.auth.logout(session['token'])

    def capabilities(self,principal):
        try:self.check(principal)
        except AppError:
            return {'state':'unavailable','profile_fields':[],'reply_on_demand':False}
        from .local_reply import LocalReplyAccess
        reply=getattr(self.app,'local_reply',None)
        enabled=(isinstance(reply,LocalReplyAccess) and reply.notes is self and
            reply.worker_active(reply.lifecycle) and reply.service.client.status()['available'])
        return {'state':'explicit_local_notes','account_key':self.account,'profile_fields':['note'],
                'reply_on_demand':bool(enabled)}

    def save(self,token,principal,data):
        if data['account_key']!=self.account or data['field']!='note':
            raise AppError(403,'LOCAL_NOTES_ONLY','本次只允许唯一账号下已有联系人的备注。')
        with self.db.lock:
            with self.auth.authorized(token,scope='profile:write',account_key=self.account) as current:
                if current!=principal:
                    raise AppError(403,'LOCAL_NOTES_AUTH_CHANGED','保存前登录状态已变化。')
                self.check(current)
                with self.db.transaction():
                    actor={key:current.as_dict()[key] for key in ('operator_key','device_key','session_id')}
                    result=self.app.profiles.set_field(self.account,data['person_key'],'note',data['value'],
                        expected_revision=data['expected_revision'],actor=actor,clear=data['clear'])
                    # Existing scalar/event/outbox writes share this transaction.
                    self.check(current)
                    if self.auth.authenticate(token,scope='profile:write',account_key=self.account)!=current:
                        raise AppError(403,'LOCAL_NOTES_AUTH_CHANGED','提交前登录状态已变化。')
                return result


def attach_local_notes(app,operator,powershell_exe):
    if getattr(app,'local_notes',None) is not None:
        raise AppError(409,'LOCAL_NOTES_ALREADY_ATTACHED','不能重复接管本地备注运行。')
    grant=LocalNotesAccess(app,operator,powershell_exe)
    app.local_notes=grant
    return grant
