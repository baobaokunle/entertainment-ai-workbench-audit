"""Windows user-bound key protection and versioned AES-256-GCM envelopes."""
import ctypes
from ctypes import wintypes
import os
import secrets

from .common import AppError


class DPAPIProtector:
    MAGIC = b'DFADPAPI1\x00'

    def _crypt(self, data, decrypt=False):
        if os.name != 'nt':
            raise AppError(503, 'dpapi_unavailable', 'Windows user DPAPI is required.')
        if not isinstance(data, bytes) or not data or len(data) > 16 * 1024 * 1024:
            raise AppError(422, 'invalid_protected_data', 'Protected data is invalid.')
        class Blob(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        src, dst = Blob(len(data), buf), Blob()
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if decrypt:
            fn = crypt.CryptUnprotectData
            fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            args = (ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst))
        else:
            fn = crypt.CryptProtectData
            fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            args = (ctypes.byref(src), 'Douyin Fan Assistant local protection', None, None, None, 1, ctypes.byref(dst))
        fn.restype = wintypes.BOOL
        if not fn(*args):
            raise AppError(503, 'dpapi_failed', 'Windows user protection failed; operation stopped.')
        try:
            return ctypes.string_at(dst.pbData, dst.cbData)
        finally:
            if dst.pbData:
                ctypes.memset(dst.pbData, 0, dst.cbData)
                kernel.LocalFree(ctypes.cast(dst.pbData, ctypes.c_void_p))

    def protect(self, data):
        return self.MAGIC + self._crypt(data)

    def unprotect(self, data):
        if not isinstance(data, bytes) or not data.startswith(self.MAGIC):
            raise AppError(422, 'invalid_protected_data', 'Unrecognized protected data format.')
        return self._crypt(data[len(self.MAGIC):], decrypt=True)


class SecureEnvelope:
    MAGIC = b'DFAA256GCM1\x00'
    MAX_BYTES = 256 * 1024 * 1024

    @staticmethod
    def _cipher(key):
        if not isinstance(key, bytes) or len(key) != 32:
            raise AppError(422, 'invalid_key', 'A 256-bit encryption key is required.')
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise AppError(503, 'crypto_unavailable', 'Required cryptography dependency is unavailable.') from exc
        return AESGCM(key)

    @classmethod
    def seal(cls, data, key, *, aad=b''):
        if not isinstance(data, bytes) or not isinstance(aad, bytes) or len(data) > cls.MAX_BYTES:
            raise AppError(413, 'envelope_limit', 'Encrypted payload is invalid or exceeds the limit.')
        nonce = secrets.token_bytes(12)
        return cls.MAGIC + nonce + cls._cipher(key).encrypt(nonce, data, cls.MAGIC + aad)

    @classmethod
    def open(cls, envelope, key, *, aad=b''):
        prefix = len(cls.MAGIC)
        if (not isinstance(envelope, bytes) or not isinstance(aad, bytes)
                or not envelope.startswith(cls.MAGIC) or len(envelope) < prefix + 28
                or len(envelope) > cls.MAX_BYTES + prefix + 28):
            raise AppError(422, 'invalid_envelope', 'Encrypted envelope format or size is invalid.')
        cipher = cls._cipher(key)
        try:
            return cipher.decrypt(envelope[prefix:prefix+12], envelope[prefix+12:], cls.MAGIC + aad)
        except Exception as exc:
            raise AppError(422, 'authentication_failed', 'Encrypted data authentication failed.') from exc
