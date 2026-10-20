"""Explicit authorized Windows export copy; source is never opened by SQLite.

Only new verified-EFS private copies are recovered and backed up. A completed
two-file directory is published once. Manifest checksums are not provenance or
permission authentication. No application, HTTP or background entry exists.
"""
import argparse
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
WORKSPACE_ROOT=ROOT.parent
sys.path[:0]=[str(ROOT),str(ROOT/'vendor')]
from server.backup_scratch import ProtectedScratch,reject_links,require_efs
from server.common import canonical_json

FORMAT='dfa-large-export-snapshot-1'
BUFFER_BYTES=1048576
MAX_SOURCE_BYTES=1024**3
MAX_PRIVATE_BYTES=2*1024**3
MAX_MANIFEST_BYTES=16384
MAX_OPERATION_SECONDS=300
MAX_SQL_SECONDS=60
MAX_VM_STEPS=100_000_000
MAX_SCHEMA_OBJECTS=32
MAX_ROWS=1_000_000
MAX_DISTINCT_CONVERSATIONS=10000
SCHEMAS={
 'messages':(('msg_id','TEXT'),('conv_id','TEXT'),('sender_uid','TEXT'),('sender_name','TEXT'),('content','TEXT'),('msg_type','INTEGER'),('media_url','TEXT'),('media_local_path','TEXT'),('timestamp','INTEGER'),('seq','INTEGER'),('raw_data','TEXT'),('ref_msg','TEXT')),
 'conversations':(('conv_id','TEXT'),('conv_type','INTEGER'),('name','TEXT'),('participant_uids','TEXT'),('last_message_time','INTEGER'),('message_count','INTEGER')),
 'users':(('uid','TEXT'),('nickname','TEXT'),('avatar_url','TEXT'),('unique_id','TEXT')),
 'voice_transcriptions':(('msg_id','TEXT'),('message_id','TEXT'),('text_result','TEXT'),('status','TEXT'),('error','TEXT'),('updated_at','INTEGER')),
}


class SnapshotError(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)


def fail(code):raise SnapshotError(code)


class FileInfo(ctypes.Structure):
    _fields_=[('attributes',wintypes.DWORD),('created',wintypes.FILETIME),('accessed',wintypes.FILETIME),('written',wintypes.FILETIME),('volume',wintypes.DWORD),('size_hi',wintypes.DWORD),('size_lo',wintypes.DWORD),('links',wintypes.DWORD),('index_hi',wintypes.DWORD),('index_lo',wintypes.DWORD)]


class LockedRead:
    """GENERIC_READ with only FILE_SHARE_READ; excludes writers/replacement."""
    def __init__(self,path):self.path=Path(path);self.handle=None
    def __enter__(self):
        if os.name!='nt':fail('WINDOWS_REQUIRED')
        reject_links(self.path)
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];self.kernel.CreateFileW.restype=wintypes.HANDLE
        self.kernel.GetFileInformationByHandle.argtypes=[wintypes.HANDLE,ctypes.POINTER(FileInfo)];self.kernel.GetFileInformationByHandle.restype=wintypes.BOOL
        self.kernel.ReadFile.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p];self.kernel.ReadFile.restype=wintypes.BOOL
        self.kernel.SetFilePointerEx.argtypes=[wintypes.HANDLE,ctypes.c_longlong,ctypes.POINTER(ctypes.c_longlong),wintypes.DWORD];self.kernel.SetFilePointerEx.restype=wintypes.BOOL
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE];self.kernel.CloseHandle.restype=wintypes.BOOL
        self.handle=self.kernel.CreateFileW(str(self.path),0x80000000,1,None,3,0x08000000|0x00200000,None)
        if self.handle==ctypes.c_void_p(-1).value:
            self.handle=None;fail('SOURCE_IN_USE' if ctypes.get_last_error() in (32,33) else 'SOURCE_LOCK_FAILED')
        try:
            self.initial=self.info()
            if self.initial['attributes']&(0x10|0x400) or self.initial['links']!=1:fail('SOURCE_PATH_REJECTED')
            if not 0<=self.initial['bytes']<=MAX_SOURCE_BYTES:fail('SOURCE_SIZE_LIMIT')
            return self
        except BaseException:self.__exit__(None,None,None);raise
    def info(self):
        info=FileInfo()
        if not self.kernel.GetFileInformationByHandle(self.handle,ctypes.byref(info)):fail('SOURCE_STAT_FAILED')
        return {'attributes':int(info.attributes),'bytes':(info.size_hi<<32)|info.size_lo,'links':int(info.links),
                'identity':(info.volume,info.index_hi,info.index_lo),'created':(info.created.dwHighDateTime,info.created.dwLowDateTime),
                'written':(info.written.dwHighDateTime,info.written.dwLowDateTime)}
    def _seek(self):
        if not self.kernel.SetFilePointerEx(self.handle,0,None,0):fail('SOURCE_READ_FAILED')
    def read_prefix(self,length):
        if type(length) is not int or not 0<=length<=100:fail('READ_BUDGET')
        self._seek();buffer=ctypes.create_string_buffer(length);read=wintypes.DWORD()
        if not self.kernel.ReadFile(self.handle,buffer,length,ctypes.byref(read),None):fail('SOURCE_READ_FAILED')
        return bytes(buffer[:read.value])
    def blocks(self):
        self._seek();buffer=ctypes.create_string_buffer(BUFFER_BYTES);total=0
        while True:
            read=wintypes.DWORD()
            if not self.kernel.ReadFile(self.handle,buffer,BUFFER_BYTES,ctypes.byref(read),None):fail('SOURCE_READ_FAILED')
            if not read.value:break
            total+=read.value
            if total>self.initial['bytes']:fail('SOURCE_CHANGED')
            yield memoryview(buffer).cast('B')[:read.value]
        if total!=self.initial['bytes'] or self.info()!=self.initial:fail('SOURCE_CHANGED')
    def copy_and_hash(self,destination,*,check=None):
        destination=Path(destination);require_efs(destination.parent)
        digest=hashlib.sha256();total=0
        with destination.open('r+b' if destination.exists() else 'xb') as output:
            require_efs(destination);output.seek(0);output.truncate()
            for block in self.blocks():
                if check:check()
                digest.update(block);output.write(block);total+=len(block)
            output.flush();os.fsync(output.fileno())
        require_efs(destination)
        return {'bytes':total,'sha256':digest.hexdigest()}
    def hash_again(self,*,check=None):
        digest=hashlib.sha256();total=0
        for block in self.blocks():
            if check:check()
            digest.update(block);total+=len(block)
        return {'bytes':total,'sha256':digest.hexdigest()}
    def __exit__(self,*args):
        if self.handle is not None:self.kernel.CloseHandle(self.handle);self.handle=None


def inventory(source):
    result={}
    for suffix in ('','-wal','-shm','-journal'):
        path=source.with_name(source.name+suffix);reject_links(path)
        if not path.exists():result[suffix]=None;continue
        stat=path.stat()
        if not path.is_file() or stat.st_nlink!=1:fail('SOURCE_PATH_REJECTED')
        result[suffix]=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,getattr(stat,'st_file_attributes',0),stat.st_nlink)
    if result[''] is None:fail('SOURCE_MISSING')
    if result['-journal'] is not None:fail('SOURCE_JOURNAL_PRESENT')
    if result['-shm'] and result['-shm'][2]>16*1048576:fail('SOURCE_SIZE_LIMIT')
    if sum(info[2] for suffix,info in result.items() if suffix in ('','-wal') and info)>MAX_SOURCE_BYTES:fail('SOURCE_SIZE_LIMIT')
    return result


def _file_hash(path,check=None):
    with LockedRead(path) as reader:return reader.hash_again(check=check)


def _verify_efs(database):
    require_efs(database.parent);require_efs(database)
    for suffix in ('-wal','-shm','-journal'):
        path=database.with_name(database.name+suffix)
        if path.exists():
            reject_links(path)
            if not path.is_file() or path.stat().st_nlink!=1:fail('PRIVATE_PATH_REJECTED')
            require_efs(path)


def _header(main,wal):
    header=main.read_prefix(100)
    if len(header)!=100 or header[:16]!=b'SQLite format 3\x00':fail('SOURCE_HEADER_INVALID')
    pagesize=int.from_bytes(header[16:18],'big');pagesize=65536 if pagesize==1 else pagesize
    if pagesize not in (512,1024,2048,4096,8192,16384,32768,65536) or header[18] not in (1,2) or header[19] not in (1,2):fail('SOURCE_HEADER_INVALID')
    if wal:
        raw=wal.read_prefix(32)
        if len(raw)!=32:fail('WAL_REJECTED')
        magic,version,size=struct.unpack('>III',raw[:12])
        if magic not in (0x377f0682,0x377f0683) or version!=3007000 or size!=pagesize or (wal.initial['bytes']-32)%(24+pagesize):fail('WAL_REJECTED')
    return pagesize


def _wal_integrity(path,pagesize,check):
    """Check finite WAL framing/checksums; SQLite alone chooses commit state.

    No frames are changed, salvaged, applied, or selected by this validator.
    Complete valid uncommitted tail frames are accepted; malformed tails reject.
    """
    def checksum(raw,endian,state):
        first,second=state
        for left,right in struct.iter_unpack(endian+'II',raw):
            first=(first+left+second)&0xffffffff;second=(second+right+first)&0xffffffff
        return first,second
    with path.open('rb') as stream:
        header=stream.read(32);magic=struct.unpack('>I',header[:4])[0]
        endian='<' if magic==0x377f0682 else '>'
        state=checksum(header[:24],endian,(0,0))
        if state!=struct.unpack('>II',header[24:32]):fail('WAL_REJECTED')
        committed=False;old_salt=None
        while True:
            check();frame=stream.read(24)
            if not frame:break
            data=stream.read(pagesize)
            if len(frame)!=24 or len(data)!=pagesize:fail('WAL_REJECTED')
            page,commit=struct.unpack('>II',frame[:8])
            if not 1<=page<=MAX_SOURCE_BYTES//pagesize or commit>MAX_SOURCE_BYTES//pagesize:fail('WAL_REJECTED')
            if frame[8:16]!=header[16:24]:
                # SQLite can recycle a WAL without truncating its old suffix.
                # We accept only one old-salt suffix after a valid current commit;
                # SQLite, not this framing check, selects the committed prefix.
                if not committed or old_salt is not None and frame[8:16]!=old_salt:fail('WAL_REJECTED')
                old_salt=frame[8:16];continue
            if old_salt is not None:fail('WAL_REJECTED')
            state=checksum(frame[:8],endian,state);state=checksum(data,endian,state)
            if state!=struct.unpack('>II',frame[16:24]):fail('WAL_REJECTED')
            if commit:committed=True


def validate_database(conn,*,deadline_seconds=None,max_steps=None):
    deadline_seconds=MAX_SQL_SECONDS if deadline_seconds is None else deadline_seconds
    max_steps=MAX_VM_STEPS if max_steps is None else max_steps
    if type(deadline_seconds) not in (int,float) or not 0<deadline_seconds<=300 or type(max_steps) is not int or not 1<=max_steps<=MAX_VM_STEPS:fail('QUERY_BUDGET')
    started=time.monotonic();steps=0;hit=False;interval=min(1000,max_steps)
    def progress():
        nonlocal steps,hit
        steps+=interval;hit=steps>=max_steps or time.monotonic()-started>=deadline_seconds
        return int(hit)
    def query(sql):
        if time.monotonic()-started>=deadline_seconds:fail('QUERY_BUDGET')
        return [tuple(row) for row in conn.execute(sql).fetchall()]
    conn.enable_load_extension(False)
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,8*1048576);conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,16384);conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN,64)
    conn.set_progress_handler(progress,interval)
    try:
        for sql in ('PRAGMA trusted_schema=OFF','PRAGMA query_only=ON','PRAGMA temp_store=MEMORY','PRAGMA mmap_size=0','PRAGMA cache_size=-8192','PRAGMA busy_timeout=1000'):conn.execute(sql)
        objects=query('SELECT count(*) FROM sqlite_schema')[0][0]
        if objects>MAX_SCHEMA_OBJECTS:fail('SCHEMA_REJECTED')
        if query("SELECT count(*) FROM sqlite_schema WHERE length(name)>128 OR length(tbl_name)>128 OR length(sql)>8192 OR type NOT IN ('table','index') OR upper(sql) LIKE '%VIRTUAL%'")[0][0]:fail('SCHEMA_REJECTED')
        tables={row[0] for row in query("SELECT name FROM sqlite_schema WHERE type='table'")}
        if not {'messages','conversations'}.issubset(tables) or tables-set(SCHEMAS):fail('SCHEMA_REJECTED')
        for table in sorted(tables):
            columns=query('PRAGMA table_xinfo("'+table+'")');expected=SCHEMAS[table]
            if table=='conversations' and len(columns)==7:expected=expected+(('avatar_url','TEXT'),)
            if tuple((row[1],row[2].upper()) for row in columns)!=expected or any(row[6]!=0 for row in columns) or columns[0][5]!=1 or any(row[5] for row in columns[1:]):fail('SCHEMA_REJECTED')
            indexes=query('PRAGMA index_list("'+table+'")')
            for index in indexes:
                if index[4]!=0:fail('SCHEMA_REJECTED')
                name=index[1].replace('"','""');keys=query('PRAGMA index_xinfo("'+name+'")')
                if len(keys)>8 or any(row[1]<0 or row[4]!='BINARY' or row[2] not in {col[0] for col in expected} for row in keys if row[5]):fail('SCHEMA_REJECTED')
        if query('PRAGMA quick_check(1)')!=[('ok',)]:fail('SQLITE_REJECTED')
        messages=query("SELECT count(*),0,sum(CASE WHEN msg_id IS NULL OR typeof(msg_id)!='text' OR length(trim(msg_id))=0 OR length(msg_id)>256 OR instr(msg_id,char(0))>0 THEN 1 ELSE 0 END),sum(CASE WHEN conv_id IS NULL OR typeof(conv_id)!='text' OR length(trim(conv_id))=0 OR length(conv_id)>256 OR instr(conv_id,char(0))>0 THEN 1 ELSE 0 END),sum(CASE WHEN typeof(timestamp)='integer' THEN 1 ELSE 0 END),sum(CASE WHEN timestamp IS NULL THEN 1 ELSE 0 END),sum(CASE WHEN timestamp IS NOT NULL AND typeof(timestamp)!='integer' THEN 1 ELSE 0 END),min(CASE WHEN typeof(timestamp)='integer' THEN timestamp END),max(CASE WHEN typeof(timestamp)='integer' THEN timestamp END) FROM messages")[0]
        conversations=query("SELECT count(*),sum(CASE WHEN conv_id IS NULL OR typeof(conv_id)!='text' OR length(trim(conv_id))=0 OR length(conv_id)>256 OR instr(conv_id,char(0))>0 THEN 1 ELSE 0 END) FROM conversations")[0]
        if messages[0]>MAX_ROWS or conversations[0]>MAX_ROWS:fail('ROW_BUDGET')
        if messages[2] or messages[3] or conversations[1]:fail('IDS_INVALID')
        membership=hashlib.sha256(b'message-id-membership-v1\x00');seen=0;distinct=set()
        cursor=conn.execute('SELECT msg_id,conv_id FROM messages ORDER BY msg_id COLLATE BINARY')
        while True:
            if time.monotonic()-started>=deadline_seconds:fail('QUERY_BUDGET')
            rows=cursor.fetchmany(512)
            if not rows:break
            for identifier,conversation in rows:
                encoded=identifier.encode('utf-8');membership.update(len(encoded).to_bytes(8,'big'));membership.update(encoded);seen+=1
                distinct.add(conversation)
                if len(distinct)>MAX_DISTINCT_CONVERSATIONS:fail('CONVERSATION_BUDGET')
        if seen!=messages[0]:fail('SNAPSHOT_CHANGED')
        schema=query('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name COLLATE BINARY')
        aggregates={'user_table_count':len(tables),'schema_object_count':objects,'messages':{'rows':messages[0],'distinct_conv_id':len(distinct),
            'malformed_msg_id':messages[2] or 0,'malformed_conv_id':messages[3] or 0,'timestamp_integer_rows':messages[4] or 0,
            'timestamp_null_rows':messages[5] or 0,'timestamp_noninteger_rows':messages[6] or 0,
            'timestamp_integer_min':messages[7],'timestamp_integer_max':messages[8],
            'message_id_set_sha256':membership.hexdigest(),'membership_digest_state':'complete'},
            'conversations':{'rows':conversations[0],'malformed_conv_id':conversations[1] or 0}}
        return {'schema_sha256':hashlib.sha256(canonical_json(schema)).hexdigest(),'aggregates':aggregates,
                'page_size':query('PRAGMA page_size')[0][0],'page_count':query('PRAGMA page_count')[0][0]}
    except sqlite3.Error:fail('QUERY_BUDGET' if hit else 'SQLITE_REJECTED')
    finally:conn.set_progress_handler(None,0)


def _destination(path):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(WORKSPACE_ROOT) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',path.name) or path.name in ('.','..'):fail('DESTINATION_REJECTED')
    reject_links(path)
    if not path.parent.is_dir():fail('DESTINATION_PARENT_REQUIRED')
    return path


def _check_manifest(value):
    fields={'format','version','status','created_at_utc','inputs','snapshot','schema_sha256','aggregates','page_size','page_count',
            'source_permission','account_binding','coverage','source_sqlite_opened','source_unchanged','source_shm_copied','physical_purge_claim'}
    def sha(value):return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)
    def file(value):return isinstance(value,dict) and set(value)=={'bytes','sha256'} and type(value['bytes']) is int and 0<=value['bytes']<=MAX_SOURCE_BYTES and sha(value['sha256'])
    if not isinstance(value,dict) or set(value)!=fields or value['format']!=FORMAT or type(value['version']) is not int or value['version']!=1 or value['status']!='complete':fail('MANIFEST_REJECTED')
    if value['source_permission']!='not_assessed' or value['account_binding']!='unverified' or value['coverage']!='unknown' or value['source_sqlite_opened'] is not False or value['source_unchanged'] is not True or value['source_shm_copied'] is not False or value['physical_purge_claim'] is not False:fail('MANIFEST_REJECTED')
    try:
        if len(value['created_at_utc'])!=20 or datetime.strptime(value['created_at_utc'],'%Y-%m-%dT%H:%M:%SZ').strftime('%Y-%m-%dT%H:%M:%SZ')!=value['created_at_utc']:fail('MANIFEST_REJECTED')
    except (ValueError,TypeError):fail('MANIFEST_REJECTED')
    inputs=value['inputs']
    if not isinstance(inputs,dict) or set(inputs)!={'main','wal','shm_present'} or not file(inputs['main']) or inputs['wal'] is not None and not file(inputs['wal']) or type(inputs['shm_present']) is not bool or not file(value['snapshot']) or not sha(value['schema_sha256']):fail('MANIFEST_REJECTED')
    if sum(info['bytes'] for info in (inputs['main'],inputs['wal']) if info)>MAX_SOURCE_BYTES:fail('MANIFEST_REJECTED')
    if type(value['page_size']) is not int or value['page_size'] not in (512,1024,2048,4096,8192,16384,32768,65536) or type(value['page_count']) is not int or not 1<=value['page_count']<=MAX_SOURCE_BYTES//value['page_size'] or value['snapshot']['bytes']!=value['page_size']*value['page_count']:fail('MANIFEST_REJECTED')
    a=value['aggregates'];message_fields={'rows','distinct_conv_id','malformed_msg_id','malformed_conv_id','timestamp_integer_rows','timestamp_null_rows','timestamp_noninteger_rows','timestamp_integer_min','timestamp_integer_max','message_id_set_sha256','membership_digest_state'}
    if not isinstance(a,dict) or set(a)!={'user_table_count','schema_object_count','messages','conversations'} or not isinstance(a['messages'],dict) or set(a['messages'])!=message_fields or not isinstance(a['conversations'],dict) or set(a['conversations'])!={'rows','malformed_conv_id'}:fail('MANIFEST_REJECTED')
    m=a['messages'];c=a['conversations']
    for key in message_fields-{'timestamp_integer_min','timestamp_integer_max','message_id_set_sha256','membership_digest_state'}:
        if type(m[key]) is not int or not 0<=m[key]<=MAX_ROWS:fail('MANIFEST_REJECTED')
    for key in c:
        if type(c[key]) is not int or not 0<=c[key]<=MAX_ROWS:fail('MANIFEST_REJECTED')
    if any(m[key] for key in ('malformed_msg_id','malformed_conv_id')) or c['malformed_conv_id'] or sum(m[key] for key in ('timestamp_integer_rows','timestamp_null_rows','timestamp_noninteger_rows'))!=m['rows'] or m['distinct_conv_id']>m['rows'] or not sha(m['message_id_set_sha256']) or m['membership_digest_state']!='complete':fail('MANIFEST_REJECTED')
    for key in ('timestamp_integer_min','timestamp_integer_max'):
        if m[key] is not None and (type(m[key]) is not int or not -(2**63)<=m[key]<2**63):fail('MANIFEST_REJECTED')
    if (m['timestamp_integer_rows']==0)!=(m['timestamp_integer_min'] is None and m['timestamp_integer_max'] is None):fail('MANIFEST_REJECTED')
    if m['timestamp_integer_rows'] and (m['timestamp_integer_min'] is None or m['timestamp_integer_max'] is None or m['timestamp_integer_min']>m['timestamp_integer_max']):fail('MANIFEST_REJECTED')
    if type(a['user_table_count']) is not int or not 2<=a['user_table_count']<=4 or type(a['schema_object_count']) is not int or not 2<=a['schema_object_count']<=MAX_SCHEMA_OBJECTS:fail('MANIFEST_REJECTED')
    return value


def load_manifest(directory,*,expected_snapshot_sha256=None):
    directory=_destination(directory)
    reject_links(directory)
    if not directory.is_dir() or {path.name for path in directory.iterdir()}!={'snapshot.sqlite3','manifest.json'}:fail('SNAPSHOT_INCOMPLETE')
    require_efs(directory)
    with ExitStack() as stack:
        snapshot=stack.enter_context(LockedRead(directory/'snapshot.sqlite3'));manifest=stack.enter_context(LockedRead(directory/'manifest.json'))
        require_efs(snapshot.path);require_efs(manifest.path)
        if manifest.initial['bytes']>MAX_MANIFEST_BYTES:fail('MANIFEST_BUDGET')
        encoded=b''.join(bytes(block) for block in manifest.blocks())
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:fail('MANIFEST_REJECTED')
                result[key]=value
            return result
        try:value=json.loads(encoded,object_pairs_hook=pairs)
        except (ValueError,UnicodeError,RecursionError):fail('MANIFEST_REJECTED')
        _check_manifest(value)
        if canonical_json(value)!=encoded:fail('MANIFEST_REJECTED')
        if expected_snapshot_sha256 is not None and expected_snapshot_sha256!=value['snapshot']['sha256']:fail('SNAPSHOT_EXPECTATION_MISMATCH')
        if snapshot.hash_again()!=value['snapshot']:fail('SNAPSHOT_CHANGED')
        header=snapshot.read_prefix(100)
        if len(header)!=100 or header[:16]!=b'SQLite format 3\x00' or header[18:20]!=b'\x01\x01':fail('SNAPSHOT_NOT_CLEAN')
        conn=sqlite3.connect(snapshot.path.as_uri()+'?mode=ro&immutable=1',uri=True,isolation_level=None,timeout=1)
        try:
            actual=validate_database(conn)
            if any(value[key]!=actual[key] for key in actual):fail('MANIFEST_MISMATCH')
        finally:conn.close()
        if {path.name for path in directory.iterdir()}!={'snapshot.sqlite3','manifest.json'}:fail('SNAPSHOT_INCOMPLETE')
        return value


def _cleanup_stage(stage,identity):
    if not stage.exists():return
    reject_links(stage);stat=stage.stat()
    if (stat.st_dev,stat.st_ino)!=identity:fail('CLEANUP_REQUIRES_REVIEW')
    allowed={'snapshot.sqlite3','manifest.json','snapshot.sqlite3-wal','snapshot.sqlite3-shm','snapshot.sqlite3-journal'}
    paths=list(stage.iterdir())
    if any(path.name not in allowed for path in paths):fail('CLEANUP_REQUIRES_REVIEW')
    for path in paths:
        reject_links(path)
        if not path.is_file() or path.stat().st_nlink!=1:fail('CLEANUP_REQUIRES_REVIEW')
    for path in paths:path.unlink()
    stage.rmdir()


def build_snapshot(source,destination,*,authorized_readonly=False,fixture_mode=False):
    if authorized_readonly is not True:fail('READ_AUTHORIZATION_REQUIRED')
    if type(fixture_mode) is not bool:fail('FIXTURE_FLAG_INVALID')
    source=Path(source);destination=_destination(destination)
    if not source.is_absolute() or '..' in source.parts:fail('SOURCE_PATH_REJECTED')
    if destination.exists():fail('DESTINATION_EXISTS')
    started=time.monotonic();stage=None;stage_identity=None;published=False;verified=False;manifest=None
    def check():
        if time.monotonic()-started>=MAX_OPERATION_SECONDS:fail('OPERATION_BUDGET')
        if stage is not None and stage.exists():
            if sum(path.stat().st_size for path in stage.iterdir() if path.is_file())+clone_bytes>MAX_PRIVATE_BYTES:fail('PRIVATE_SIZE_LIMIT')
    clone_bytes=0;before=inventory(source)
    with ExitStack() as stack:
        main=stack.enter_context(LockedRead(source))
        wal=stack.enter_context(LockedRead(source.with_name(source.name+'-wal'))) if before['-wal'] else None
        shm=stack.enter_context(LockedRead(source.with_name(source.name+'-shm'))) if before['-shm'] else None
        if inventory(source)!=before:fail('SOURCE_CHANGED')
        _header(main,wal)
        shm_hash=shm.hash_again(check=check) if shm else None
        try:
            with ProtectedScratch(destination.parent) as scratch:
                main_hash=main.copy_and_hash(scratch.database,check=check);wal_hash=wal.copy_and_hash(scratch.database.with_name(scratch.database.name+'-wal'),check=check) if wal else None
                clone_bytes=sum(info['bytes'] for info in (main_hash,wal_hash) if info)
                if _file_hash(scratch.database,check)!=main_hash or wal_hash and _file_hash(scratch.database.with_name(scratch.database.name+'-wal'),check)!=wal_hash:fail('PRIVATE_COPY_CHANGED')
                if wal:_wal_integrity(scratch.database.with_name(scratch.database.name+'-wal'),_header(main,wal),check)
                _verify_efs(scratch.database)
                stage=destination.parent/('.large-snapshot-'+uuid.uuid4().hex);stage.mkdir(mode=0o700)
                stage_identity=(stage.stat().st_dev,stage.stat().st_ino);require_efs(stage,encrypt=True)
                clean=stage/'snapshot.sqlite3'
                with clean.open('xb') as stream:
                    require_efs(clean);marker=os.urandom(32);stream.write(marker);stream.flush();os.fsync(stream.fileno())
                with clean.open('r+b') as stream:
                    if stream.read()!=marker:fail('PROTECTED_STORAGE_UNAVAILABLE')
                    stream.seek(0);stream.truncate();stream.flush();os.fsync(stream.fileno())
                src=sqlite3.connect(scratch.database.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=1)
                dst=sqlite3.connect(clean.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=1)
                try:
                    src.execute('BEGIN');source_summary=validate_database(src)
                    _verify_efs(scratch.database)
                    dst.execute('PRAGMA synchronous=FULL');dst.execute('PRAGMA temp_store=MEMORY')
                    src.backup(dst,pages=256,progress=lambda status,remaining,total:check(),sleep=0.01)
                    src.rollback();dst.execute('PRAGMA journal_mode=DELETE')
                    summary=validate_database(dst)
                    if summary!=source_summary:fail('BACKUP_MISMATCH')
                finally:dst.close();src.close();_verify_efs(clean);_verify_efs(scratch.database)
                if {path.name for path in stage.iterdir()}!={'snapshot.sqlite3'}:fail('SNAPSHOT_NOT_CLEAN')
                snapshot_hash=_file_hash(clean,check)
                if main.hash_again(check=check)!=main_hash or wal and wal.hash_again(check=check)!=wal_hash or shm and shm.hash_again(check=check)!=shm_hash or inventory(source)!=before:fail('SOURCE_CHANGED')
                verified=True
                manifest={'format':FORMAT,'version':1,'status':'complete','created_at_utc':datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'inputs':{'main':main_hash,'wal':wal_hash,'shm_present':shm is not None},'snapshot':snapshot_hash,**summary,
                    'source_permission':'not_assessed','account_binding':'unverified','coverage':'unknown','source_sqlite_opened':False,
                    'source_unchanged':True,'source_shm_copied':False,'physical_purge_claim':False}
                _check_manifest(manifest);encoded=canonical_json(manifest)
                if len(encoded)>MAX_MANIFEST_BYTES:fail('MANIFEST_BUDGET')
                with (stage/'manifest.json').open('xb') as output:
                    require_efs(stage/'manifest.json');output.write(encoded);output.flush();os.fsync(output.fileno())
                load_manifest(stage,expected_snapshot_sha256=snapshot_hash['sha256']);check()
                if destination.exists():fail('DESTINATION_EXISTS')
                try:os.rename(stage,destination)
                except FileExistsError:fail('DESTINATION_EXISTS')
                published=True
            if inventory(source)!=before:verified=False;fail('SOURCE_CHANGED')
            return {'format':FORMAT,'stage':'complete','published':True,'source_unchanged':True,'cleanup_complete':True,'manifest':manifest}
        except Exception as exc:
            cleanup_ok=True
            if stage is not None and stage.exists():
                try:_cleanup_stage(stage,stage_identity)
                except Exception:cleanup_ok=False
            if published or not cleanup_ok:
                return {'format':FORMAT,'stage':'requires_review','published':published,'source_unchanged':verified,
                        'cleanup_complete':False,'code':exc.code if isinstance(exc,SnapshotError) else 'SNAPSHOT_UNAVAILABLE','manifest':manifest if published else None}
            if isinstance(exc,SnapshotError):raise
            if isinstance(exc,sqlite3.Error):fail('SQLITE_REJECTED')
            fail('PROTECTED_STORAGE_UNAVAILABLE')


def main():
    parser=argparse.ArgumentParser(description='Explicit authorized snapshot; no source SQLite access.')
    parser.add_argument('--authorized-readonly',action='store_true',required=True)
    parser.add_argument('--fictional',action='store_true')
    parser.add_argument('--source',required=True);parser.add_argument('--destination',required=True)
    try:
        args=parser.parse_args();result=build_snapshot(args.source,args.destination,authorized_readonly=args.authorized_readonly,fixture_mode=args.fictional)
        print(json.dumps(result));return 0 if result['stage']=='complete' else 2
    except Exception as exc:
        print(json.dumps({'format':FORMAT,'stage':'refused','published':False,'code':exc.code if isinstance(exc,SnapshotError) else 'SNAPSHOT_UNAVAILABLE'}));return 1


if __name__=='__main__':sys.exit(main())
