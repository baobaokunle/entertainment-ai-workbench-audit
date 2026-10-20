"""Bounded AES-GCM records: authenticated order, size, terminal manifest and EOF."""
import hashlib
import hmac
import json
import secrets
import struct
from .common import AppError,canonical_json

MAGIC=b'DFABSTREAM2\x00'
CHUNK_BYTES=1024*1024
MAX_DATABASE_BYTES=64*1024**3
MAX_MANIFEST_BYTES=1024*1024
HEADER_BYTES=len(MAGIC)+32+8+4
FRAME=struct.Struct('>BII')
MAX_FILE_BYTES=HEADER_BYTES+MAX_DATABASE_BYTES+(MAX_DATABASE_BYTES//CHUNK_BYTES+1)*(FRAME.size+16)+MAX_MANIFEST_BYTES+FRAME.size+16

def _cipher(key,salt):
    if not isinstance(key,bytes) or len(key)!=32:raise ValueError('key')
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    derived=HKDF(algorithm=hashes.SHA256(),length=32,salt=salt,info=b'dfa/sqlite-backup/stream/v2').derive(key)
    return AESGCM(derived)

def _read(stream,length):
    data=stream.read(length)
    if len(data)!=length:raise ValueError('truncated record')
    return data

def write_stream(source,target,key,metadata):
    salt=secrets.token_bytes(32);prefix=secrets.token_bytes(8)
    header=MAGIC+salt+prefix+struct.pack('>I',CHUNK_BYTES)
    cipher=_cipher(key,salt);target.write(header)
    total=0;sequence=0;sha=hashlib.sha256()
    def record(kind,plain):
        framing=FRAME.pack(kind,sequence,len(plain))
        target.write(framing)
        target.write(cipher.encrypt(prefix+struct.pack('>I',sequence),plain,header+framing))
    while True:
        plain=source.read(CHUNK_BYTES)
        if not plain:break
        total+=len(plain)
        if total>MAX_DATABASE_BYTES:raise AppError(413,'backup_size_limit','Streaming backup exceeds the 64 GiB limit.')
        sha.update(plain);record(1,plain);sequence+=1
    manifest=dict(metadata,format='dfa-sqlite-backup',version=2,database_bytes=total,database_sha256=sha.hexdigest(),chunk_bytes=CHUNK_BYTES,chunk_count=sequence)
    final=canonical_json(manifest)
    if not total or len(final)>MAX_MANIFEST_BYTES:raise AppError(413,'backup_manifest_limit','Backup database or manifest is invalid.')
    record(2,final)
    return manifest

def read_stream(source,target,key):
    """Write only to protected scratch; caller publishes after complete validation."""
    try:
        header=_read(source,HEADER_BYTES)
        if not header.startswith(MAGIC) or struct.unpack('>I',header[-4:])[0]!=CHUNK_BYTES:raise ValueError('header')
        salt=header[len(MAGIC):len(MAGIC)+32];prefix=header[-12:-4]
        cipher=_cipher(key,salt);sequence=0;total=0;short=False;sha=hashlib.sha256()
        while True:
            framing=_read(source,FRAME.size);kind,index,length=FRAME.unpack(framing)
            if index!=sequence or kind not in (1,2):raise ValueError('record order')
            if kind==1 and (short or not 0<length<=CHUNK_BYTES or total+length>MAX_DATABASE_BYTES):raise ValueError('data bound')
            if kind==2 and not 0<length<=MAX_MANIFEST_BYTES:raise ValueError('manifest bound')
            plain=cipher.decrypt(prefix+struct.pack('>I',sequence),_read(source,length+16),header+framing)
            if kind==1:
                target.write(plain);sha.update(plain);total+=length;sequence+=1;short=length<CHUNK_BYTES
                continue
            def pairs(items):
                result={}
                for name,value in items:
                    if name in result:raise ValueError('duplicate manifest key')
                    result[name]=value
                return result
            manifest=json.loads(plain,object_pairs_hook=pairs)
            if (not isinstance(manifest,dict) or manifest.get('format')!='dfa-sqlite-backup' or manifest.get('version')!=2
                or type(manifest.get('database_bytes')) is not int or manifest['database_bytes']!=total or not total
                or manifest.get('chunk_bytes')!=CHUNK_BYTES or type(manifest.get('chunk_count')) is not int or manifest['chunk_count']!=sequence
                or not isinstance(manifest.get('database_sha256'),str) or not hmac.compare_digest(manifest['database_sha256'],sha.hexdigest())
                or source.read(1)):
                raise ValueError('terminal manifest')
            return manifest
    except AppError:raise
    except Exception as exc:
        raise AppError(422,'backup_verification_failed','Streaming backup authentication or framing failed.') from exc
