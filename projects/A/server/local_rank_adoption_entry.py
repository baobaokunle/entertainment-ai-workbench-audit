"""Protected host submission of one previously reviewed rank plan, no HTTP API."""
import hashlib
from pathlib import Path
import re
import sqlite3
import uuid

from . import backend_candidates as backend
from .common import AppError
from .db import digest
from .local_device_pairing import _read_protected
from .local_pending_import import registered_import_application,check_import_storage
from .local_workspace_preparation import _acl,_file,_shell_path,local_f_path
from .paths import Paths
from .rank_handoff_adoption import apply

def submit(data_root,request_file,expected_sha256,operator,powershell_exe):
    """Digest is supplied after review; never computed here and called approval."""
    if type(expected_sha256) is not str or re.fullmatch('[0-9a-f]{64}',expected_sha256) is None:
        raise AppError(400,'RANK_REVIEW_DIGEST_REQUIRED','需要事先审核的有限接入计划摘要。')
    paths=Paths.from_root(str(local_f_path(data_root)))
    path=local_f_path(request_file);shell=_shell_path(powershell_exe)
    if path.parent!=paths.root/'validation' or path.suffix!='.json':
        raise AppError(400,'RANK_PLAN_PATH','榜单采纳计划须位于现有工作区受保护validation目录。')
    def read():
        _file(path);_acl(shell,[path.parent,path],timeout_seconds=5)
        raw=_read_protected(path,2*1024*1024)
        if hashlib.sha256(raw).hexdigest()!=expected_sha256:
            raise AppError(409,'RANK_PLAN_CHANGED','计划已变化，未提交。')
        return raw
    raw=read();plan=backend.decode(raw,2*1024*1024)
    binding={'account_key':plan['account_key'],'source_id':plan['source_id'],
             'source_version':'1','account_namespace':'douyin-passport-user_id_str'}
    with registered_import_application(paths.root,operator,binding,shell) as (app,token):
        account=plan['account_key']
        with app.db.lock,app.auth.authorized(token,scope='source:register',account_key=account) as principal:
            principal.require('identities:write',account)
            check_import_storage(app,shell,prepared_login=True)
            if read()!=raw:raise AppError(409,'RANK_PLAN_CHANGED','计划已变化。')
            # Consistent local rollback copy, never copy a live main DB alone.
            backup=paths.backup/('before-rank-'+uuid.uuid4().hex+'.sqlite3')
            with backup.open('xb'):pass
            _file(backup);_acl(shell,[paths.backup,backup],timeout_seconds=5)
            target=sqlite3.connect(backup)
            try:app.db.conn.backup(target)
            finally:target.close()
            _file(backup)
            with app.db.transaction():
                result=apply(plan,digest(plan),app.db,principal)
                check_import_storage(app,shell,prepared_login=True)
                if read()!=raw or app.auth.authenticate(token,scope='source:register',account_key=account)!=principal:
                    raise AppError(409,'RANK_PLAN_CHANGED','最终提交前计划或授权变化。')
        return {**result,'backup_created':True,'read_only_host_reload_required':True}
