"""Windows owned process group in an atomic, non-killing anonymous Job Object."""
import ctypes,os,socket,struct,subprocess
from ctypes import wintypes as W
from pathlib import Path
from .common import AppError

PTR=ctypes.c_void_p;SIZE=ctypes.c_size_t;DWORD=ctypes.c_uint32;BOOL=ctypes.c_int
class SecurityAttributes(ctypes.Structure):
    _fields_=[('length',DWORD),('descriptor',PTR),('inherit',BOOL)]
class StartupInfo(ctypes.Structure):
    _fields_=[('cb',DWORD),('reserved',W.LPWSTR),('desktop',W.LPWSTR),('title',W.LPWSTR),
        ('x',DWORD),('y',DWORD),('xsize',DWORD),('ysize',DWORD),('xchars',DWORD),('ychars',DWORD),
        ('fill',DWORD),('flags',DWORD),('show',ctypes.c_uint16),('reserved2bytes',ctypes.c_uint16),
        ('reserved2',PTR),('stdin',PTR),('stdout',PTR),('stderr',PTR)]
class StartupInfoEx(ctypes.Structure):_fields_=[('basic',StartupInfo),('attributes',PTR)]
class ProcessInfo(ctypes.Structure):_fields_=[('process',PTR),('thread',PTR),('pid',DWORD),('tid',DWORD)]

def process_error(code='AI_ENGINE_PROCESS_UNVERIFIED'):
    raise AppError(503,code,'本机模型进程状态未核验；不接管既有程序、不强杀或切换端口')

class WindowsOwnedProcess:
    def __init__(self):
        if os.name!='nt' or ctypes.sizeof(PTR)!=8:process_error()
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.ip=ctypes.WinDLL('iphlpapi',use_last_error=True)
        signatures={
            'CreateFileW':([W.LPCWSTR,DWORD,DWORD,PTR,DWORD,DWORD,PTR],PTR),
            'CloseHandle':([PTR],BOOL),'CreateJobObjectW':([PTR,W.LPCWSTR],PTR),
            'QueryInformationJobObject':([PTR,ctypes.c_int,PTR,DWORD,ctypes.POINTER(DWORD)],BOOL),
            'InitializeProcThreadAttributeList':([PTR,DWORD,DWORD,ctypes.POINTER(SIZE)],BOOL),
            'UpdateProcThreadAttribute':([PTR,DWORD,SIZE,PTR,SIZE,PTR,PTR],BOOL),
            'DeleteProcThreadAttributeList':([PTR],None),
            'CreateProcessW':([W.LPCWSTR,W.LPWSTR,PTR,PTR,BOOL,DWORD,PTR,W.LPCWSTR,PTR,ctypes.POINTER(ProcessInfo)],BOOL),
            'GetConsoleProcessList':([ctypes.POINTER(DWORD),DWORD],DWORD),
            'GetExitCodeProcess':([PTR,ctypes.POINTER(DWORD)],BOOL),
            'GetProcessId':([PTR],DWORD),'WaitForSingleObject':([PTR,DWORD],DWORD),
            'GenerateConsoleCtrlEvent':([DWORD,DWORD],BOOL),
        }
        for name,(args,result) in signatures.items():
            function=getattr(self.kernel,name);function.argtypes=args;function.restype=result
        self.ip.GetExtendedTcpTable.argtypes=[PTR,ctypes.POINTER(DWORD),BOOL,DWORD,DWORD,DWORD]
        self.ip.GetExtendedTcpTable.restype=DWORD
        self.process=None;self.job=None;self.pid=None;self.signal_sent=False;self.leases=[]
    def require_console(self):
        values=(DWORD*128)();count=self.kernel.GetConsoleProcessList(values,128)
        if not 0<count<=128 or os.getpid() not in list(values)[:count]:process_error('AI_ENGINE_CONSOLE_REQUIRED')
    def listeners(self):
        found=set()
        for family,row_size,port_offset,pid_offset in ((2,24,8,20),(23,56,20,52)):
            size=DWORD(0);result=self.ip.GetExtendedTcpTable(None,ctypes.byref(size),False,family,3,0)
            if result not in (0,122):process_error()
            for _ in range(3):
                if not 4<=size.value<=1024**2:process_error()
                buffer=ctypes.create_string_buffer(size.value)
                result=self.ip.GetExtendedTcpTable(buffer,ctypes.byref(size),False,family,3,0)
                if result==122:continue
                if result!=0:process_error()
                raw=buffer.raw;count=struct.unpack_from('<I',raw)[0]
                if 4+count*row_size>len(raw):process_error()
                for index in range(count):
                    base=4+index*row_size
                    port=socket.ntohs(struct.unpack_from('<I',raw,base+port_offset)[0]&0xffff)
                    if port==11434:
                        pid=struct.unpack_from('<I',raw,base+pid_offset)[0]
                        if not pid:process_error()
                        found.add(pid)
                break
            else:process_error()
        return found
    def lease(self,path,*,directory=False):
        # Attributes-only directory handles do not participate in the normal
        # read/write/delete sharing check. FILE_LIST_DIRECTORY does: protect
        # this object from rename, without claiming to sandbox its children.
        handle=self.kernel.CreateFileW(str(path),1 if directory else 0x80000000,1,None,3,0x02000000 if directory else 0x80,None)
        if not handle or handle==PTR(-1).value:process_error('AI_ENGINE_FILE_LEASE_FAILED')
        self.leases.append(handle)
    def members(self):
        if self.job is None:return set()
        buffer=ctypes.create_string_buffer(8+128*ctypes.sizeof(SIZE));used=DWORD()
        if not self.kernel.QueryInformationJobObject(self.job,3,buffer,len(buffer),ctypes.byref(used)):process_error()
        assigned,count=struct.unpack_from('<II',buffer.raw)
        if count!=assigned or count>128 or used.value>len(buffer) or 8+count*8>len(buffer):process_error()
        pids={struct.unpack_from('<Q',buffer.raw,8+index*8)[0] for index in range(count)}
        if len(pids)!=count or any(not pid for pid in pids):process_error()
        return pids
    def parent_status(self):
        if self.process is None:return {'exited':True,'exit_code':None}
        status=self.kernel.WaitForSingleObject(self.process,0)
        if status==258:return {'exited':False,'exit_code':None}
        if status!=0:process_error()
        code=DWORD()
        if not self.kernel.GetExitCodeProcess(self.process,ctypes.byref(code)):process_error()
        return {'exited':True,'exit_code':code.value}
    def launch(self,program,environment,cwd):
        if self.process is not None or self.job is not None:process_error('AI_ENGINE_ALREADY_STARTED')
        self.require_console()
        if self.listeners():process_error('AI_ENGINE_PORT_OCCUPIED')
        self.job=self.kernel.CreateJobObjectW(None,None)
        if not self.job:process_error()
        attributes=None;attributes_initialized=False;nul=[]
        try:
            security=SecurityAttributes(ctypes.sizeof(SecurityAttributes),None,True)
            for access in (0x80000000,0x40000000):
                handle=self.kernel.CreateFileW('NUL',access,3,ctypes.byref(security),3,0x80,None)
                if not handle or handle==PTR(-1).value:process_error()
                nul.append(handle)
            size=SIZE();self.kernel.InitializeProcThreadAttributeList(None,2,0,ctypes.byref(size))
            if not 0<size.value<=65536:process_error()
            attributes=ctypes.create_string_buffer(size.value)
            if not self.kernel.InitializeProcThreadAttributeList(attributes,2,0,ctypes.byref(size)):process_error()
            attributes_initialized=True
            jobs=(PTR*1)(self.job);handles=(PTR*2)(*nul)
            if not self.kernel.UpdateProcThreadAttribute(attributes,0,0x2000D,jobs,ctypes.sizeof(jobs),None,None):process_error()
            if not self.kernel.UpdateProcThreadAttribute(attributes,0,0x20002,handles,ctypes.sizeof(handles),None,None):process_error()
            startup=StartupInfoEx();startup.basic.cb=ctypes.sizeof(startup);startup.basic.flags=0x100
            startup.basic.stdin=nul[0];startup.basic.stdout=startup.basic.stderr=nul[1]
            startup.attributes=ctypes.cast(attributes,PTR)
            command=ctypes.create_unicode_buffer(subprocess.list2cmdline([str(program),'serve']))
            block=ctypes.create_unicode_buffer('\0'.join(key+'='+value for key,value in sorted(environment.items(),key=lambda pair:pair[0].upper()))+'\0\0')
            info=ProcessInfo()
            # Job assignment is atomic, so no unassigned suspended orphan exists.
            if not self.kernel.CreateProcessW(str(program),command,None,None,True,0x80000|0x400|0x200,block,str(cwd),ctypes.byref(startup),ctypes.byref(info)):process_error('AI_ENGINE_CREATE_FAILED')
            self.process=info.process;self.pid=info.pid
            self.kernel.CloseHandle(info.thread)
            if self.pid not in self.members():process_error()
        finally:
            if attributes_initialized:self.kernel.DeleteProcThreadAttributeList(attributes)
            for handle in nul:self.kernel.CloseHandle(handle)
    def request_stop(self):
        if self.process is None or self.signal_sent or self.parent_status()['exited']:return
        if not self.pid or self.kernel.GetProcessId(self.process)!=self.pid or self.pid not in self.members():process_error()
        self.require_console()
        if not self.kernel.GenerateConsoleCtrlEvent(1,self.pid):process_error('AI_ENGINE_STOP_SIGNAL_FAILED')
        self.signal_sent=True
    def close(self):
        if not self.parent_status()['exited'] or self.members():process_error('AI_ENGINE_STOP_PENDING')
        if self.process is not None:self.kernel.CloseHandle(self.process);self.process=None
        if self.job is not None:self.kernel.CloseHandle(self.job);self.job=None
        for handle in self.leases:self.kernel.CloseHandle(handle)
        self.leases=[]
