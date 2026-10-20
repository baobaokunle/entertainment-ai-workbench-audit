"""Read-only minimal Windows foreground metadata. No titles, keys, clipboard or screenshots.

This is not an installed collector. HWND-to-Chrome-window binding remains explicit
and unverified until target-device calibration; unknown windows stay unknown.
"""
import ctypes,os
from ctypes import wintypes
from pathlib import PureWindowsPath

def read_foreground(*,include_image_path=False):
    if os.name!='nt':raise RuntimeError('Windows required')
    user=ctypes.WinDLL('user32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    user.GetForegroundWindow.restype=wintypes.HWND
    hwnd=user.GetForegroundWindow();pid=wintypes.DWORD()
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
    handle=kernel.OpenProcess(0x1000,False,pid.value);program=None;image_path=None
    if handle:
        try:
            size=wintypes.DWORD(32768);buffer=ctypes.create_unicode_buffer(size.value)
            kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
            if kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)):image_path=buffer.value;program=PureWindowsPath(buffer.value).name
        finally:
            kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle(handle)
    result={'foreground_hwnd':int(hwnd or 0),'process_id':pid.value,'program_basename':program,'binding_state':'unverified'}
    if include_image_path:result['image_path']=image_path  # Local verification only; never an activity payload field.
    return result
