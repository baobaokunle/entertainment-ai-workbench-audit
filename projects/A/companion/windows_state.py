"""Opt-in Windows metadata APIs and notification pump. No input hooks or window titles."""
import ctypes,os,secrets
from ctypes import wintypes as W
from collections import deque
from .foreground import read_foreground

class WindowsStateAPI:
    def __init__(self,*,windows_session_id=None,user_sid=None):
        if os.name!='nt':raise RuntimeError('Windows required')
        self.user=ctypes.WinDLL('user32',use_last_error=True);self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.advapi=ctypes.WinDLL('advapi32',use_last_error=True);self.wts=ctypes.WinDLL('wtsapi32',use_last_error=True)
        self.queue=deque(maxlen=256);self.window=None;self.callback=None
        self.required_session=windows_session_id;self.required_sid=user_sid
    def session_identity(self):
        kernel=self.kernel;advapi=self.advapi;session=W.DWORD();sid=None
        kernel.GetCurrentProcessId.restype=W.DWORD
        kernel.ProcessIdToSessionId.argtypes=[W.DWORD,ctypes.POINTER(W.DWORD)]
        if not kernel.ProcessIdToSessionId(kernel.GetCurrentProcessId(),ctypes.byref(session)):return {'windows_session_id':None,'user_sid':None,'session_state':'unknown'}
        kernel.GetCurrentProcess.restype=W.HANDLE;token=W.HANDLE()
        advapi.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,ctypes.POINTER(W.HANDLE)]
        if advapi.OpenProcessToken(kernel.GetCurrentProcess(),0x0008,ctypes.byref(token)):
            try:
                needed=W.DWORD();advapi.GetTokenInformation.argtypes=[W.HANDLE,ctypes.c_int,ctypes.c_void_p,W.DWORD,ctypes.POINTER(W.DWORD)]
                advapi.GetTokenInformation(token,1,None,0,ctypes.byref(needed))
                if 0<needed.value<=65536:
                    buffer=ctypes.create_string_buffer(needed.value)
                    if advapi.GetTokenInformation(token,1,buffer,needed,ctypes.byref(needed)):
                        pointer=ctypes.cast(buffer,ctypes.POINTER(ctypes.c_void_p))[0];text=W.LPWSTR()
                        advapi.ConvertSidToStringSidW.argtypes=[ctypes.c_void_p,ctypes.POINTER(W.LPWSTR)]
                        if advapi.ConvertSidToStringSidW(pointer,ctypes.byref(text)):
                            try:sid=text.value
                            finally:
                                kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree(ctypes.cast(text,ctypes.c_void_p))
            finally:kernel.CloseHandle.argtypes=[W.HANDLE];kernel.CloseHandle(token)
        state='unknown';memory=ctypes.c_void_p();size=W.DWORD()
        self.wts.WTSQuerySessionInformationW.argtypes=[W.HANDLE,W.DWORD,ctypes.c_int,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(W.DWORD)]
        if self.wts.WTSQuerySessionInformationW(None,session,8,ctypes.byref(memory),ctypes.byref(size)):
            try:
                if size.value>=ctypes.sizeof(ctypes.c_int):state='active' if ctypes.cast(memory,ctypes.POINTER(ctypes.c_int))[0]==0 else 'inactive'
            finally:self.wts.WTSFreeMemory.argtypes=[ctypes.c_void_p];self.wts.WTSFreeMemory(memory)
        return {'windows_session_id':session.value,'user_sid':sid,'session_state':state}
    def lock_state(self):
        # Access failure is unknown, never inferred to mean locked.
        user=self.user;user.OpenInputDesktop.argtypes=[W.DWORD,W.BOOL,W.DWORD];user.OpenInputDesktop.restype=W.HANDLE
        desktop=user.OpenInputDesktop(0,False,0x0001)
        if not desktop:return 'unknown'
        try:
            buffer=ctypes.create_unicode_buffer(256);needed=W.DWORD()
            user.GetUserObjectInformationW.argtypes=[W.HANDLE,ctypes.c_int,ctypes.c_void_p,W.DWORD,ctypes.POINTER(W.DWORD)]
            if not user.GetUserObjectInformationW(desktop,2,buffer,ctypes.sizeof(buffer),ctypes.byref(needed)):return 'unknown'
            return {'Default':'unlocked','Winlogon':'locked'}.get(buffer.value,'unknown')
        finally:user.CloseDesktop.argtypes=[W.HANDLE];user.CloseDesktop(desktop)
    def read_context(self):
        identity=self.session_identity();locked=self.lock_state()
        if identity['user_sid'] is None or identity['session_state']!='active' or locked!='unlocked' or self.required_session is not None and identity['windows_session_id']!=self.required_session or self.required_sid is not None and identity['user_sid']!=self.required_sid:
            return {**identity,'lock_state':locked,'foreground_hwnd':None,'process_id':None,'program_basename':None,'visible':False,'focused':False}
        value=read_foreground(include_image_path=True);value.update(identity);value['lock_state']=locked
        self.user.IsWindowVisible.argtypes=[W.HWND];value['visible']=bool(value['foreground_hwnd'] and self.user.IsWindowVisible(value['foreground_hwnd']))
        value['focused']=bool(value['foreground_hwnd'] and value['session_state']=='active' and value['lock_state']=='unlocked')
        return value
    def start_notifications(self):
        """Explicit caller-owned message pump, no worker thread or autostart."""
        if self.window:return
        callback_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,W.HWND,W.UINT,W.WPARAM,W.LPARAM)
        class WindowClass(ctypes.Structure):
            _fields_=[('style',W.UINT),('proc',callback_type),('cls_extra',ctypes.c_int),('wnd_extra',ctypes.c_int),('instance',W.HINSTANCE),('icon',W.HANDLE),('cursor',W.HANDLE),('background',W.HANDLE),('menu',W.LPCWSTR),('name',W.LPCWSTR)]
        user=self.user
        user.DefWindowProcW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM];user.DefWindowProcW.restype=ctypes.c_ssize_t
        def callback(hwnd,msg,wp,lp):
            if msg==0x218:
                action={4:'sleep',18:'resume',7:'resume'}.get(int(wp))
                if action:self.queue.append(action)
            elif msg==0x2b1:
                action={7:'lock',8:'unlock',2:'session_inactive',4:'session_inactive',6:'session_inactive',1:'session_changed',3:'session_changed',5:'session_changed'}.get(int(wp))
                if action:self.queue.append(action)
            return user.DefWindowProcW(hwnd,msg,wp,lp)
        self.callback=callback_type(callback);self.class_name='DFAReadOnlyActivity_'+secrets.token_hex(8)
        self.kernel.GetModuleHandleW.argtypes=[W.LPCWSTR];self.kernel.GetModuleHandleW.restype=W.HMODULE
        self.instance=self.kernel.GetModuleHandleW(None);description=WindowClass(0,self.callback,0,0,self.instance,None,None,None,None,self.class_name)
        user.RegisterClassW.argtypes=[ctypes.POINTER(WindowClass)];user.RegisterClassW.restype=W.ATOM
        if not user.RegisterClassW(ctypes.byref(description)):raise OSError('Read-only notification class registration failed')
        user.CreateWindowExW.argtypes=[W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,W.HWND,W.HMENU,W.HINSTANCE,ctypes.c_void_p];user.CreateWindowExW.restype=W.HWND
        # Invisible top-level window receives system power broadcasts; not message-only.
        self.window=user.CreateWindowExW(0,self.class_name,'',0,0,0,0,0,None,None,self.instance,None)
        self.wts.WTSRegisterSessionNotification.argtypes=[W.HWND,W.DWORD]
        if not self.window or not self.wts.WTSRegisterSessionNotification(self.window,0):
            self.close();raise OSError('Read-only session notification registration failed')
    def poll_notifications(self):
        if not self.window:return []
        message=W.MSG();self.user.PeekMessageW.argtypes=[ctypes.POINTER(W.MSG),W.HWND,W.UINT,W.UINT,W.UINT]
        self.user.DispatchMessageW.argtypes=[ctypes.POINTER(W.MSG)];self.user.DispatchMessageW.restype=ctypes.c_ssize_t
        for _ in range(256):
            if not self.user.PeekMessageW(ctypes.byref(message),self.window,0,0,1):break
            self.user.DispatchMessageW(ctypes.byref(message))
        result=list(self.queue);self.queue.clear();return result
    def close(self):
        if self.window:
            self.wts.WTSUnRegisterSessionNotification.argtypes=[W.HWND];self.wts.WTSUnRegisterSessionNotification(self.window)
            self.user.DestroyWindow.argtypes=[W.HWND];self.user.DestroyWindow(self.window);self.window=None
        if getattr(self,'class_name',None):
            self.user.UnregisterClassW.argtypes=[W.LPCWSTR,W.HINSTANCE];self.user.UnregisterClassW(self.class_name,self.instance);self.class_name=None
