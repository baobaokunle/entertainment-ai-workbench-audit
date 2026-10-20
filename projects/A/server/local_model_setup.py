"""Explicit fixed local-model binding; no download, startup, default read or worker."""
import hashlib,json,re,urllib.request
from pathlib import Path
from .common import AppError,canonical_json
from .backup_scratch import reject_links
from .local_ai import LocalModelClient,ModelRegistration,NoRedirect,OLLAMA_TAG
from .pinned_qwen_tokenizer import PinnedQwenTokenizer,MODEL_DIGEST,TOKENIZER_DIGEST,OFFICIAL_COMMIT

MANIFEST_DIGEST='0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0'
MODEL_BYTES=2497280480

def asset_error():raise AppError(503,'AI_MODEL_ASSETS_UNVERIFIED','固定公开模型资产未核验；不下载、启动或回退其他模型')
def runtime_error():raise AppError(503,'AI_MODEL_RUNTIME_UNVERIFIED','固定本机模型服务未核验；不启动、下载或使用其他端口')

def verify_asset_file(path,expected_digest,expected_size=None,limit=None):
    reject_links(path)
    if not path.is_file():asset_error()
    size=path.stat().st_size
    if expected_size is not None and size!=expected_size or limit is not None and size>limit:asset_error()
    hashed=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''):hashed.update(chunk)
    if hashed.hexdigest()!=expected_digest:asset_error()

def runtime_metadata(route,body=None):
    if route not in ('version','tags','show'):runtime_error()
    url='http://127.0.0.1:11434/api/'+route
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    request=urllib.request.Request(url,data=canonical_json(body) if body is not None else None,headers={'Content-Type':'application/json'})
    try:
        with opener.open(request,timeout=20) as response:
            if response.geturl()!=url:runtime_error()
            raw=response.read(65537)
        if len(raw)>65536:runtime_error()
        value=json.loads(raw)
        if not isinstance(value,dict):runtime_error()
        return value
    except AppError:raise
    except (OSError,ValueError,TypeError):runtime_error()

def prepare_client(asset_root):
    root=Path(asset_root)
    if not root.is_absolute():asset_error()
    reject_links(root);root=root.resolve()
    if not root.is_dir():asset_error()
    blob=root/'models/blobs'/('sha256-'+MODEL_DIGEST.removeprefix('sha256:'))
    manifest=root/'models/manifests/registry.ollama.ai/library/qwen3/4b-instruct-2507-q4_K_M'
    try:
        verify_asset_file(blob,MODEL_DIGEST.removeprefix('sha256:'),expected_size=MODEL_BYTES)
        verify_asset_file(manifest,MANIFEST_DIGEST,limit=65536)
        tokenizer=PinnedQwenTokenizer(root/'tokenizer_assets')
        version=runtime_metadata('version');tags=runtime_metadata('tags');shown=runtime_metadata('show',{'model':OLLAMA_TAG})
        if version.get('version')!='0.40.3' or not isinstance(tags.get('models'),list):runtime_error()
        if not any(isinstance(row,dict) and row.get('name')==OLLAMA_TAG and row.get('digest')==MANIFEST_DIGEST for row in tags['models']):runtime_error()
        details=shown.get('details');modelfile=shown.get('modelfile')
        if not isinstance(details,dict) or details.get('family')!='qwen3' or details.get('quantization_level')!='Q4_K_M' or not isinstance(modelfile,str):runtime_error()
        origins=re.findall(r'^FROM[ \t]+([^\r\n]+)',modelfile,re.MULTILINE)
        if len(origins)!=1:runtime_error()
        source=origins[0].strip()
        if len(source)>=2 and source[0]==source[-1]=='"':source=source[1:-1]
        source=Path(source)
        if not source.is_absolute() or source.resolve()!=blob.resolve():runtime_error()
    except AppError:raise
    except (OSError,ValueError,TypeError):asset_error()
    registration=ModelRegistration(MODEL_DIGEST,'Apache-2.0','official LICENSE SHA832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e',TOKENIZER_DIGEST,'official '+OFFICIAL_COMMIT+'; pinned tokenizers0.22.2 and current full renderer',True)
    client=LocalModelClient(registration,tokenizer=tokenizer)
    if not client.status()['available']:asset_error()
    return client

def bind_explicit_local_model(app,asset_root):
    if app.read_only or not app.writes_enabled or app.config.get('source_mode')!='fictional' or app.config.get('device_role')!='manager':
        raise AppError(403,'AI_MODEL_EXPLICIT_FIXTURE_ONLY','固定模型接线仅允许明确虚构管理工作区；不启用真实资料或只读写入')
    if app.ai is None:raise AppError(503,'AI_MIGRATION_REQUIRED','已有AI schema尚未初始化；不自动迁移')
    client=prepare_client(asset_root)
    # Keep all Application/SyncRuntime handlers bound to the same existing service.
    app.ai.client=client;app.ai.approved_model_digests.add(MODEL_DIGEST)
    return {'registered':True,'model_digest':MODEL_DIGEST,'tokenizer_digest':TOKENIZER_DIGEST,
            'runtime_check':'matched_at_binding_only','worker_started':False,'real_quality':'NOT_RUN'}


def bind_local_reply_model(app,asset_root,engine):
    """Only the current finite policy and its newly owned, protected engine."""
    from .local_reply import LocalReplyAccess
    from .local_model_runtime import OwnedLocalModel
    policy=getattr(app,'local_reply',None)
    if (not isinstance(policy,LocalReplyAccess) or policy.app is not app or
            policy.service is not app.ai or app.ai.execution_policy is not policy or
            not policy.host_active() or not isinstance(engine,OwnedLocalModel) or
            engine.assets!=Path(asset_root) or engine.data!=app.paths.root or engine.protection_shell is None):
        raise AppError(403,'LOCAL_REPLY_OWNED_ENGINE_REQUIRED','有限回复需要本次受保护的固定模型及原管理会话')
    engine.require_private_runtime()
    client=prepare_client(asset_root)
    engine.require_private_runtime()
    engine.guard_client(client);policy.bind_engine(engine)
    app.ai.client=client;app.ai.approved_model_digests.add(MODEL_DIGEST)
    return {'registered':True,'runtime_check':'current_owned_protected_engine','worker_started':False,'real_quality':'NOT_RUN'}
