"""Decrypt authorized image bytes using the key supplied in that source message.

Pinned public pc-im worker: 12-byte nonce, AES-GCM ciphertext, 16-byte tag.
Windows CNG performs authenticated decryption; no key guessing or plaintext files.
"""
import ctypes as C
import os
import re
from .security import CollectorError


class AuthInfo(C.Structure):
    _fields_ = [('size',C.c_uint32),('version',C.c_uint32),('nonce',C.c_void_p),('nonce_size',C.c_uint32),
                ('aad',C.c_void_p),('aad_size',C.c_uint32),('tag',C.c_void_p),('tag_size',C.c_uint32),
                ('mac',C.c_void_p),('mac_size',C.c_uint32),('aad_count',C.c_uint32),
                ('data_count',C.c_uint64),('flags',C.c_uint32)]


def decrypt_source_image(data, key_hex):
    if os.name != 'nt': raise CollectorError('WINDOWS_REQUIRED')
    if not isinstance(key_hex,str) or not re.fullmatch(r'[0-9a-fA-F]{64}',key_hex):
        raise CollectorError('MEDIA_SOURCE_KEY_INVALID')
    if not isinstance(data,bytes) or not 28 <= len(data) <= 20*1024*1024+28:
        raise CollectorError('MEDIA_CIPHER_SIZE')
    dll=C.WinDLL('bcrypt',use_last_error=True);handle=C.c_void_p;u32=C.c_uint32;ptr=C.c_void_p
    signatures={
      'BCryptOpenAlgorithmProvider':[C.POINTER(handle),C.c_wchar_p,C.c_wchar_p,u32],
      'BCryptSetProperty':[handle,C.c_wchar_p,ptr,u32,u32],
      'BCryptGenerateSymmetricKey':[handle,C.POINTER(handle),ptr,u32,ptr,u32,u32],
      'BCryptDecrypt':[handle,ptr,u32,ptr,ptr,u32,ptr,u32,C.POINTER(u32),u32],
      'BCryptDestroyKey':[handle], 'BCryptCloseAlgorithmProvider':[handle,u32]}
    for name,args in signatures.items():getattr(dll,name).argtypes=args;getattr(dll,name).restype=C.c_long
    algorithm=handle();key=handle();keybuf=C.create_string_buffer(bytes.fromhex(key_hex))
    output=C.create_string_buffer(max(1,len(data)-28));written=u32()
    def checked(status):
        if status != 0: raise CollectorError('MEDIA_DECRYPT_FAILED')
    try:
        checked(dll.BCryptOpenAlgorithmProvider(C.byref(algorithm),'AES',None,0))
        mode=C.create_unicode_buffer('ChainingModeGCM')
        checked(dll.BCryptSetProperty(algorithm,'ChainingMode',mode,C.sizeof(mode),0))
        # CNG owns its key-object memory; BCryptDestroyKey releases it.
        checked(dll.BCryptGenerateSymmetricKey(algorithm,C.byref(key),None,0,keybuf,32,0))
        nonce=C.create_string_buffer(data[:12]);tag=C.create_string_buffer(data[-16:])
        encrypted=C.create_string_buffer(data[12:-16]);info=AuthInfo()
        info.size=C.sizeof(info);info.version=1;info.nonce=C.addressof(nonce);info.nonce_size=12
        info.tag=C.addressof(tag);info.tag_size=16
        checked(dll.BCryptDecrypt(key,encrypted,len(data)-28,C.byref(info),None,0,
                                  output,len(output),C.byref(written),0))
        if written.value != len(data)-28: raise CollectorError('MEDIA_DECRYPT_FAILED')
        return output.raw[:written.value]
    finally:
        if key.value:dll.BCryptDestroyKey(key)
        if algorithm.value:dll.BCryptCloseAlgorithmProvider(algorithm,0)
        C.memset(keybuf,0,C.sizeof(keybuf));C.memset(output,0,C.sizeof(output))
