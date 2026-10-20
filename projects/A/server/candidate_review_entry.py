"""Explicit fixed F-disk review wiring; no default discovery or business writes."""
import hmac,json,secrets,threading
from pathlib import Path
from .api import unique_object
from .backup_scratch import reject_links
from .candidate_plan_entry import plan_unknown_page
from .common import AppError
from .db import digest
from .import_binding import ImportBindingService

CONFIG_FIELDS={'format','binding','candidate_dir','snapshot_dir','profile_file','profile_sha256'}
REQUEST_FIELDS={'binding','review_key','page_index','page_count','unknown_records'}

def fixed_local_path(value):
    if type(value) is not str or not value or len(value)>2048 or '\x00' in value:
        raise AppError(400,'CANDIDATE_REVIEW_CONFIG_INVALID','只读核对配置路径格式无效')
    path=Path(value)
    if not path.is_absolute() or path.drive.upper()!='F:':
        raise AppError(400,'CANDIDATE_REVIEW_LOCAL_F_REQUIRED','只读核对仅接受明确的本机F盘路径，不接受网络位置')
    reject_links(path)
    return str(path)

class CandidateReviewEntry:
    def __init__(self,app,config_file):
        if not app.read_only or not app.db.read_only or app.file_import is None:
            raise AppError(409,'CANDIDATE_REVIEW_READ_ONLY_REQUIRED','工作台核对需已有只读业务库，不自动迁移或启用来源')
        config_file=fixed_local_path(config_file)
        try:
            with Path(config_file).open('rb') as stream:raw=stream.read(8193)
            if len(raw)>8192:raise ValueError()
            config=json.loads(raw.decode('utf-8'),object_pairs_hook=unique_object,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            if type(config) is not dict or set(config)!=CONFIG_FIELDS or config['format']!='dfa-candidate-review-config-1':raise ValueError()
            ImportBindingService._binding(config['binding'])
            for key in ('candidate_dir','snapshot_dir','profile_file'):config[key]=fixed_local_path(config[key])
            profile_hash=config['profile_sha256']
            if type(profile_hash) is not str or len(profile_hash)!=64 or any(c not in '0123456789abcdef' for c in profile_hash):raise ValueError()
        except (OSError,ValueError,UnicodeError,RecursionError):
            raise AppError(400,'CANDIDATE_REVIEW_CONFIG_INVALID','本机只读核对配置未通过有限结构检查') from None
        # Configuration content is copied, the profile bytes are authenticated
        # after authorization on each request, and a restart invalidates the key.
        self.app,self.config=app,config
        self.review_key=digest({'config':config,'instance':secrets.token_hex(32)})
        self.lock=threading.Lock();self.stopping=threading.Event()
    def stop(self):self.stopping.set()
    def _active(self):
        if self.stopping.is_set():raise AppError(503,'CANDIDATE_REVIEW_STOPPING','本机核对服务正在收尾，不发布新计划')
    def context(self,account,principal):
        self._active();service=ImportBindingService(self.app.file_import)
        service._authorize(principal,account)
        if account!=self.config['binding']['account_key']:
            return {'format':'dfa-candidate-review-context-1','configured':False,'account_key':account,'persisted':False,'real_write_enabled':False}
        service.check_target(self.config['binding'],principal)
        return {'format':'dfa-candidate-review-context-1','configured':True,'account_key':account,
            'binding':dict(self.config['binding']),'binding_scope_digest':digest(self.config['binding']),'review_key':self.review_key,'page_count_limit':20,
            'persisted':False,'source_revalidated':False,'real_write_enabled':False,'commit_authorized':False}
    def plan(self,request,principal,reauthorize):
        self._active()
        if type(request) is not dict or set(request)!=REQUEST_FIELDS or request['unknown_records'] is not True:
            raise AppError(400,'CANDIDATE_REVIEW_REQUEST_INVALID','核对仅接受明确范围与保留未知选择，不接受文件路径')
        ImportBindingService._binding(request['binding'])
        key=request['review_key']
        if request['binding']!=self.config['binding'] or type(key) is not str or len(key)!=64 or any(c not in '0123456789abcdef' for c in key) or not hmac.compare_digest(key,self.review_key):
            raise AppError(409,'CANDIDATE_REVIEW_CONFIG_STALE','核对配置或服务会话已变化，请重新读取核对范围')
        if not self.lock.acquire(blocking=False):raise AppError(409,'CANDIDATE_REVIEW_BUSY','已有只读核对正在执行，不启动重复审查')
        def current(original):
            self._active()
            return reauthorize(original)
        try:
            result=plan_unknown_page(self.app,request['binding'],principal,
                candidate_dir=self.config['candidate_dir'],snapshot_dir=self.config['snapshot_dir'],
                profile_file=self.config['profile_file'],profile_sha256=self.config['profile_sha256'],
                page_index=request['page_index'],page_count=request['page_count'],reauthorize=current)
            if current(principal)!=principal:raise AppError(403,'CANDIDATE_IMPORT_AUTH_CHANGED','本机会话已变化，不发布计划')
            return result|{'review_key':self.review_key}
        finally:self.lock.release()
