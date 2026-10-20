"""Explicit owned engine lifecycle; finite reply adds private-profile checks."""
import hashlib,json,os,secrets,time,urllib.request
from pathlib import Path
from .backup_scratch import reject_links,require_efs
from .common import AppError
from .local_model_engine import verify_engine_directory,ENGINE_MANIFEST,ENGINE_MANIFEST_SHA256
from .local_model_process import WindowsOwnedProcess,process_error
from .local_model_setup import MODEL_DIGEST,MODEL_BYTES,MANIFEST_DIGEST,verify_asset_file
from .pinned_qwen_tokenizer import PinnedQwenTokenizer
from .local_ai import NoRedirect

class OwnedLocalModel:
    def __init__(self,asset_root,data_root,*,backend=None,clock=time.monotonic,sleep=time.sleep,protection_shell=None):
        self.assets=Path(asset_root);self.data=Path(data_root);self.backend=backend or WindowsOwnedProcess()
        self.clock=clock;self.sleep=sleep;self.started=False;self.closed=False;self.profile=None;self.last_stop=None
        self.protection_shell=protection_shell;self.protection_failed=False;self.directory_ids={}
        if protection_shell is not None:
            from .local_workspace_preparation import _shell_path
            self.protection_shell=_shell_path(protection_shell)
    def start(self):
        if self.started or self.closed:process_error('AI_ENGINE_ALREADY_STARTED')
        for root in (self.assets,self.data):
            if not root.is_absolute() or root.drive.upper()!='F:':process_error('AI_ENGINE_F_ROOT_REQUIRED')
            reject_links(root)
            if not root.is_dir():process_error()
        self.backend.require_console()
        if self.backend.listeners():process_error('AI_ENGINE_PORT_OCCUPIED')
        self.backend.lease(ENGINE_MANIFEST)
        cpu=self.assets/'cpu-runtime-v0403';verified=verify_engine_directory(cpu)
        raw=ENGINE_MANIFEST.read_bytes()
        if len(raw)>65536 or hashlib.sha256(raw).hexdigest()!=ENGINE_MANIFEST_SHA256:process_error()
        manifest=json.loads(raw)
        for directory in (cpu,cpu/'lib',cpu/'lib/ollama'):self.backend.lease(directory,directory=True)
        for row in manifest['files']:self.backend.lease(cpu/row['file'])
        # Recheck under non-inherited read leases before native loading.
        verify_engine_directory(cpu)
        model_manifest=self.assets/'models/manifests/registry.ollama.ai/library/qwen3/4b-instruct-2507-q4_K_M'
        blob=self.assets/'models/blobs'/('sha256-'+MODEL_DIGEST.removeprefix('sha256:'))
        self.backend.lease(model_manifest);self.backend.lease(blob)
        verify_asset_file(model_manifest,MANIFEST_DIGEST,limit=65536);verify_asset_file(blob,MODEL_DIGEST.removeprefix('sha256:'),expected_size=MODEL_BYTES)
        PinnedQwenTokenizer(self.assets/'tokenizer_assets')
        # This is a fresh EFS profile, never an old Windows or model profile.
        parent=self.data/'runtime/model-engine-runs'
        if self.protection_shell is not None:
            self._pin_directories((self.data,self.data/'runtime'))
            self.check_storage()
        reject_links(parent);parent.mkdir(parents=True,exist_ok=True)
        self.profile=parent/('owned-'+secrets.token_hex(16));self.profile.mkdir(exist_ok=False);require_efs(self.profile,encrypt=True)
        if self.protection_shell is not None:
            self._pin_directories((parent,self.profile))
            self.check_storage()
        environment=self.environment(cpu)
        self.backend.launch(verified['program'],environment,self.profile);self.started=True
        deadline=self.clock()+30
        while self.clock()<deadline:
            if self.backend.parent_status()['exited']:process_error('AI_ENGINE_EARLY_EXIT')
            listeners=self.backend.listeners()
            if listeners:
                self.assert_owned_listener()
                try:
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
                    with opener.open('http://127.0.0.1:11434/api/version',timeout=1) as response:
                        if response.geturl()!='http://127.0.0.1:11434/api/version':process_error()
                        raw=response.read(65537)
                    if len(raw)>65536 or json.loads(raw).get('version')!='0.40.3':process_error()
                    self.assert_owned_listener()
                    if self.protection_shell is not None:self.check_storage()
                    return {'started':True,'owned':True,'ready':True,'downloaded':False,'requested_model':False}
                except (OSError,ValueError):pass
            self.sleep(0.1)
        process_error('AI_ENGINE_READY_TIMEOUT')
    def environment(self,cpu):
        system=Path(os.environ['SystemRoot'])
        return {'SystemRoot':str(system),'WINDIR':str(system),'PATH':str(cpu)+';'+str(cpu/'lib/ollama')+';'+str(system/'System32'),
            'USERPROFILE':str(self.profile),'HOME':str(self.profile),'LOCALAPPDATA':str(self.profile),'APPDATA':str(self.profile),
            'TEMP':str(self.profile),'TMP':str(self.profile),'OLLAMA_MODELS':str(self.assets/'models'),
            'OLLAMA_HOST':'127.0.0.1:11434','OLLAMA_NO_CLOUD':'1','OLLAMA_NO_HISTORY':'1','OLLAMA_DEBUG':'false',
            'OLLAMA_NUM_PARALLEL':'1','OLLAMA_MAX_LOADED_MODELS':'1','OLLAMA_CONTEXT_LENGTH':'4096','OLLAMA_KEEP_ALIVE':'5m',
            'CUDA_VISIBLE_DEVICES':'-1','HIP_VISIBLE_DEVICES':'-1','GGML_VK_VISIBLE_DEVICES':'-1'}
    def assert_owned_listener(self):
        if not self.started or self.closed or self.backend.parent_status()['exited']:process_error('AI_ENGINE_OWNERSHIP_CHANGED')
        members=self.backend.members();listeners=self.backend.listeners()
        if not listeners or not listeners<=members:process_error('AI_ENGINE_OWNERSHIP_CHANGED')
    def guard_client(self,client):
        transport=client.transport
        def guarded(payload,timeout):
            self.assert_owned_listener()
            if self.protection_shell is not None:self.check_storage()
            try:return transport(payload,timeout)
            finally:
                # A timeout can still leave engine-created files behind.
                if self.protection_shell is not None:self.check_storage()
                self.assert_owned_listener()
        client.transport=guarded

    def _pin_directories(self,paths):
        for directory in paths:
            reject_links(directory)
            self.backend.lease(directory,directory=True)
            info=directory.stat();self.directory_ids[directory]=(info.st_dev,info.st_ino)

    def check_storage(self):
        """Only this run's profile; never scan old profiles or read file bodies.

        Directory leases are not a filesystem sandbox. The fixed trusted engine
        still runs as the current user; public asset writes need separate probes.
        """
        from .local_workspace_preparation import _acl,_file
        if self.protection_shell is None or self.protection_failed:
            process_error('AI_ENGINE_STORAGE_UNVERIFIED')
        try:
            paths=[self.data,self.data/'runtime']
            if self.profile is not None:paths.extend([self.profile.parent,self.profile])
            for directory in paths:
                reject_links(directory)
                if not directory.is_dir():process_error('AI_ENGINE_STORAGE_UNVERIFIED')
                require_efs(directory)
                info=directory.stat()
                if directory in self.directory_ids and self.directory_ids[directory]!=(info.st_dev,info.st_ino):
                    process_error('AI_ENGINE_STORAGE_CHANGED')
            pending=[] if self.profile is None else [self.profile];total=0
            while pending:
                directory=pending.pop()
                for path in directory.iterdir():
                    reject_links(path)
                    if len(paths)>=128:process_error('AI_ENGINE_STORAGE_LIMIT')
                    if path.is_dir():require_efs(path);pending.append(path)
                    else:
                        _file(path);total+=path.stat().st_size
                        if total>64*1024*1024:process_error('AI_ENGINE_STORAGE_LIMIT')
                    paths.append(path)
            for start in range(0,len(paths),24):_acl(self.protection_shell,paths[start:start+24],timeout_seconds=5)
            for directory,identity in self.directory_ids.items():
                reject_links(directory);info=directory.stat()
                if identity!=(info.st_dev,info.st_ino):process_error('AI_ENGINE_STORAGE_CHANGED')
            return {'protected':True,'checked_entries':len(paths),'profile_bytes':total}
        except Exception:
            self.protection_failed=True
            # Revoke use and request only our normal process-group stop. Retain
            # ownership until the existing cleanup proves the whole job exited.
            if self.started:
                try:self.backend.request_stop()
                except AppError:pass
            raise

    def require_private_runtime(self):
        self.assert_owned_listener()
        return self.check_storage()
    def stop(self,timeout=20):
        if self.closed:return self.last_stop
        try:self.backend.request_stop()
        except AppError:pass  # Query again and retain ownership on an uncertain signal.
        deadline=self.clock()+timeout
        while True:
            parent=self.backend.parent_status();members=self.backend.members();listeners=self.backend.listeners()
            if parent['exited'] and not members:
                # A newly occupied external port is reported separately and untouched.
                self.last_stop={'stopped':True,'owned_processes_stopped':True,'parent_exit_code':parent['exit_code'],
                    'job_active_processes':0,'fixed_port_state':'free' if not listeners else 'occupied_by_unowned_process',
                    'force_kill':False,'existing_profile_read':False}
                if self.protection_shell is not None:
                    try:self.last_stop['profile_protection']=self.check_storage()
                    except Exception:self.last_stop['profile_protection']={'protected':False,'state':'failed'}
                self.backend.close();self.closed=True;return self.last_stop
            if self.clock()>=deadline:
                self.last_stop={'stopped':False,'state':'STOP_PENDING','parent_exited':parent['exited'],
                    'job_active_processes':len(members),'force_kill':False,'handles_retained':True}
                return self.last_stop
            self.sleep(0.1)
