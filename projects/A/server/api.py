"""Finite routes and server-owned authorization. No send, fill, cloud or self-completion route."""
import json, uuid
from urllib.parse import parse_qs, urlsplit
from .common import AppError, canonical_json
from .dsh_app import principal_json
from .settings import VERSIONS

MAX_BODY = 512 * 1024
DIAGNOSTIC_FAILURE_CODES = {
    400:'API_BAD_REQUEST',401:'API_UNAUTHORIZED',403:'API_FORBIDDEN',
    409:'API_CONFLICT',413:'API_LIMIT',503:'API_UNAVAILABLE',500:'API_INTERNAL_ERROR',
}

def unique_object(pairs):
    result = {}
    for key,value in pairs:
        if key in result: raise AppError(400,'duplicate_json_key','JSON字段重复')
        result[key]=value
    return result

def decode_body(blob):
    if len(blob) > MAX_BODY: raise AppError(413,'request_too_large','请求超过大小限制')
    try:
        value=json.loads(blob.decode('utf-8'),object_pairs_hook=unique_object,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
    except (ValueError,UnicodeError): raise AppError(400,'invalid_json','JSON无效') from None
    if not isinstance(value,dict): raise AppError(400,'invalid_json','请求必须为JSON对象')
    return value

class API:
    def __init__(self, app): self.app=app
    def handle(self, method, target, headers, body=b''):
        request_id=uuid.uuid4().hex
        try:
            data,status = self.route(method,target,{k.lower():v for k,v in headers.items()},body)
            return status, self.envelope(request_id,'ok',data,[])
        except AppError as error:
            self.record_failure(target,error.status)
            return error.status,self.envelope(request_id,'error',None,[{'code':error.code,'message':error.message}])
        except Exception:
            self.record_failure(target,500)
            # No exception repr/body/credential crosses the boundary.
            return 500,self.envelope(request_id,'error',None,[{'code':'internal_error','message':'请求处理失败；已保留可重试状态'}])
    def record_failure(self,target,status):
        # A closed mapping is filled only with catalog codes; never exception text
        # or request data. Diagnostics failures cannot mask the original response.
        try:
            if urlsplit(target).path.startswith('/api/v1/diagnostics') or not self.app.writes_enabled or self.app.read_only or self.app.config['source_mode']!='fictional':return
            from .diagnostics import ERROR_CATALOG
            mapped=DIAGNOSTIC_FAILURE_CODES.get(status)
            if mapped in ERROR_CATALOG:self.app.diagnostics.record_code(mapped)
        except Exception:pass
    def envelope(self, rid, status, data, errors):
        return {'request_id':rid,'schema_version':VERSIONS['schema'],'status':status,
                'data_version':self.app.db.health()['data_version'],'coverage':{'state':'unverified','scope':'candidate'},'data':data,'errors':errors}
    def principal(self, headers, scope, account=None):
        bearer=headers.get('authorization','')
        if not bearer.startswith('Bearer ') or len(bearer)>1024:
            raise AppError(401,'authentication_required','请先完成启动认证')
        return self.app.auth.authenticate(bearer[7:],scope=scope,account_key=account)
    def write_guard(self, headers):
        if headers.get('x-dsh-schema') != '2': raise AppError(409,'version_mismatch','写入版本不兼容')
        if not self.app.writes_enabled:
            raise AppError(503,'source_not_activated','真实来源和静态保护尚未通过验收，敏感写入未启用')
    def route(self, method, target, headers, blob):
        parsed=urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise AppError(400,'invalid_target','请求路径无效')
        query=parse_qs(parsed.query,keep_blank_values=True)
        if set(query)&{'token','key','credential','authorization','access_token'}:
            raise AppError(400,'credential_in_url','凭据不得放入URL')
        if any(len(v)!=1 for v in query.values()): raise AppError(400,'duplicate_parameter','查询参数重复')
        path=parsed.path
        if method=='GET' and path=='/api/v1/health': return self.app.health(),200
        from .local_reply import LocalReplyAccess
        reply=getattr(self.app,'local_reply',None)
        finite_reply=isinstance(reply,LocalReplyAccess) and reply.app is self.app
        from .workbench_pairing import WorkbenchPairingGrant
        pairing=getattr(self.app,'workbench_pairing',None)
        finite_pairing=(isinstance(pairing,WorkbenchPairingGrant) and
            pairing._finite_notes is not None and pairing._finite_notes is self.app.local_notes and
            pairing._finite_notes.app is self.app and pairing._access_guard is not None)
        pairing_paths={'/api/v1/auth/pairing/'+name for name in ('status','redeem','challenge','session')}
        # Native reads use POST for structured input; each endpoint still
        # enforces account authorization and independently registered evidence.
        if (method=='POST' and getattr(self.app,'local_notes',None) is not None and path not in {
                '/api/v1/auth/startup','/api/v1/auth/web-login','/api/v1/auth/logout','/api/v1/profile/field',
                '/api/v1/native/profile','/api/v1/native/registration-status','/api/v1/native/runtime-resolve'} and
                not (finite_reply and path=='/api/v1/ai/request') and not (finite_pairing and path in pairing_paths)):
            raise AppError(503,'LOCAL_NOTES_ONLY','本次有限运行仅开放本机登录及备注，其他写入仍关闭。')
        if method=='POST':
            if headers.get('content-type','').split(';')[0].strip().lower()!='application/json':
                raise AppError(415,'json_required','请求必须为JSON')
            data=decode_body(blob)
        else: data={}
        workbench_pairing_routes={
            '/api/v1/auth/pairing/status':{'extension_id'},
            '/api/v1/auth/pairing/redeem':{'extension_id','pairing_code'},
            '/api/v1/auth/pairing/challenge':{'extension_id','client_challenge'},
            '/api/v1/auth/pairing/session':{'extension_id','client_challenge','server_challenge','proof'},
        }
        if path in workbench_pairing_routes:
            if (method!='POST' or query or len(blob)>8192 or headers.get('x-dsh-schema')!='2' or
                set(data)!=workbench_pairing_routes[path] or any(type(value)is not str for value in data.values())):
                raise AppError(400,'WORKBENCH_PAIRING_REQUEST_INVALID','配对只接受有限明确字段')
            extension_id=data['extension_id']
            if extension_id not in self.app.config['extension_ids'] or headers.get('origin')!='chrome-extension://'+extension_id:
                raise AppError(403,'WORKBENCH_PAIRING_EXTENSION_REQUIRED','配对只允许已登记扩展')
            from .workbench_pairing import WorkbenchPairingGrant
            grant=getattr(self.app,'workbench_pairing',None)
            if not isinstance(grant,WorkbenchPairingGrant):
                raise AppError(503,'WORKBENCH_PAIRING_UNAVAILABLE','本机正常配对未显式配置；保留现有登记')
            grant.require_app(self.app,extension_id)
            if path.endswith('/status'):return grant.status(),200
            if path.endswith('/challenge'):return grant.challenge(extension_id,data['client_challenge']),200
            if path.endswith('/session'):
                result=grant.resume(extension_id,data['client_challenge'],data['server_challenge'],data['proof'])
            else:result=grant.redeem(extension_id,data['pairing_code'])
            reply={'token':result['token'],'principal':principal_json(result['principal']),
                   'authority_digest':result['authority_digest']}
            if path.endswith('/redeem'):reply['device_credential']=result['device_credential']
            return reply,200
        if path=='/api/v1/auth/web-login':
            from .workbench_web_login import ORIGIN, WorkbenchWebLogin
            if (method!='POST' or query or len(blob)>1024 or headers.get('x-dsh-schema')!='2' or
                set(data)!={'code'} or type(data['code']) is not str):
                raise AppError(400,'WEB_LOGIN_REQUEST_INVALID','工作台登录只接受一次性登录码')
            if headers.get('origin')!=ORIGIN:
                raise AppError(403,'WEB_LOGIN_ORIGIN_INVALID','登录码仅供本机工作台网页使用')
            grant=getattr(self.app,'web_login',None)
            if not isinstance(grant,WorkbenchWebLogin):
                raise AppError(503,'WEB_LOGIN_UNAVAILABLE','本机工作台登录窗口尚未启用')
            grant.require_app(self.app)
            result=grant.redeem(data['code'])
            return {'token':result['token'],'principal':principal_json(result['principal']),
                    'expires_in':result['expires_in'],'expires_at':result['expires_at']},200
        if method=='POST' and path=='/api/v1/auth/startup':
            if set(data)-{'operator_key','credential','device_key','client_kind','extension_id'}:
                raise AppError(400,'unsupported_login_field','身份和权限由登记信息决定')
            required=('operator_key','credential','device_key')
            if any(not isinstance(data.get(x),str) for x in required): raise AppError(400,'invalid_login','启动认证信息不完整')
            if data['device_key']!=self.app.config['device_key']:raise AppError(403,'device_binding_mismatch','启动身份与此安装设备不一致')
            origin=headers.get('origin')
            kind=data.get('client_kind','workbench')
            if kind=='extension' and origin!='chrome-extension://'+str(data.get('extension_id','')):
                raise AppError(403,'extension_origin_mismatch','扩展身份和来源不一致')
            if kind=='workbench' and origin is not None and origin not in ('http://127.0.0.1:8801','http://localhost:8801'):
                raise AppError(403,'workbench_origin_mismatch','工作台来源不一致')
            result=self.app.auth.login(data['operator_key'],data['credential'],device_key=data['device_key'],
                                      client_kind=data.get('client_kind','workbench'),extension_id=data.get('extension_id'))
            public=dict(result); public['principal']=principal_json(result['principal'])
            return public,200
        if method=='POST' and path=='/api/v1/auth/logout':
            principal=self.principal(headers,'capabilities:read')
            self.app.auth.logout(headers['authorization'][7:])
            return {'revoked':True},200
        if method=='GET' and path in ('/api/v1/diagnostics/status','/api/v1/diagnostics/events'):
            allowed={'device_key'} if path.endswith('/status') else {'device_key','start_utc','end_utc','cursor','limit'}
            required={'device_key'} if path.endswith('/status') else {'device_key','start_utc','end_utc'}
            if set(query)-allowed or not required.issubset(query) or any(not query[key][0] or len(query[key][0])>4096 for key in query):
                raise AppError(400,'invalid_diagnostics_query','诊断查询只接受明确本机设备与有限UTC窗口')
            principal=self.principal(headers,'capabilities:read')
            principal.require('diagnostics:read' if principal.role=='manager' else 'diagnostics:self')
            if principal.role not in ('manager','baobao') or principal.device_key!=self.app.config['device_key'] or query['device_key'][0]!=principal.device_key:
                raise AppError(403,'DIAGNOSTICS_DEVICE_SCOPE','诊断只允许已登记本机设备范围')
            if path.endswith('/status'):return self.app.diagnostics.status(principal,device_key=query['device_key'][0]),200
            options={key:value[0] for key,value in query.items()}
            if 'limit' in options:
                if not options['limit'].isascii() or not options['limit'].isdigit() or str(int(options['limit']))!=options['limit'] or not 1<=int(options['limit'])<=200:raise AppError(400,'invalid_diagnostics_query','诊断分页数量须为1至200的整数')
                options['limit']=int(options['limit'])
            return self.app.diagnostics.query(principal,**options),200
        if method=='POST' and path=='/api/v1/diagnostics/policy':
            if query or set(data)!={'device_key','expected_revision','enabled','retention_days','maintenance_enabled','max_rows','max_bytes'}:raise AppError(400,'invalid_diagnostics_policy','策略必须明确本机设备、全部有限字段与版本')
            principal=self.principal(headers,'diagnostics:configure')
            if principal.role!='manager' or principal.device_key!=self.app.config['device_key'] or data['device_key']!=principal.device_key:raise AppError(403,'DIAGNOSTICS_DEVICE_SCOPE','诊断策略仅本机管理可配置')
            self.write_guard(headers)
            if self.app.read_only:raise AppError(503,'source_not_activated','只读回退不能配置诊断策略')
            return self.app.diagnostics.configure(principal,**{key:value for key,value in data.items() if key!='device_key'}),200
        if method=='POST' and path=='/api/v1/deletion/plan':
            if query or set(data)!={'account_key','target_kind','target_key'} or not isinstance(data.get('account_key'),str) or not data['account_key'] or len(data['account_key'])>200 or data.get('target_kind') not in ('message','observation') or not isinstance(data.get('target_key'),str) or not data['target_key'] or len(data['target_key'])>200:
                raise AppError(400,'INVALID_DELETION_PLAN','清单只接受明确账号、message/observation和已保存条目定位键')
            principal=self.principal(headers,'deletion:plan',data['account_key'])
            if principal.role!='manager':raise AppError(403,'DELETION_MANAGER_REQUIRED','必要删除范围清单仅允许本机管理查看')
            result=self.app.deletion_manifest.plan(data['account_key'],data['target_kind'],data['target_key'],principal)
            if len(canonical_json(result))>768*1024:raise AppError(413,'DELETION_PLAN_RESPONSE_LIMIT','完整清单响应超过预算；未返回截断清单')
            return result,200
        if method=='GET' and path=='/api/v1/capabilities':
            principal=self.principal(headers,'capabilities:read')
            return {'principal':principal_json(principal),'versions':dict(VERSIONS),
                    'history':True,'observations':self.app.writes_enabled,'actions':self.app.writes_enabled,
                    'real_dom':False,'two_machine_sync':False,'model':False,'native_send':'manual',
                    'profiles':bool(self.app.profiles),'history_cursor':'opaque' if self.app.history else 'account_object',
                    'source_state':'fictional_only' if self.app.writes_enabled else 'unverified',
                    'local_access':self.app.local_notes.capabilities(principal) if getattr(self.app,'local_notes',None) else None},200
        if path in ('/api/v1/import/binding-options','/api/v1/import/binding-inspect'):
            expected='GET' if path.endswith('binding-options') else 'POST'
            if method!=expected:raise AppError(404,'not_found','接口不存在')
            if method=='GET':
                if set(query)!={'account_key'}:raise AppError(400,'IMPORT_BINDING_INVALID','请选择明确账号')
                account=query['account_key'][0]
            else:
                if query or set(data)!={'binding'} or type(data['binding']) is not dict:raise AppError(400,'IMPORT_BINDING_INVALID','仅接受明确登记对象，不接受文件路径')
                account=data['binding'].get('account_key')
            if type(account) is not str or not account or len(account)>200:raise AppError(400,'IMPORT_BINDING_INVALID','请选择明确账号')
            principal=self.principal(headers,'identities:write',account)
            principal.require('observations:write',account)
            if principal.role!='manager' or principal.device_key!=self.app.config['device_key']:raise AppError(403,'IMPORT_LOCAL_DEVICE_REQUIRED','绑定核对仅允许已登记本机管理工作台')
            service=getattr(self.app,'file_import',None)
            if service is None:raise AppError(503,'feature_not_initialized','文件导入表尚未显式安装')
            from .import_binding import ImportBindingService
            binding_service=ImportBindingService(service)
            result=binding_service.options(account,principal) if method=='GET' else binding_service.inspect(data['binding'],principal)
            return result,200
        if path in ('/api/v1/import/candidate-context','/api/v1/import/candidate-plan'):
            expected='GET' if path.endswith('candidate-context') else 'POST'
            if method!=expected:raise AppError(404,'not_found','接口不存在')
            if method=='GET':
                if set(query)!={'account_key'}:raise AppError(400,'CANDIDATE_REVIEW_REQUEST_INVALID','请选择明确账号')
                account=query['account_key'][0]
            else:
                if query or set(data)!={'binding','review_key','page_index','page_count','unknown_records'} or type(data['binding']) is not dict:
                    raise AppError(400,'CANDIDATE_REVIEW_REQUEST_INVALID','核对请求不接受文件路径或额外字段')
                account=data['binding'].get('account_key')
            if type(account) is not str or not account or len(account)>200:raise AppError(400,'CANDIDATE_REVIEW_REQUEST_INVALID','请选择明确账号')
            principal=self.principal(headers,'identities:write',account);principal.require('observations:write',account)
            if principal.role!='manager' or principal.device_key!=self.app.config['device_key']:raise AppError(403,'IMPORT_LOCAL_DEVICE_REQUIRED','核对仅允许已登记本机管理工作台')
            entry=getattr(self.app,'candidate_review',None)
            if entry is None:return {'format':'dfa-candidate-review-context-1','configured':False,'account_key':account,'persisted':False,'real_write_enabled':False},200 if method=='GET' else 503
            def reauthorize(original):
                actual=self.principal(headers,'identities:write',account);actual.require('observations:write',account)
                return actual
            return (entry.context(account,principal) if method=='GET' else entry.plan(data,principal,reauthorize)),200
        import_routes={'/api/v1/import/context':'GET','/api/v1/import/plan':'POST','/api/v1/import/commit':'POST',
                       '/api/v1/import/observations':'GET','/api/v1/import/withdraw':'POST'}
        if path in import_routes and method==import_routes[path]:
            if method=='GET':
                allowed={'account_key'} if path.endswith('context') else {'account_key','batch_id','keyword','before','limit','person_key','source_id','source_version','account_namespace'}
                if set(query)-allowed:raise AppError(400,'invalid_import_query','导入查询字段无效')
                account=query.get('account_key',[None])[0]
            elif path.endswith(('plan','commit')):
                allowed={'preview','mapping'}|({'expected_plan_hash'} if path.endswith('commit') else set())
                if set(data)!=allowed or not isinstance(data.get('mapping'),dict):raise AppError(400,'invalid_import_request','仅接收预览JSON和显式映射，不接收文件路径')
                account=data['mapping'].get('account_key')
            else:
                if set(data)!={'account_key','batch_id','expected_scope_hash'} or not isinstance(data.get('expected_scope_hash'),str) or not data['expected_scope_hash']:raise AppError(400,'invalid_import_request','撤回需要明确账号、批次定位键和已展示范围版本')
                account=data['account_key']
            if not isinstance(account,str) or not account or len(account)>256:raise AppError(400,'invalid_import_account','请选择明确账号')
            if path.endswith('observations'):
                principal=self.principal(headers,'history:read',account)
                if principal.role not in ('manager','baobao'):raise AppError(403,'reader_required','文件原文查询仅允许已登记管理或饱饱身份')
            else:
                principal=self.principal(headers,'identities:write',account)
                principal.require('observations:write',account)
                if principal.role!='manager':raise AppError(403,'manager_required','文件导入仅允许本机管理工作台')
            service=getattr(self.app,'file_import',None)
            if not service:raise AppError(503,'feature_not_initialized','文件导入表尚未显式安装')
            if path.endswith('context'):
                with self.app.db.lock:
                    accounts=[dict(row) for row in self.app.db.conn.execute('SELECT account_key,platform_uid,verified FROM own_account WHERE account_key=?',(account,))]
                    aliases=[dict(row) for row in self.app.db.conn.execute('SELECT person_key,object_kind,identity_kind,identity_value,verified FROM identity_alias WHERE account_key=? ORDER BY person_key,identity_kind',(account,))]
                    conversations=[dict(row) for row in self.app.db.conn.execute('SELECT conversation_key,person_key,kind,platform_conversation_key FROM conversation WHERE account_key=? ORDER BY conversation_key',(account,))]
                    sources=[]
                    for row in self.app.db.conn.execute('SELECT source_id,source_version,source_kind,verified,capabilities_json FROM source ORDER BY source_id,source_version'):
                        caps=json.loads(row['capabilities_json'])
                        if isinstance(caps,dict) and 'account_keys' in caps and account not in caps['account_keys']:continue
                        sources.append({key:row[key] for key in ('source_id','source_version','source_kind','verified')})
                result={'account_key':account,'accounts':accounts,'sources':sources,'identities':aliases,'conversations':conversations,
                        'coverage':{'state':'unverified','scope':'registered_local_objects'},'writes_enabled':self.app.writes_enabled}
                notes=getattr(self.app,'local_notes',None)
                if notes is not None:
                    notes.check(principal)
                    if notes.account!=account:raise AppError(403,'account_denied','仅允许本次本地备注账号。')
                    result['pending_read_binding']=dict(notes.binding)
                return result,200
            if path.endswith('observations'):
                principal.require('history:read',account)
                options={key:values[0] for key,values in query.items() if key!='account_key'}
                selected={'person_key','source_id','source_version','account_namespace'}&set(options)
                notes=getattr(self.app,'local_notes',None)
                if notes is not None and not selected:raise AppError(400,'invalid_import_query','本次有限接入只允许按当前对象和批准来源查询。')
                if selected:
                    if selected!={'person_key','source_id','source_version','account_namespace'}:raise AppError(400,'invalid_import_query','按对象查询需要完整来源范围。')
                    options['binding']={'account_key':account,**{key:options.pop(key) for key in ('source_id','source_version','account_namespace')}}
                    if notes is not None:
                        notes.check(principal)
                        if options['binding']!=notes.binding:raise AppError(403,'account_denied','只能查询本次获准来源。')
                        options['preview_sha256']=notes.selection['selection']['preview_sha256']
                if 'limit' in options:
                    if not options['limit'].isdigit():raise AppError(400,'invalid_import_query','分页数量无效')
                    options['limit']=int(options['limit'])
                result=service.observations(account,principal,**options)
                # Pending exports contain history too. A principal checked before
                # the query cannot authorize a response after session revocation.
                if self.principal(headers,'history:read',account)!=principal:
                    raise AppError(403,'history_authorization_changed','历史读取期间权限已变化，请重新登录。')
                return result,200
            if path.endswith('plan'):return service.plan(data['preview'],data['mapping'],principal),200
            self.write_guard(headers)
            if path.endswith('commit'):return service.commit(data['preview'],data['mapping'],data['expected_plan_hash'],principal),200
            # Batch is only a locator; withdrawal is for its entire source origin.
            return service.withdraw(data['batch_id'],principal,account_key=account,expected_scope_hash=data['expected_scope_hash']),200
        if method=='POST' and path in ('/api/v1/observations','/api/v1/actions'):
            scope='observations:write' if path.endswith('observations') else 'actions:write'
            principal=self.principal(headers,scope,data.get('account_key'))
            self.write_guard(headers)
            if any(x in data for x in ('actor','actor_key','operator_key','manual','done','completed','role','scopes')):
                raise AppError(400,'untrusted_fact_claim','请求不能自报操作者或完成状态')
            source=self.app.identities.get_source(data.get('source_id'),data.get('source_version','1'))
            if not source or source.get('source_kind')!='fictional':
                raise AppError(503,'source_not_activated','当前只接收隔离虚构来源')
            with self.app.db.transaction():
                if scope=='observations:write':
                    result=self.app.messages.ingest(data,principal=principal)
                    if result.get('message_key'):
                        message=self.app.messages.get_message(result['message_key'])
                        if message['direction']=='out' and message['success_state']=='confirmed':self.app.actions.link_success(result['message_key'])
                        if self.app.history:self.app.history.update(result['message_key'])
                else:
                    result=self.app.actions.record_action(data,principal)
                    rows=self.app.db.conn.execute("SELECT message_key FROM message WHERE account_key=? AND conversation_key=? AND platform_correlation_key=? AND direction='out' AND success_state='confirmed'",(data['account_key'],data['conversation_key'],data.get('platform_correlation_key'))).fetchall()
                    for row in rows:self.app.actions.link_success(row[0])
            result=dict(result); result['committed']=result.get('status') in ('accepted','duplicate')
            return result,200
        if method=='GET' and path=='/api/v1/history':
            if set(query)-{'account_key','person_key','conversation_key','keyword','day','cursor','limit'}:
                raise AppError(400,'unsupported_parameter','查询包含不支持字段')
            account=query.get('account_key',[None])[0]
            if not account: raise AppError(400,'account_required','查询需要明确账号')
            principal=self.principal(headers,'history:read',account)
            try:
                limit=int(query.get('limit',['50'])[0]); before=json.loads(query['cursor'][0]) if 'cursor' in query and not self.app.history else None
                if 'cursor' in query and not self.app.history and type(before) is not dict:raise ValueError()
            except (ValueError,TypeError): raise AppError(400,'invalid_cursor','分页游标或数量无效') from None
            if self.app.history:
                result=self.app.history.search(account,query.get('person_key',[None])[0],conversation_key=query.get('conversation_key',[None])[0],
                       keyword=query.get('keyword',[None])[0],day=query.get('day',[None])[0],cursor=query.get('cursor',[None])[0],limit=limit)
            else:
                result=self.app.messages.list_messages(account,conversation_key=query.get('conversation_key',[None])[0],
                          person_key=query.get('person_key',[None])[0],keyword=query.get('keyword',[None])[0],day=query.get('day',[None])[0],before=before,limit=limit)
            # Query snapshots do not lock authentication. Discard the rows and
            # cursor if logout, expiry or enrollment changes during the read.
            if self.principal(headers,'history:read',account)!=principal:
                raise AppError(403,'history_authorization_changed','历史读取期间权限已变化，请重新登录。')
            return result,200
        if method=='POST' and path=='/api/v1/native/runtime-resolve':
            if query or len(blob)>2048:raise AppError(400,'NATIVE_RUNTIME_INVALID','身份运行核对请求无效')
            if headers.get('x-dsh-schema')!='2':raise AppError(409,'version_mismatch','身份运行核对版本不兼容')
            principal=self.principal(headers,'sidebar:read')
            from .native_runtime import resolve_runtime
            def reauthorize(original,account):return self.principal(headers,'sidebar:read',account)
            return resolve_runtime(self.app,data,principal,reauthorize),200
        if method=='POST' and path=='/api/v1/native/registration-status':
            if query or len(blob)>2048:raise AppError(400,'NATIVE_REGISTRATION_STATUS_INVALID','登记状态请求无效')
            if headers.get('x-dsh-schema')!='2':raise AppError(409,'version_mismatch','登记状态版本不兼容')
            principal=self.principal(headers,'sidebar:read')
            from .native_registration import NativeProfileRegistration
            def reauthorize(original,account):return self.principal(headers,'sidebar:read',account)
            return NativeProfileRegistration(self.app).status(data,principal,reauthorize),200
        if method=='POST' and path=='/api/v1/native/profile':
            if query:raise AppError(400,'NATIVE_PROFILE_REQUEST_INVALID','资料核对不接受查询参数')
            if headers.get('x-dsh-schema')!='2':raise AppError(409,'version_mismatch','资料核对版本不兼容')
            principal=self.principal(headers,'sidebar:read')
            from .native_profile import NativeProfileLookup
            def reauthorize(original,account):return self.principal(headers,'sidebar:read',account)
            return NativeProfileLookup(self.app).lookup(data,principal,reauthorize),200
        if method=='GET' and path=='/api/v1/sidebar':
            account=query.get('account_key',[None])[0]; person=query.get('person_key',[None])[0]
            self.principal(headers,'sidebar:read',account)
            if not account or not person: raise AppError(400,'identity_required','请先核验账号和个人身份')
            if self.app.projections:return self.app.projections.sidebar(account,person),200
            return {'account_key':account,'person_key':person,'identity_state':'unverified',
                    'grade':{'state':'unknown','value':None},'reply':{'state':'unknown'},
                    'maintenance':{'state':'unverified'},'spark':{'state':'unverified'},
                    'topics':{'state':'not_enabled','items':[]}},200
        if method=='GET' and path=='/api/v1/contact-statistics':
            if set(query)-{'account_key','start_day','end_day','operator_key'} or any(len(v)!=1 or not v[0] for v in query.values()):raise AppError(400,'unsupported_parameter','联系统计查询包含不支持字段或重复值')
            account=query.get('account_key',[None])[0]
            if not account:raise AppError(400,'account_required','联系统计需要明确账号')
            principal=self.principal(headers,'history:read',account)
            if not all(k in query for k in ('start_day','end_day')):raise AppError(400,'invalid_contact_window','请给出联系窗口的起止日期')
            result=self.app.contact_stats.read(account,principal,start_day=query['start_day'][0],end_day=query['end_day'][0],operator_key=query.get('operator_key',[None])[0])
            if self.principal(headers,'history:read',account)!=principal:raise AppError(403,'forbidden','登录状态已变化')
            return result,200
        if method=='GET' and path=='/api/v1/sync/status':
            if set(query)!={'account_key'}:raise AppError(400,'account_required','同步状态需要唯一账号范围')
            account=query['account_key'][0];self.principal(headers,'capabilities:read',account)
            baseline=dict(self.app.sync.readiness());baseline['account_key']=account
            with self.app.db.lock:
                ready=self.app.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='sync_received'").fetchone()
                baseline['local_outbox']={r[0]:r[1] for r in self.app.db.conn.execute('SELECT o.state,count(*) FROM outbox o JOIN domain_event e USING(event_id) WHERE e.account_key=? GROUP BY o.state',(account,))}
                baseline['received_events']={r[0]:r[1] for r in self.app.db.conn.execute('SELECT state,count(*) FROM sync_received WHERE account_key=? GROUP BY state',(account,))} if ready and 'account_key' in {r[1] for r in self.app.db.conn.execute('PRAGMA table_info(sync_received)')} else {}
                if ready and not baseline['received_events']:
                    baseline['received_events']={r[0]:r[1] for r in self.app.db.conn.execute("SELECT state,count(*) FROM sync_received WHERE json_extract(event_json,'$.account_key')=? GROUP BY state",(account,))}
            return baseline,200
        if method=='GET' and path in ('/api/v1/contacts','/api/v1/overview'):
            account=query.get('account_key',[None])[0]
            if not account:raise AppError(400,'account_required','查询需要明确账号')
            self.principal(headers,'sidebar:read',account)
            if not self.app.projections:raise AppError(503,'feature_not_initialized','请先对候选库显式迁移')
            return (self.app.profiles.contacts(account) if path.endswith('contacts') else self.app.projections.overview(account)),200
        if method=='GET' and path in ('/api/v1/profile/birthday-candidates','/api/v1/profile/birthday-source'):
            allowed={'account_key','person_key','cursor'} if path.endswith('candidates') else {'account_key','person_key','message_key','candidate_key'}
            if set(query)-allowed or not {'account_key','person_key'}<=set(query) or any(not v[0] or len(v[0])>4096 for v in query.values()):raise AppError(400,'invalid_birthday_query','生日候选查询范围无效')
            account,person=query['account_key'][0],query['person_key'][0]
            principal=self.principal(headers,'history:read',account)
            from .birthday_candidates import BirthdayCandidates
            service=BirthdayCandidates(self.app)
            if path.endswith('candidates'):result=service.read(account,person,query.get('cursor',[None])[0])
            else:
                if set(query)!=allowed:raise AppError(400,'invalid_birthday_query','候选原文需要明确修订依据')
                result=service.inspect(account,person,query['message_key'][0],query['candidate_key'][0])
            if self.principal(headers,'history:read',account)!=principal:raise AppError(403,'forbidden','登录状态已变化')
            return result,200
        if method=='GET' and path=='/api/v1/profile/editor':
            if set(query)!={'account_key','person_key'} or any(len(query[k])!=1 or not query[k][0] for k in query):
                raise AppError(400,'invalid_profile_request','资料编辑需要唯一账号和对象')
            account,person=query['account_key'][0],query['person_key'][0]
            principal=self.principal(headers,'sidebar:read',account)
            from .profile_editor import snapshot
            result=snapshot(self.app,account,person,principal)
            if self.principal(headers,'sidebar:read',account)!=principal:
                raise AppError(403,'forbidden','登录状态已变化')
            return result,200
        if method=='POST' and path=='/api/v1/profile/field':
            if query or set(data)!={'account_key','person_key','field','value','expected_revision','clear'}:
                raise AppError(400,'invalid_profile_request','资料修改需要明确字段和版本')
            principal=self.principal(headers,'profile:write',data['account_key'])
            if headers.get('x-dsh-schema')!='2':raise AppError(409,'version_mismatch','写入版本不兼容')
            if getattr(self.app,'local_notes',None) is None:self.write_guard(headers)
            if not self.app.profiles:raise AppError(503,'feature_not_initialized','资料模块尚未显式初始化')
            if type(data['clear'])is not bool:raise AppError(400,'invalid_clear','清空状态无效')
            if getattr(self.app,'local_notes',None) is not None:
                return self.app.local_notes.save(headers['authorization'][7:],principal,data),200
            actor={'operator_key':principal.operator_key,'device_key':principal.device_key,'session_id':principal.session_id}
            result=self.app.profiles.set_field(data['account_key'],data['person_key'],data['field'],data['value'],expected_revision=data['expected_revision'],actor=actor,clear=data['clear'])
            return result,200
        if method=='GET' and path=='/api/v1/groups/contacts':
            if set(query)!={'account_key'} or len(query['account_key'])!=1 or not query['account_key'][0]:
                raise AppError(400,'invalid_group_query','分组浏览需要唯一账号')
            account=query['account_key'][0]
            self.principal(headers,'sidebar:read',account)
            if not self.app.projections:raise AppError(503,'feature_not_initialized','分组尚未显式初始化')
            result=self.app.projections.group_directory(account)
            self.principal(headers,'sidebar:read',account)
            return result,200
        if method=='GET' and path=='/api/v1/groups':
            account=query.get('account_key',[None])[0]
            if not account:raise AppError(400,'account_required','分组查询需要明确账号')
            self.principal(headers,'sidebar:read',account)
            if not self.app.projections:raise AppError(503,'feature_not_initialized','分组尚未显式初始化')
            return self.app.projections.groups.definitions(account),200
        if method=='POST' and path in ('/api/v1/groups/create','/api/v1/groups/update','/api/v1/groups/membership'):
            account=data.get('account_key');principal=self.principal(headers,'profile:write',account);self.write_guard(headers)
            if not account or not self.app.projections:raise AppError(400,'group_context_missing','分组修改需要明确账号')
            actor={'operator_key':principal.operator_key,'device_key':principal.device_key,'session_id':principal.session_id};service=self.app.projections.groups
            if path.endswith('create'):
                if set(data)!={'account_key','name'}:raise AppError(400,'invalid_group_request','分组创建字段无效')
                return service.create(account,data['name'],actor=actor),200
            if path.endswith('update'):
                if set(data)!={'account_key','group_id','name','deleted','expected_revision'} or type(data['deleted'])is not bool:raise AppError(400,'invalid_group_request','分组修改字段无效')
                return service.update(account,data['group_id'],name=data['name'],deleted=data['deleted'],expected_revision=data['expected_revision'],actor=actor),200
            if set(data)!={'account_key','person_key','group_id','member','expected_revision'}:raise AppError(400,'invalid_group_request','分组成员字段无效')
            return service.set_membership(account,data['person_key'],data['group_id'],data['member'],expected_revision=data['expected_revision'],actor=actor),200
        if method=='POST' and path in ('/api/v1/opportunity/cancel','/api/v1/opportunity/display','/api/v1/opportunity/time'):
            expected={'account_key','person_key','expected_revision',('time' if path.endswith('time') else 'day')}
            if set(data)!=expected:raise AppError(400,'invalid_round_request','机会修改需要明确对象、日期及版本')
            principal=self.principal(headers,'profile:write',data['account_key']);self.write_guard(headers)
            if not self.app.projections:raise AppError(503,'feature_not_initialized','机会模块尚未显式初始化')
            actor={'operator_key':principal.operator_key,'device_key':principal.device_key,'session_id':principal.session_id}
            function=self.app.projections.set_opportunity_time if path.endswith('time') else self.app.projections.cancel_opportunity if path.endswith('cancel') else self.app.projections.display_opportunity
            result=function(data['account_key'],data['person_key'],data.get('time',data.get('day')),expected_revision=data['expected_revision'],actor=actor)
            return result,200
        if method=='GET' and path in ('/api/v1/activity/devices','/api/v1/activity/timeline'):
            principal=self.principal(headers,'capabilities:read')
            if principal.role not in ('manager','baobao'):raise AppError(403,'ACTIVITY_READ_FORBIDDEN','活动查询需要已登记管理或饱饱身份')
            principal.require('activities:read' if principal.role=='manager' else 'activities:self')
            service=getattr(self.app,'activity_read',None)
            if service is None:raise AppError(503,'ACTIVITY_NOT_INITIALIZED','活动模式尚未显式初始化；未启动采集')
            if path.endswith('devices'):
                if query:raise AppError(400,'INVALID_ACTIVITY_QUERY','设备列表不接受额外字段')
                return service.devices(principal),200
            required={'device_key','start_utc','end_utc'}
            if not required<=set(query) or set(query)-required-{'tau'}:raise AppError(400,'INVALID_ACTIVITY_QUERY','仅接受设备、UTC起止及30/60/90秒候选')
            tau=query.get('tau',['60'])[0]
            if tau not in ('30','60','90'):raise AppError(400,'INVALID_ACTIVITY_TAU','仅允许30/60/90秒候选')
            return service.timeline(principal,device_key=query['device_key'][0],start_utc=query['start_utc'][0],end_utc=query['end_utc'][0],tau=int(tau)),200
        if method=='GET' and path=='/api/v1/activity/policy':
            if query:raise AppError(400,'unsupported_parameter','留存策略查询不接受额外字段')
            self.principal(headers,'activities:read')
            service=getattr(self.app,'activity_retention',None)
            if service is None:raise AppError(503,'ACTIVITY_RETENTION_NOT_INITIALIZED','活动留存尚未显式初始化')
            return {'policy':service.policy(),'task_installed':False,'real_source_activated':False},200
        if method=='GET' and path=='/api/v1/activity/archives':
            if set(query)-{'device_key','start_day','end_day','tau'} or not {'device_key','start_day','end_day'}<=set(query):raise AppError(400,'invalid_activity_request','活动归档需要明确设备和日期窗口')
            principal=self.principal(headers,'capabilities:read')
            service=getattr(self.app,'activity_retention',None)
            if service is None:raise AppError(503,'ACTIVITY_RETENTION_NOT_INITIALIZED','活动留存尚未显式初始化')
            try:tau=int(query.get('tau',['60'])[0])
            except ValueError:raise AppError(400,'invalid_activity_request','仅允许30/60/90秒候选') from None
            return {'items':service.archives(principal,device_key=query['device_key'][0],start_day=query['start_day'][0],end_day=query['end_day'][0],tau=tau),'coverage':'observed_only'},200
        if method=='POST' and path in ('/api/v1/activity/source/approve','/api/v1/activity/retention/configure','/api/v1/activity/retention/maintain'):
            scope='activity_sources:approve' if path.endswith('/approve') else 'activities:retain'
            principal=self.principal(headers,scope);self.write_guard(headers)
            if principal.role!='manager':raise AppError(403,'MANAGER_REQUIRED','活动来源和留存维护仅限管理端')
            if path.endswith('/approve'):
                approval=data.get('approval')
                if set(data)!={'approval'} or not isinstance(approval,dict) or approval.get('source_kind')!='fictional':raise AppError(403,'ACTIVITY_REAL_SOURCE_DISABLED','本轮仅登记隔离虚构来源；真实活动来源不由配置激活')
                adapter=getattr(self.app,'activity_adapter',None)
                if adapter is None:raise AppError(503,'ACTIVITY_ADAPTER_NOT_INITIALIZED','活动来源模式尚未显式初始化')
                return adapter.approvals.approve(approval,principal),200
            service=getattr(self.app,'activity_retention',None)
            if service is None:raise AppError(503,'ACTIVITY_RETENTION_NOT_INITIALIZED','活动留存尚未显式初始化')
            if path.endswith('/configure'):
                if set(data)!={'policy'}:raise AppError(400,'invalid_activity_request','仅允许有限留存策略')
                return service.configure(data['policy'],principal),200
            if set(data)!={'limit'}:raise AppError(400,'invalid_activity_request','维护仅允许有限批量；时刻由服务端确定')
            from datetime import datetime,timezone
            return service.maintain(principal,now=datetime.now(timezone.utc),limit=data['limit']),200
        if path.startswith('/api/v1/ai/'):
            reads={'/api/v1/ai/status','/api/v1/ai/job'}
            writes={'/api/v1/ai/request','/api/v1/ai/control','/api/v1/ai/preference','/api/v1/ai/process-next','/api/v1/ai/retry'}
            if (method=='GET' and path not in reads) or (method=='POST' and path not in writes) or method not in ('GET','POST'):
                raise AppError(404,'route_not_found','接口不存在')
            account=query.get('account_key',[None])[0] if method=='GET' else data.get('account_key')
            if not isinstance(account,str) or not account:raise AppError(400,'account_required','模型参考需要明确账号')
            scope='model:read' if method=='GET' else 'model:correct' if path.endswith(('control','preference')) else 'model:request'
            principal=self.principal(headers,scope,account)
            if finite_reply:
                if account!=reply.account:raise AppError(403,'account_denied','仅允许本次本地回复账号。')
                token=headers['authorization'][7:]
                if method=='GET' and path.endswith('status'):
                    if set(query)-{'account_key','person_key'}:raise AppError(400,'invalid_ai_request','模型查询字段无效')
                    return reply.status(token,principal,query.get('person_key',[None])[0]),200
                if method=='GET':
                    if set(query)!={'account_key','job_key'}:raise AppError(400,'invalid_ai_request','参考查询需要任务编号')
                    return reply.read(token,principal,query['job_key'][0]),200
                if path!='/api/v1/ai/request':raise AppError(503,'LOCAL_REPLY_ONLY','本次只开放主动短回复参考。')
                if headers.get('x-dsh-schema')!='2':raise AppError(409,'version_mismatch','写入版本不兼容')
                if query or set(data)!={'account_key','person_key','purpose','on_demand'}:
                    raise AppError(400,'invalid_ai_request','参考请求字段无效')
                if any(type(data[key])is not str or not data[key] for key in ('account_key','person_key','purpose')):
                    raise AppError(400,'invalid_ai_request','参考请求需要明确账号、对象和用途')
                return reply.request(token,principal,data),200
            if method=='POST':self.write_guard(headers)
            if method=='GET' and getattr(self.app,'local_notes',None) is not None:
                if account!=self.app.local_notes.account:raise AppError(403,'account_denied','仅允许本次本地备注账号。')
                if path.endswith('status'):
                    if set(query)-{'account_key','person_key'}:raise AppError(400,'invalid_ai_request','模型查询字段无效')
                    # No queue, cached result, person evidence or model IO. The
                    # internal reply policy is not a deployed Web capability.
                    return {'available':False,'request_enabled':False,'processing':'local_notes_only',
                            'reason':'reliable_chat_context_unavailable','preferences':{},
                            'scheduler':'not_enabled','real_quality':'NOT_RUN'},200
                raise AppError(503,'LOCAL_REPLY_NOT_CONNECTED','可靠聊天上下文尚未接通，回复参考未启用。')
            service=getattr(self.app,'ai',None)
            if service is None:raise AppError(503,'AI_MIGRATION_REQUIRED','模型候选表尚未显式迁移；未启动模型')
            if method=='GET' and path.endswith('status'):
                if set(query)-{'account_key','person_key'}:raise AppError(400,'invalid_ai_request','模型查询字段无效')
                result=service.status(account)
                if 'person_key' in query:result['preferences']=service.preferences(account,query['person_key'][0],principal)
                return result,200
            if method=='GET':
                if set(query)!={'account_key','job_key'}:raise AppError(400,'invalid_ai_request','参考查询需要任务编号')
                job=query['job_key'][0]
                if service.store.raw_job(job)['account_key']!=account:raise AppError(403,'account_denied','参考任务不属于此账号')
                return service.get(job,principal),200
            if path.endswith('request'):
                if set(data)!={'account_key','person_key','purpose','on_demand'}:raise AppError(400,'invalid_ai_request','参考请求字段无效')
                return service.request(account,data['person_key'],data['purpose'],principal,on_demand=data['on_demand']),200
            if path.endswith('preference'):
                if set(data)!={'account_key','person_key','purpose','enabled','expected_revision'}:raise AppError(400,'invalid_ai_request','参考开关字段无效')
                return service.set_enabled(account,data['person_key'],data['purpose'],data['enabled'],data['expected_revision'],principal),200
            if path.endswith('control'):
                if set(data)!={'account_key','job_key','operation','value','expected_revision'}:raise AppError(400,'invalid_ai_request','参考控制字段无效')
                if service.store.raw_job(data['job_key'])['account_key']!=account:raise AppError(403,'account_denied','参考任务不属于此账号')
                return service.control(data['job_key'],data['operation'],data['value'],data['expected_revision'],principal),200
            if path.endswith('retry'):
                if set(data)!={'account_key','job_key','expected_generation','expected_revision'}:raise AppError(400,'invalid_ai_request','重试需要原任务和精确执行版本')
                if not isinstance(data['job_key'],str):raise AppError(400,'invalid_ai_request','重试任务编号无效')
                if service.store.raw_job(data['job_key'])['account_key']!=account:raise AppError(403,'account_denied','参考任务不属于此账号')
                return service.retry(data['job_key'],data['expected_generation'],data['expected_revision'],principal),200
            if set(data)!={'account_key'}:raise AppError(400,'invalid_ai_request','测试处理仅接受明确账号')
            principal.require('identities:write',account)  # Excludes reduced extension sessions even for manager operators.
            if principal.role!='manager':raise AppError(403,'manager_required','显式候选处理仅允许本机管理测试')
            return service.process_next(account_key=account),200
        if method=='POST' and path in ('/api/v1/backups/create','/api/v1/backups/verify'):
            scope='backup:create' if path.endswith('create') else 'backup:verify'
            self.principal(headers,scope)
            if headers.get('x-dsh-schema')!='2': raise AppError(409,'version_mismatch','备份接口版本不兼容')
            if path.endswith('create') and data: raise AppError(400,'unsupported_backup_field','备份位置由本机登记决定')
            from .backup import BackupService
            service=BackupService(self.app.paths.db,self.app.paths.backup,key=self.app.keys.load())
            if path.endswith('create'): return service.create(),200
            filename=data.get('filename')
            import re
            if set(data)!={'filename'} or not isinstance(filename,str) or not re.fullmatch('[0-9a-f]{32}\\.dfab',filename):
                raise AppError(400,'invalid_backup_filename','只允许本机备份文件编号')
            return service.verify(filename),200
        # No old production cloud/demo/train/fill/manual-completion route is reachable.
        raise AppError(404,'route_not_found','接口不存在')
