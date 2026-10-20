"""Explicit candidate lifecycle. Never defaults to the user's old installation/data."""
import argparse, getpass, json, secrets, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.dont_write_bytecode=True
sys.path[:0]=[str(ROOT),str(ROOT/'vendor')]

def _retain_engine_resources(app,server,worker,session,engine):
    import threading,time
    if getattr(engine,'_retainer',None) is not None:return
    def collect():
        while True:
            try:
                _finish_engine_serving(app,server,worker,session,engine,defer=False)
                return
            except BaseException:
                if getattr(engine,'_serving_cleanup',{}).get('released'):return
                time.sleep(1)
    # A live non-daemon owner, not only Python variables or a PID/control file.
    thread=threading.Thread(target=collect,name='DSH-owned-model-stop-pending',daemon=False)
    engine._retainer=thread
    try:thread.start()
    except BaseException:collect()

def _finish_engine_serving(app,server,worker,session,engine,*,defer=True):
    from server.common import AppError
    import threading
    state=getattr(engine,'_serving_cleanup',None)
    if state is None:
        state={'http_closer':None,'http_complete':server is None,'worker_stop_requested':False,
            'worker_complete':worker is None,'engine_complete':False,'auth_complete':session is None,
            'app_complete':False,'released':False,'result':None}
        engine._serving_cleanup=state
    if state['released']:return state['result']
    try:
        if not state['http_complete']:
            closer=state['http_closer']
            if closer is None:
                closer={'finished':threading.Event(),'completed':False,'error':False}
                def close_http():
                    try:server.server_close();closer['completed']=True
                    except BaseException:closer['error']=True
                    finally:closer['finished'].set()
                thread=threading.Thread(target=close_http,name='DSH-owned-model-HTTP-close',daemon=False)
                closer['thread']=thread
                # Publish before start: an interruption inside start leaves its
                # outcome uncertain, so a second closer must never be created.
                state['http_closer']=closer
                try:thread.start()
                except BaseException:raise AppError(503,'AI_HTTP_CLOSE_START_PENDING','HTTP收尾拥有者尚未启动') from None
            if not closer['finished'].wait(20) or not closer['completed']:
                raise AppError(503,'AI_HTTP_STOP_PENDING','同一个HTTP收尾仍未完整完成')
            state['http_complete']=True
        if not state['worker_complete']:
            if not state['worker_stop_requested']:
                worker.stop();state['worker_stop_requested']=True
            if worker.thread:
                worker.thread.join(95)
                if worker.thread.is_alive():raise AppError(503,'AI_WORKER_STOP_PENDING','参考处理仍在收尾')
            state['worker_complete']=True
        if not state['engine_complete']:
            result=engine.stop()
            if result.get('stopped') is not True:raise AppError(503,'AI_ENGINE_STOP_PENDING','本机模型仍在收尾')
            state['engine_complete']=True;state['result']=result
        if not state['auth_complete']:
            app.auth.logout(session['token']);state['auth_complete']=True
        if not state['app_complete']:
            app.close();state['app_complete']=True
    except BaseException:
        if defer:_retain_engine_resources(app,server,worker,session,engine)
        if state['released']:return state['result']
        raise AppError(503,'AI_SERVING_STOP_PENDING','本机服务／参考／模型仍在收尾；拥有者继续保留资源，不强杀或声明已停止') from None
    state['released']=True
    # Output is not a resource phase and must never restart cleanup on a closed DB.
    try:print(json.dumps({'owned_model_stop':state['result']},ensure_ascii=False))
    except (OSError,UnicodeError):pass
    return state['result']

def _finish_serving(app,server=None,worker=None,session=None,engine=None):
    if engine is not None:return _finish_engine_serving(app,server,worker,session,engine)
    candidate_review=getattr(app,'candidate_review',None)
    pairing=getattr(app,'workbench_pairing',None) or getattr(app,'web_login',None)
    requires_http_completion=server is not None
    server_finished=server is None
    try:
        if candidate_review:candidate_review.stop()
        if server:
            try:server.server_close()
            except BaseException:
                if requires_http_completion:
                    from server.common import AppError
                    code=('CANDIDATE_REVIEW_STOP_PENDING' if candidate_review else
                          'WORKBENCH_PAIRING_STOP_PENDING' if pairing else 'WORKBENCH_HTTP_STOP_PENDING')
                    raise AppError(503,code,'本机只读请求仍在收尾；保留数据库资源，不强杀、不声明已停止') from None
                raise
        server_finished=True
    finally:
        try:
            if worker:
                worker.stop()
                if worker.thread:
                    worker.thread.join(95)
                    if worker.thread.is_alive():
                        from server.common import AppError
                        raise AppError(503,'AI_WORKER_STOP_PENDING','本机参考处理仍在收尾；保留数据库资源，不强杀、不声明已停止')
        finally:
            try:
                if session:app.auth.logout(session['token'])
            finally:
                # A non-daemon serving worker keeps Python alive until it exits.
                if (not requires_http_completion or server_finished) and (not worker or not worker.thread or not worker.thread.is_alive()):app.close()

def main():
    parser=argparse.ArgumentParser(description='隔离候选工程；默认不启用真实来源')
    parser.add_argument('command',choices=['init','enroll','prepare-passwordless','prepare-local-import','local-pending-import','local-chatlab-import','serve','serve-readonly','check','migrate','migrate-file-import','init-keys','backup','verify-backup','restore-backup','export-recovery','import-recovery','ai-process-next','candidate-plan','sync-init','sync-public','sync-pack','sync-import','sync-retry','sync-preserve','sync-recover','deletion-plan','deletion-manifest','diagnostics-init'])
    parser.add_argument('--data-root',required=True)
    parser.add_argument('--device-key')
    parser.add_argument('--device-role',choices=['manager','baobao'])
    parser.add_argument('--operator-key')
    parser.add_argument('--account',action='append',default=[])
    parser.add_argument('--extension-id',action='append',default=[])
    parser.add_argument('--fictional',action='store_true',help='只启用临时虚构数据写入；不是真实账号激活')
    parser.add_argument('--file',help='登记备份目录内的文件编号；prepare-local-import时为受保护请求文件的绝对路径')
    parser.add_argument('--channel',choices=['messages','actions','profile','collection','config','model','activities'])
    parser.add_argument('--message-key',help='单条候选消息键，仅用于只读必要删除盘点')
    parser.add_argument('--target-kind',choices=['message','observation'],help='只读删除清单的明确目标类型')
    parser.add_argument('--target-key',help='只读删除清单的稳定目标键')
    parser.add_argument('--model-assets',help='明确的固定公开模型资产绝对目录；虚构管理或显式local-reply可用，此参数本身不下载或启动模型')
    parser.add_argument('--ai-worker',action='store_true',help='显式处理虚构管理工作区已请求的参考队列；需既有认证及唯一账号，不发起额外请求')
    parser.add_argument('--start-model-engine',action='store_true',help='显式创建并管理本次固定CPU引擎，仅已有虚构管理serve及原认证；无下载或真实源激活')
    parser.add_argument('--candidate-dir',help='已有完整候选库绝对目录，仅candidate-plan只读审查')
    parser.add_argument('--snapshot-dir',help='已有固定来源绝对目录；不重建快照')
    parser.add_argument('--candidate-profile',help='独立固定profile JSON绝对文件；不可从候选自身推定')
    parser.add_argument('--source-id')
    parser.add_argument('--source-version')
    parser.add_argument('--namespace')
    parser.add_argument('--page-index',type=int)
    parser.add_argument('--page-count',type=int,help='candidate-plan明确连续1至20页；同一审查会话复用来源核验，默认1')
    parser.add_argument('--unknown-records',action='store_true',help='明确保留身份映射未知；candidate-plan只生成计划，local-chatlab-import受控保存')
    parser.add_argument('--candidate-review-config',help='仅serve-readonly显式加载F盘固定核对配置；默认不扫描候选资料')
    parser.add_argument('--pair-local',action='store_true',help='只消费完整正常配对登记；不初始化或接管试点')
    parser.add_argument('--web-login',action='store_true',help='原工作台一次码登录，沿用只读/虚构/有限本地选择；不改变原角色权限、不重建登记')
    parser.add_argument('--pairing-duration',type=int,help='正常配对有限窗口1至1800秒；临时码最多300秒')
    parser.add_argument('--pairing-until-stopped',action='store_true',help='正常只读工作台持续连接至手动停止或退出；配对码及挑战仍短时有效')
    parser.add_argument('--pairing-ui-shell',help='明确本机PowerShell7用于临时码窗口；码不进入参数或输出')
    parser.add_argument('--snapshot-file',help='有限导入明确批准的原文件；local-chatlab-import可选择JSONL或ZIP，不改原件')
    parser.add_argument('--snapshot-sha256',help='已批准快照的完整SHA256')
    parser.add_argument('--document-sha256',help='ChatLab原文件或ZIP成员的完整SHA256')
    parser.add_argument('--member-index',type=int,help='ChatLab ZIP非目录成员序号，从1开始；不解压')
    parser.add_argument('--preview-sha256',help='已核对首批预览的SHA256')
    parser.add_argument('--node-exe',help='有限导入沿用已有本机归一化程序')
    parser.add_argument('--protection-shell',help='有限导入沿用已有本机PowerShell7保护检查')
    parser.add_argument('--row-limit',type=int,help='有限导入明确的首批1至100条')
    parser.add_argument('--local-notes',action='store_true',help='仅serve与已有免记密码登记：显式开放受保护工作区的备注，其他写入关闭')
    parser.add_argument('--local-reply',action='store_true',help='仅有限备注与网页登录：显式启动本次受保护模型，只处理主动回复，不恢复旧队列')
    args=parser.parse_args()
    if args.command=='prepare-local-import':
        allowed={'command','data_root','file','protection_shell'}
        if (not args.file or not args.protection_shell or
                any(value is not None and value is not False and not (type(value) is list and not value)
                    for name,value in vars(args).items() if name not in allowed)):
            parser.error('prepare-local-import仅接受新受保护工作区、受保护请求文件及本机保护检查程序')
        from server.local_import_preparation import prepare_local_import
        from server.common import AppError
        try:
            result=prepare_local_import(args.data_root,args.file,args.protection_shell)
        except Exception as error:
            code=error.code if isinstance(error,AppError) else 'LOCAL_PREPARATION_FAILED'
            raise AppError(409,code,'有限准备未完成；保留已有资料，不覆盖、不启动或导入。') from None
        print(json.dumps(result,ensure_ascii=False));return
    if args.command=='local-chatlab-import':
        required=('snapshot_file','snapshot_sha256','document_sha256','node_exe','protection_shell','source_id','source_version','namespace')
        allowed={'command','data_root','operator_key','account','member_index','unknown_records',*required}
        if (not args.operator_key or len(args.account)!=1 or not args.unknown_records or
                not all(getattr(args,name) is not None for name in required) or
                (args.member_index is not None and not 1<=args.member_index<=10000) or
                any(value is not None and value is not False and not (type(value) is list and not value)
                    for name,value in vars(args).items() if name not in allowed)):
            parser.error('local-chatlab-import需已有唯一账号/来源登记、明确文件与摘要及unknown-records；不混用其他业务参数')
        from server.chatlab_pending_import import import_registered_chatlab_document
        from server.common import AppError
        try:
            result=import_registered_chatlab_document(args.data_root,args.operator_key,
                {'account_key':args.account[0],'source_id':args.source_id,'source_version':args.source_version,'account_namespace':args.namespace},
                source=args.snapshot_file,expected_source_sha256=args.snapshot_sha256,document_sha256=args.document_sha256,
                member_index=args.member_index,node_exe=args.node_exe,powershell_exe=args.protection_shell)
        except Exception as error:
            code=error.code if isinstance(error,AppError) else 'CHATLAB_ENTRY_FAILED'
            raise AppError(409,code,'旧聊天导入未全部完成；原件和已提交批次保留，可用相同输入续传。') from None
        print(json.dumps(result,ensure_ascii=False));return
    if args.document_sha256 is not None or args.member_index is not None:
        parser.error('ChatLab文档选择参数仅用于local-chatlab-import')
    pending_fields=('snapshot_file','snapshot_sha256','preview_sha256','node_exe','protection_shell','row_limit')
    if args.command=='local-pending-import':
        allowed={'command','data_root','operator_key','account','source_id','source_version','namespace',*pending_fields}
        if (any(value is not None and value is not False and not (type(value) is list and not value)
                for name,value in vars(args).items() if name not in allowed) or
                not args.operator_key or len(args.account)!=1 or
                not all(getattr(args,name) is not None for name in (*pending_fields,'source_id','source_version','namespace')) or
                not 1<=args.row_limit<=100):
            parser.error('local-pending-import需完整已有登记、唯一账号、来源/摘要/页界和本机工具，不混用启动/模型/同步/候选分页参数')
        from server.local_pending_import import import_registered_first_page
        from server.common import AppError
        try:
            result=import_registered_first_page(args.data_root,args.operator_key,
                {'account_key':args.account[0],'source_id':args.source_id,'source_version':args.source_version,'account_namespace':args.namespace},
                source=args.snapshot_file,expected_sha256=args.snapshot_sha256,expected_preview_sha256=args.preview_sha256,
                node_exe=args.node_exe,powershell_exe=args.protection_shell,limit=args.row_limit)
        except Exception as error:
            code=error.code if isinstance(error,AppError) else 'LOCAL_IMPORT_ENTRY_FAILED'
            raise AppError(409,code,'有限导入未返回成功回执；保留已有资料，未开启日常业务或发送。') from None
        print(json.dumps(result,ensure_ascii=False));return
    if args.local_reply and (not args.local_notes or not args.model_assets):
        parser.error('local-reply需显式local-notes及固定模型资产；账号沿用有限登记，不单独指定')
    if args.local_notes:
        allowed={'command','data_root','operator_key','web_login','local_notes','protection_shell','pairing_duration','pairing_ui_shell'}
        if args.local_reply:allowed.update({'local_reply','model_assets'})
        if args.pair_local:allowed.update({'pair_local','extension_id'})
        if (args.command!='serve' or not args.web_login or not args.protection_shell or not args.operator_key or
                any(value is not None and value is not False and not (type(value) is list and not value)
                    for name,value in vars(args).items() if name not in allowed)):
            parser.error('有限本地模式仅允许serve、已有网页登录登记及保护检查；回复须明确local-reply，不混用通用worker、账号、导入或同步参数')
    if any(getattr(args,name) is not None for name in pending_fields if not (args.local_notes and name=='protection_shell')):
        parser.error('有限快照导入参数仅用于local-pending-import')
    finite_pairing=args.local_notes and args.web_login and args.pair_local
    if args.pairing_until_stopped and (args.command!='serve-readonly' or not args.pair_local or args.web_login or args.local_notes):
        parser.error('pairing-until-stopped仅用于正常serve-readonly及pair-local，不用于有限业务或网页登录')
    if args.web_login and ((not finite_pairing and (args.pair_local or args.extension_id)) or args.command not in ('serve-readonly','serve') or not args.operator_key or
                          (args.account and not (args.ai_worker or args.start_model_engine)) or
                          args.pairing_duration is None or not 1<=args.pairing_duration<=300):
        parser.error('web-login需原登记及1至300秒窗口；仅有限本地模式可显式同时配对扩展')
    if args.pair_local and ((args.command!='serve-readonly' and not finite_pairing) or not args.operator_key or len(args.extension_id)!=1 or args.account or
                           args.pairing_duration is None or not 1<=args.pairing_duration<=1800):
        parser.error('pair-local需只读或有限本地启动、已有operator、唯一extension-id及有限窗口')
    if not (args.pair_local or args.web_login) and (args.pairing_duration is not None or args.pairing_ui_shell is not None):
        parser.error('pairing参数仅用于明确pair-local或web-login')
    if args.command=='prepare-passwordless' and (not args.operator_key or len(args.extension_id)!=1):
        parser.error('prepare-passwordless需明确operator-key及唯一已登记extension-id，账号按明确account范围')
    if args.candidate_review_config is not None and args.command!='serve-readonly':parser.error('candidate-review-config仅允许serve-readonly，不启用可写业务')
    if args.model_assets is not None and args.command not in ('serve','ai-process-next'):
        parser.error('model-assets仅允许serve或ai-process-next；只读及其他命令不能加载模型')
    if args.ai_worker and (args.command!='serve' or args.model_assets is None or not args.operator_key or len(args.account)!=1):
        parser.error('ai-worker仅用于serve，需model-assets、operator-key和唯一account；默认及只读不启动')
    if args.start_model_engine and (args.command!='serve' or args.model_assets is None or not args.operator_key or len(args.account)!=1):
        parser.error('start-model-engine仅用于serve，需model-assets、已有operator-key和唯一account；默认、只读及其他命令不启动')
    candidate_options=(args.candidate_dir,args.snapshot_dir,args.candidate_profile,args.source_id,args.source_version,args.namespace,args.page_index)
    if args.command!='candidate-plan' and (any(value is not None for value in candidate_options) or args.page_count is not None or args.unknown_records):parser.error('候选分页参数仅用于candidate-plan')
    if args.command=='candidate-plan' and (not args.operator_key or len(args.account)!=1 or not all(value is not None for value in candidate_options) or args.page_index<1 or not args.unknown_records):
        parser.error('candidate-plan需管理认证、唯一account、完整候选/来源/profile参数、正页号及unknown-records明确选择')
    if args.command=='candidate-plan' and args.page_count is not None and not 1<=args.page_count<=20:
        parser.error('candidate-plan的page-count仅允许1至20')
    if args.command=='check':
        from check import diagnose
        print(json.dumps(diagnose(args.data_root),ensure_ascii=False,indent=2)); return
    from server.paths import Paths
    from server.common import AppError
    from server.settings import DEFAULTS,read_settings,write_settings
    if args.command=='diagnostics-init':
        if not args.fictional:
            raise AppError(403,'DIAGNOSTICS_FIXTURE_ONLY','诊断初始化目前仅允许显式虚构候选；未启用真实资料记录')
        from server.backup_scratch import reject_links
        reject_links(Path(args.data_root))
    if args.pair_local or args.web_login or args.command=='prepare-passwordless':
        from server.backup_scratch import reject_links
        reject_links(Path(args.data_root))
    paths=Paths.from_root(args.data_root)
    if args.pair_local or args.web_login or args.command=='prepare-passwordless':
        from server.local_device_pairing import _file_stat
        for filename in (paths.settings,paths.db):_file_stat(filename)
        _file_stat(paths.auth,missing_ok=args.command=='prepare-passwordless')
        _file_stat(paths.root/'keys'/'workbench-device.dpapi',missing_ok=args.command=='prepare-passwordless')
    if args.command=='init':
        if paths.db.exists() or paths.settings.exists(): raise AppError(409,'already_initialized','目录已初始化；不会覆盖')
        if not args.device_key or not args.device_role: parser.error('init需要device-key及device-role')
        config=DEFAULTS|{'device_key':args.device_key,'device_role':args.device_role,
                         'extension_ids':args.extension_id,'source_mode':'fictional' if args.fictional else 'unverified'}
        from server.settings import validate_settings
        config=validate_settings(config)
        paths.initialize_directories()
        from server.db import Database
        with_db=Database(paths.db,create=True)
        try:
            with_db.set_runtime_identity(args.device_key)
            from server.migrations import install_candidate_features
            install_candidate_features(with_db)
            from server.file_import import initialize_import_schema
            initialize_import_schema(with_db)
            if args.device_role=='baobao':
                from server.collection import CollectionService
                collector=CollectionService(with_db,owner_device=args.device_key);collector.initialize()
                with with_db.transaction() as conn:conn.execute('UPDATE collection_config SET enabled=0 WHERE task_key=?',(collector.task_key,))
        finally: with_db.close()
        write_settings(paths.settings,config)
        from server.keys import KeyStore
        KeyStore(paths.root/'keys'/'backup.dpapi').initialize()
        print('候选目录已初始化；真实来源、同步、模型和定时作业均未激活。'); return
    config=read_settings(paths.settings)
    if args.web_login and args.command=='serve' and config['source_mode']!='fictional' and not args.local_notes:
        raise AppError(403,'WEB_LOGIN_FICTIONAL_REQUIRED','可写免记密码启动只允许已有虚构配置，不改变真实来源')
    if args.command=='prepare-passwordless':
        from server.workbench_pairing_entry import prepare_workbench_pairing
        result=prepare_workbench_pairing(paths,config,args.operator_key,args.account,args.extension_id[0])
        print(json.dumps(result,ensure_ascii=False));return
    if args.model_assets is not None and not args.local_reply and (config['source_mode']!='fictional' or config['device_role']!='manager'):
        raise AppError(403,'AI_MODEL_EXPLICIT_FIXTURE_ONLY','model-assets需明确虚构管理配置；不改来源或设备角色')
    if args.command=='candidate-plan':
        from server.dsh_app import Application
        from server.candidate_plan_entry import plan_unknown_page
        app=Application(paths,read_only=True);session=None
        try:
            session=app.auth.login(args.operator_key,getpass.getpass('本机管理只读计划凭据：'),device_key=config['device_key'])
            principal=session['principal'];account=args.account[0]
            binding={'account_key':account,'source_id':args.source_id,'source_version':args.source_version,'account_namespace':args.namespace}
            def reauthorize(original):return app.auth.authenticate(session['token'],scope='identities:write',account_key=account)
            result=plan_unknown_page(app,binding,principal,candidate_dir=args.candidate_dir,snapshot_dir=args.snapshot_dir,profile_file=args.candidate_profile,page_index=args.page_index,reauthorize=reauthorize,page_count=args.page_count if args.page_count is not None else 1)
            if reauthorize(principal)!=principal:raise AppError(403,'CANDIDATE_IMPORT_AUTH_CHANGED','本机会话已变化，不发布计划')
            print(json.dumps(result,ensure_ascii=False,indent=2))
        finally:
            try:
                if session:app.auth.logout(session['token'])
            finally:app.close()
        return
    if args.command=='diagnostics-init':
        if config['source_mode']!='fictional':
            raise AppError(403,'DIAGNOSTICS_FIXTURE_ONLY','诊断初始化需明确虚构候选配置；不会自动切换来源模式')
        if not args.operator_key:
            parser.error('diagnostics-init需要operator-key及本机管理认证')
        from server.auth import AuthStore
        from server.diagnostics import DiagnosticsStore
        from server.backup_scratch import reject_links
        auth=AuthStore(paths.auth,allowed_extension_ids=config['extension_ids'])
        session=auth.login(args.operator_key,getpass.getpass('本机诊断初始化凭据：'),device_key=config['device_key'])
        created_directory=False
        directory=paths.root/'diagnostics'
        try:
            principal=session['principal']
            principal.require('diagnostics:configure')
            if principal.role!='manager' or principal.device_key!=config['device_key']:
                raise AppError(403,'DIAGNOSTICS_MANAGER_REQUIRED','诊断初始化仅允许此设备已认证管理')
            reject_links(directory)
            if not directory.exists():
                directory.mkdir(mode=0o700)
                created_directory=True
            initialized=DiagnosticsStore.initialize(directory/'events.sqlite3',config['device_key'],fixture_mode=True)
            print('本机虚构诊断库已显式初始化；记录与14天维护均关闭，没有安装定时任务。')
            if initialized.initialization_stage=='requires_review':
                print('诊断库已发布；加密暂存清理状态需复核，保持记录与维护关闭。')
        except Exception:
            # Remove only the empty directory created by this command. Never
            # remove a winner's file, populated directory or pre-existing path.
            if created_directory:
                try:directory.rmdir()
                except OSError:pass
            raise
        finally:
            auth.logout(session['token'])
        return
    if args.command=='deletion-manifest':
        if not args.operator_key or len(args.account)!=1 or not args.target_kind or not args.target_key:
            parser.error('deletion-manifest需要operator-key、唯一account、target-kind和target-key')
        if config['source_mode']!='fictional':raise AppError(403,'isolated_deletion_plan_only','本轮完整盘点仅允许隔离虚构资料；不执行删除')
        from server.auth import AuthStore
        from server.db import Database
        from server.deletion_manifest import DeletionManifestService
        auth=AuthStore(paths.auth,allowed_extension_ids=config['extension_ids'])
        session=auth.login(args.operator_key,getpass.getpass('本机管理盘点凭据：'),device_key=config['device_key'])
        db=None
        try:
            db=Database(paths.db,read_only=True)
            print(json.dumps(DeletionManifestService(db).plan(args.account[0],args.target_kind,args.target_key,session['principal']),ensure_ascii=False,indent=2))
        finally:
            if db:db.close()
            auth.logout(session['token'])
        return
    if args.command=='deletion-plan':
        if not args.operator_key or len(args.account)!=1 or not args.message_key:parser.error('deletion-plan需要operator-key、唯一account和message-key')
        if config['source_mode']!='fictional':raise AppError(403,'isolated_deletion_plan_only','本轮盘点入口仅允许隔离虚构资料；不执行删除')
        from server.auth import AuthStore
        from server.db import Database
        from server.deletion_plan import DeletionPlanner
        auth=AuthStore(paths.auth,allowed_extension_ids=config['extension_ids'])
        session=auth.login(args.operator_key,getpass.getpass('本机管理盘点凭据：'),device_key=config['device_key'])
        db=None
        try:
            principal=session['principal'];principal.require('identities:write',args.account[0]);principal.require('history:read',args.account[0])
            if principal.role!='manager':raise AppError(403,'MANAGER_REQUIRED','盘点仅限管理端')
            db=Database(paths.db,read_only=True)
            print(json.dumps(DeletionPlanner(db,paths.root).plan(args.account[0],args.message_key,principal),ensure_ascii=False,indent=2))
        finally:
            if db:db.close()
            auth.logout(session['token'])
        return
    if args.command in ('migrate','migrate-file-import'):
        if config['source_mode']!='fictional':raise AppError(403,'isolated_migration_only','本轮迁移命令仅用于隔离虚构候选库')
        from server.db import Database
        from server.migrations import install_candidate_features
        db=Database(paths.db,create=False)
        try:
            if args.command=='migrate':install_candidate_features(db)
            from server.file_import import initialize_import_schema
            initialize_import_schema(db)
        finally:db.close()
        print('候选扩展模式已显式迁移；未读取旧安装或激活真实来源。');return
    if args.command=='enroll':
        if not args.operator_key: parser.error('enroll需要operator-key')
        if not config['device_key'] or not config['device_role']: raise AppError(409,'initialization_required','请先显式初始化候选目录')
        from server.auth import AuthStore
        credential=getpass.getpass('本机启动凭据（不会保存明文或打印）：')
        repeat=getpass.getpass('重复输入：')
        if not secrets.compare_digest(credential.encode('utf-8'),repeat.encode('utf-8')): raise AppError(400,'credential_mismatch','两次凭据不同')
        AuthStore(paths.auth,allowed_extension_ids=config['extension_ids']).enroll(
            args.operator_key,config['device_role'],config['device_key'],credential,account_keys=args.account)
        print('本机身份已登记；角色和账号权限由服务端登记决定。'); return
    if args.command in ('init-keys','backup','verify-backup','restore-backup','export-recovery','import-recovery'):
        if not args.operator_key: parser.error('此操作需要operator-key及本机备份认证')
        from server.auth import AuthStore
        from server.keys import KeyStore
        session=AuthStore(paths.auth,allowed_extension_ids=config['extension_ids']).login(
            args.operator_key,getpass.getpass('本机启动凭据：'),device_key=config['device_key'])
        local_scope='backup:local:create' if args.command in ('init-keys','backup','export-recovery') else 'backup:local:verify' if args.command=='verify-backup' else 'backup:local:restore'
        session['principal'].require(local_scope)
        keys=KeyStore(paths.root/'keys'/'backup.dpapi')
        if args.command=='init-keys':
            keys.initialize(); print('备份钥已由本机Windows用户保护；恢复文件需单独导出并保管。'); return
        if args.command=='export-recovery':
            passphrase=getpass.getpass('恢复口令（至少16字符）：')
            repeat=getpass.getpass('重复恢复口令：')
            if not secrets.compare_digest(passphrase.encode(),repeat.encode()): raise AppError(400,'credential_mismatch','两次口令不同')
            filename='recovery-'+secrets.token_hex(16)+'.dfar'
            result=keys.export_recovery(paths.backup/filename,passphrase)
            print('已生成加密恢复文件：'+filename); return
        import re
        filename=args.file
        pattern=r'recovery-[0-9a-f]{32}\.dfar' if args.command=='import-recovery' else r'[0-9a-f]{32}\.dfab'
        if args.command!='backup' and (not filename or not re.fullmatch(pattern,filename)):
            parser.error('file必须为登记备份目录中的文件编号')
        if args.command=='import-recovery':
            keys.import_recovery(paths.backup/filename,getpass.getpass('恢复口令：'))
            print('恢复钥已验证并转存为本机DPAPI保护；未覆盖任何现存钥。'); return
        from server.backup import BackupService
        service=BackupService(paths.db,paths.backup,key=keys.load())
        if args.command=='backup': result=service.create()
        elif args.command=='verify-backup': result=service.verify(filename)
        else:
            destination=paths.root/'staging'/('restore-'+secrets.token_hex(16)+'.sqlite3')
            result=service.restore(filename,destination)
        print(json.dumps(result,ensure_ascii=False,indent=2)); return
    if args.command=='ai-process-next':
        if not args.operator_key or len(args.account)!=1:parser.error('ai-process-next需要operator-key及唯一account，仅用于虚构候选测试')
        if config['source_mode']!='fictional':raise AppError(403,'isolated_ai_test_only','候选处理仅允许隔离虚构模式')
        from server.auth import AuthStore
        from server.dsh_app import Application
        auth=AuthStore(paths.auth,allowed_extension_ids=config['extension_ids'])
        session=auth.login(args.operator_key,getpass.getpass('本机管理测试凭据：'),device_key=config['device_key'])
        try:
            principal=session['principal'];principal.require('model:request',args.account[0])
            if principal.role!='manager':raise AppError(403,'manager_required','候选处理仅允许管理测试')
            app=Application(paths,auth=auth)
            try:
                if app.ai is None:raise AppError(503,'AI_MIGRATION_REQUIRED','请先显式迁移隔离候选库')
                if args.model_assets is not None:
                    principal.require('identities:write',args.account[0])
                    from server.local_model_setup import bind_explicit_local_model
                    bind_explicit_local_model(app,args.model_assets)
                print(json.dumps(app.ai.process_next(account_key=args.account[0]),ensure_ascii=False,indent=2))
            finally:app.close()
        finally:auth.logout(session['token'])
        return
    from server.dsh_app import Application
    from server.http_server import create_server
    if args.local_notes:
        from server.local_notes import check_notes_workspace
        from server.db import Database
        if check_notes_workspace(paths,args.protection_shell)!=config:
            raise AppError(409,'LOCAL_NOTES_CONFIGURATION_CHANGED','打开业务库前配置已变化。')
        owned_db=Database(paths.db,create=False)
        try:app=Application(paths,database=owned_db)
        except BaseException:
            owned_db.close();raise
    else:app=Application(paths,read_only=args.command=='serve-readonly')
    if args.command.startswith('sync-'):
        try:
            if config['source_mode']!='fictional':raise AppError(403,'isolated_sync_only','本轮仅运行隔离虚构同步；真实来源未激活')
            if not args.operator_key:parser.error('同步维护需要operator-key及本机启动认证')
            session=app.auth.login(args.operator_key,getpass.getpass('本机同步维护凭据：'),device_key=config['device_key'])
            principal=session['principal'];principal.require('identities:write' if principal.role=='manager' else 'collection:configure')
            from server.sync_runtime import SyncRuntime
            if args.command=='sync-init':result=SyncRuntime.initialize(app.db,paths.root)
            elif args.command=='sync-public':
                from server.keys import KeyStore
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
                from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
                private=Ed25519PrivateKey.from_private_bytes(KeyStore(paths.root/'keys/sync-signing.dpapi').load())
                result={'device_key':config['device_key'],'public_signing_key':private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex(),'schema_version':2,'protocol_version':1}
            elif args.command=='sync-pack':
                if not args.channel:parser.error('sync-pack需要明确channel')
                result=app.sync.publish(args.channel)
            elif args.command=='sync-import':result=app.sync.import_file(args.file)
            elif args.command=='sync-retry':result=app.sync.retry_dependencies()
            elif args.command=='sync-preserve':result=app.sync.preserve_before_restore()
            else:result=app.sync.recover_journal()
            print(json.dumps(result,ensure_ascii=False,indent=2));return
        finally:app.close()
    server=None;worker=None;worker_session=None;engine=None;worker_auth=None
    try:
        handoffs=[]
        if args.local_notes:
            from server.local_notes import attach_local_notes
            attach_local_notes(app,args.operator_key,args.protection_shell)
        if args.local_reply:
            from server.local_reply import attach_local_reply
            attach_local_reply(app)
        worker_account=app.local_notes.account if args.local_reply else args.account[0] if args.account else None
        if args.candidate_review_config is not None:
            from server.candidate_review_entry import CandidateReviewEntry
            app.candidate_review=CandidateReviewEntry(app,args.candidate_review_config)
        if args.ai_worker or args.start_model_engine or args.local_reply:
            if app.ai is None:raise AppError(503,'AI_MIGRATION_REQUIRED','已有AI schema尚未初始化；不自动迁移')
            from server.workbench_web_login import prepared_worker_session,worker_session_guard
            if args.web_login:
                worker_session,worker_auth=prepared_worker_session(app,args.operator_key,worker_account)
            else:
                worker_session=app.auth.login(args.operator_key,getpass.getpass('本机管理启动凭据：'),device_key=config['device_key'])
                worker_auth=worker_session_guard(app,worker_session,worker_account)
            principal=worker_session['principal'];principal.require('model:request',worker_account);principal.require('identities:write',worker_account)
            if principal.role!='manager':raise AppError(403,'manager_required','参考／引擎处理需要当前已登记的管理权限')
        if args.local_reply:app.local_reply.require_source(principal)
        if args.start_model_engine or args.local_reply:
            from server.local_model_runtime import OwnedLocalModel
            options={'protection_shell':args.protection_shell} if args.local_reply else {}
            engine=OwnedLocalModel(args.model_assets,paths.root,**options)
            engine.start()
        if args.model_assets is not None:
            if args.local_reply:
                from server.local_model_setup import bind_local_reply_model
                bind_local_reply_model(app,args.model_assets,engine)
            else:
                from server.local_model_setup import bind_explicit_local_model
                bind_explicit_local_model(app,args.model_assets)
                if engine:engine.guard_client(app.ai.client)
            print('本地固定模型已按本次启动选择登记；未请求生成，真实业务仍待验收。')
        if worker_auth is not None:worker_auth()
        if args.ai_worker or args.local_reply:
            from server.ai_worker import AiWorker
            from server.workbench_web_login import worker_commit_guard
            worker=AiWorker(app.ai,fictional=not args.local_reply,reauthorize=worker_auth,
                            commit_guard=None if args.local_reply else worker_commit_guard(app,worker_session,worker_account))
            if worker.start(principal,worker_account,background=True,daemon=False)['state']!='running':raise AppError(503,'AI_WORKER_UNAVAILABLE','已有参考处理未启动')
        if args.pair_local:
            # Both bounded login windows start after optional model preparation.
            from server.workbench_pairing_entry import attach_workbench_pairing,pairing_handoff
            extension_grant=attach_workbench_pairing(app,args.operator_key,args.extension_id[0],args.pairing_duration,
                until_stopped=args.pairing_until_stopped)
            handoffs.append((pairing_handoff(app,extension_grant,args.extension_id[0]),
                ['-ExpectedExtensionId',args.extension_id[0]]))
        if args.web_login:
            # Model preparation may take minutes; create the bounded code only after it succeeds.
            from server.workbench_web_login import attach_web_login,web_login_handoff
            web_grant=attach_web_login(app,args.operator_key,args.pairing_duration)
            handoffs.append((web_login_handoff(app,web_grant),['-WebLogin']))
        server=create_server(app)
        if app.candidate_review is not None or engine is not None or args.pair_local or args.web_login:server.daemon_threads=False
        if args.pairing_ui_shell is not None:
            from server.backup_scratch import reject_links
            import subprocess
            shell=Path(args.pairing_ui_shell)
            reject_links(shell)
            if not shell.is_absolute() or shell.name.lower()!='pwsh.exe' or not shell.is_file():
                raise AppError(400,'WORKBENCH_PAIRING_UI_SHELL_INVALID','临时码窗口需要明确本机PowerShell7')
            for pairing_file,ui_args in handoffs:
                subprocess.Popen([str(shell),'-NoProfile','-STA','-File',str(ROOT/'tools'/'show_identity_pairing.ps1'),
                    '-WorkbenchDataRoot',str(paths.root),'-PairingFile',str(pairing_file),*ui_args],
                    creationflags=subprocess.CREATE_NO_WINDOW,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            print('本机临时码窗口已请求打开；网页登录和扩展配对分别授权，真实页面仍待核验。')
        print('候选服务：http://127.0.0.1:8801；真实来源仍待验收。')
        print('网页退出仅退出登录。停止工作台请在此窗口按 Ctrl+C，等待程序正常结束后再关闭窗口。')
        try:server.serve_forever()
        except KeyboardInterrupt:pass
    finally:_finish_serving(app,server,worker,worker_session,engine)

if __name__=='__main__':
    try: main()
    except Exception as error:
        from server.common import AppError
        if isinstance(error,AppError): print(error.code+': '+error.message,file=sys.stderr)
        else: print('candidate_failed: 启动失败；不会回退旧云、填入或采集作业',file=sys.stderr)
        raise SystemExit(1)
