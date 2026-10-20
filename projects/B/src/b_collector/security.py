"""Windows CurrentUser DPAPI and a private directory. No plaintext fallback."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import re
import stat


class CollectorError(ValueError):
    """Only fixed codes may cross the native protocol boundary."""


def check_path(path: Path, boundary: Path) -> Path:
    path = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(boundary))
    if path != boundary and boundary not in path.parents:
        raise CollectorError("PATH_BOUNDARY")
    # Include ancestors: a reparse point above the selected B directory is unsafe too.
    for p in [*reversed(path.parents), path]:
        if p.exists() or p.is_symlink():
            s = p.lstat()
            if stat.S_ISLNK(s.st_mode) or getattr(s, "st_file_attributes", 0) & 0x400:
                raise CollectorError("REPARSE_PATH")
            if stat.S_ISREG(s.st_mode) and s.st_nlink > 1:
                raise CollectorError("HARDLINK_PATH")
    return path


class Blob(C.Structure):
    _fields_ = [("size", W.DWORD), ("data", C.POINTER(C.c_ubyte))]


class AclInfo(C.Structure):
    _fields_ = [("count", W.DWORD), ("used", W.DWORD), ("free", W.DWORD)]


class WindowsProtection:
    def __init__(self):
        if os.name != "nt":
            raise CollectorError("WINDOWS_REQUIRED")
        self.adv = C.WinDLL("advapi32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.crypt = C.WinDLL("crypt32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = W.HANDLE
        self.kernel.LocalFree.argtypes = [C.c_void_p]
        self.kernel.LocalFree.restype = C.c_void_p
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.adv.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
        self.adv.GetTokenInformation.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
        self.adv.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
        self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)]
        self.adv.GetNamedSecurityInfoW.argtypes = [W.LPWSTR, C.c_int, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(C.c_void_p), C.POINTER(C.c_void_p), C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)]
        self.adv.GetNamedSecurityInfoW.restype = W.DWORD
        self.adv.GetSecurityDescriptorControl.argtypes = [C.c_void_p, C.POINTER(W.WORD), C.POINTER(W.DWORD)]
        self.adv.GetAclInformation.argtypes = [C.c_void_p, C.c_void_p, W.DWORD, C.c_int]
        self.adv.GetAce.argtypes = [C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)]
        self.adv.SetNamedSecurityInfoW.argtypes = [W.LPWSTR, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]
        self.adv.SetNamedSecurityInfoW.restype = W.DWORD
        self.adv.GetSecurityDescriptorDacl.argtypes = [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)]
        self.crypt.CryptProtectData.argtypes = [C.POINTER(Blob), W.LPCWSTR, C.POINTER(Blob), C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(Blob)]
        self.crypt.CryptUnprotectData.argtypes = [C.POINTER(Blob), C.POINTER(W.LPWSTR), C.POINTER(Blob), C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(Blob)]
        self.sid = self._current_sid()
        self.allowed = {self.sid, "S-1-5-18", "S-1-5-32-544"}

    def _sid_text(self, pointer):
        out = W.LPWSTR()
        if not self.adv.ConvertSidToStringSidW(pointer, C.byref(out)):
            raise CollectorError("SID_READ_FAILED")
        try:
            return out.value
        finally:
            self.kernel.LocalFree(C.cast(out, C.c_void_p))

    def _current_sid(self):
        token = W.HANDLE()
        if not self.adv.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, C.byref(token)):
            raise CollectorError("TOKEN_READ_FAILED")
        try:
            size = W.DWORD()
            self.adv.GetTokenInformation(token, 1, None, 0, C.byref(size))
            buf = C.create_string_buffer(size.value)
            if not self.adv.GetTokenInformation(token, 1, buf, size, C.byref(size)):
                raise CollectorError("TOKEN_READ_FAILED")
            return self._sid_text(C.cast(buf, C.POINTER(C.c_void_p))[0])
        finally:
            self.kernel.CloseHandle(token)

    def restrict_new_directory(self, path: Path):
        """Only called for a newly created empty directory, before any data."""
        sd = C.c_void_p()
        sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{self.sid})"
        if not self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(sd), None):
            raise CollectorError("ACL_BUILD_FAILED")
        try:
            present, defaulted, dacl = W.BOOL(), W.BOOL(), C.c_void_p()
            if not self.adv.GetSecurityDescriptorDacl(sd, C.byref(present), C.byref(dacl), C.byref(defaulted)):
                raise CollectorError("ACL_BUILD_FAILED")
            if self.adv.SetNamedSecurityInfoW(str(path), 1, 4 | 0x80000000, None, None, dacl, None):
                raise CollectorError("ACL_SET_FAILED")
        finally:
            self.kernel.LocalFree(sd)
        self.verify_acl(path, protected=True)

    def verify_acl(self, path: Path, *, protected=False):
        owner, dacl, sd = C.c_void_p(), C.c_void_p(), C.c_void_p()
        if self.adv.GetNamedSecurityInfoW(str(path), 1, 1 | 4, C.byref(owner), None, C.byref(dacl), None, C.byref(sd)):
            raise CollectorError("ACL_READ_FAILED")
        try:
            if not dacl.value or self._sid_text(owner) not in self.allowed:
                raise CollectorError("ACL_UNSAFE")
            control, revision = W.WORD(), W.DWORD()
            if not self.adv.GetSecurityDescriptorControl(sd, C.byref(control), C.byref(revision)):
                raise CollectorError("ACL_READ_FAILED")
            if protected and not control.value & 0x1000:
                raise CollectorError("ACL_INHERITANCE")
            info = AclInfo()
            if not self.adv.GetAclInformation(dacl, C.byref(info), C.sizeof(info), 2) or not info.count:
                raise CollectorError("ACL_UNSAFE")
            current_allowed = False
            for i in range(info.count):
                ace = C.c_void_p()
                if not self.adv.GetAce(dacl, i, C.byref(ace)):
                    raise CollectorError("ACL_READ_FAILED")
                header = C.string_at(ace, 8)
                # Only ordinary allow/deny ACEs; object/callback ACEs fail closed.
                if header[0] not in {0, 1}:
                    raise CollectorError("ACL_UNSAFE")
                sid = self._sid_text(ace.value + 8)
                if sid not in self.allowed:
                    raise CollectorError("ACL_UNSAFE")
                if sid == self.sid and header[0] == 0 and not header[1] & 8:
                    current_allowed = True
            if not current_allowed:
                raise CollectorError("ACL_CURRENT_USER_MISSING")
        finally:
            self.kernel.LocalFree(sd)

    def _transform(self, value: bytes, encrypt: bool) -> bytes:
        if not value or len(value) > 4 * 1024 * 1024:
            raise CollectorError("PROTECTION_SIZE")
        buf = (C.c_ubyte * len(value)).from_buffer_copy(value)
        incoming, outgoing = Blob(len(value), buf), Blob()
        # CRYPTPROTECT_UI_FORBIDDEN; CurrentUser; null optional entropy.
        okay = (self.crypt.CryptProtectData(C.byref(incoming), "B collector v1", None, None, None, 1, C.byref(outgoing))
                if encrypt else self.crypt.CryptUnprotectData(C.byref(incoming), None, None, None, None, 1, C.byref(outgoing)))
        try:
            if not okay:
                raise CollectorError("PROTECTION_FAILED")
            return C.string_at(outgoing.data, outgoing.size)
        finally:
            C.memset(buf, 0, len(value))
            if outgoing.data:
                C.memset(outgoing.data, 0, outgoing.size)
                self.kernel.LocalFree(outgoing.data)

    def protect(self, value):
        return b"BLC1\0" + self._transform(value, True)

    def unprotect(self, value):
        if not value.startswith(b"BLC1\0"):
            raise CollectorError("CIPHER_FORMAT")
        return self._transform(value[5:], False)


def private_root(base: Path, protector: WindowsProtection):
    root = check_path(base / "runtime" / "collector-private", base)
    root.parent.mkdir(exist_ok=True)
    try:
        root.mkdir()
    except FileExistsError:
        pass
    else:
        protector.restrict_new_directory(root)
    protector.verify_acl(root, protected=True)
    return root
