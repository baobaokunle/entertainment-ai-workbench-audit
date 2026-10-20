"""Explicit independent EFS export staging. No business DB, HTTP, or expiry.

Checksums detect corruption; they are not signatures or account provenance.
Completion independently replays the pinned snapshot before changing state.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime,timezone
import hashlib,json,math,os,re,sqlite3,time,uuid,threading
from pathlib import Path
from .backup_scratch import reject_links,require_efs
from .common import canonical_json

WORKSPACE=Path(__file__).resolve().parents[2]
FORMAT='dfa-export-candidates-1'
MAX_BYTES=4*1024**3
MAX_ROWS=1000000
MAX_PAGE_BYTES=32*1024**2
MAX_FIELD_CHARS=256000
MEMBERSHIP_SEED=b'message-id-membership-v1\x00'
ROOT_SEED='export-candidate-pages-v1'
_WRITERS=set()
_WRITER_LOCK=threading.Lock()
DDL={
'candidate_meta':'CREATE TABLE candidate_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)',
'candidate_page':'CREATE TABLE candidate_page (page_index INTEGER PRIMARY KEY, row_count INTEGER NOT NULL, input_digest TEXT NOT NULL, record_digest TEXT NOT NULL, first_id TEXT, last_id TEXT)',
'candidate_record':'CREATE TABLE candidate_record (page_index INTEGER NOT NULL, ordinal INTEGER NOT NULL, source_key TEXT NOT NULL UNIQUE, version_digest TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(page_index,ordinal), FOREIGN KEY(page_index) REFERENCES candidate_page(page_index))',
}
RECORD_FIELDS={'source_key','account_namespace','platform_message_id','platform_conversation_id','source_kind','revision_state','observations','versions','key_origin'}
VERSION_FIELDS={'original_body','original_raw_data','original_ref_msg','original_timestamp','timestamp','platform_sender_id','msg_type','direction','display_body','display_visibility_hint','reference_display_body','platform_reference_ids','display_parse_state','identity','delivery','body_revision'}
PROFILE_FIELDS={'snapshot_sha256','schema_sha256','account_namespace','source_kind','expected_rows','expected_membership_sha256','page_size','max_bytes'}
META_FIELDS=PROFILE_FIELDS|{'format','store_id','created_at_utc','state','revision','pages','rows','content_root'}

class CandidateError(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)
def fail(code):raise CandidateError(code)
def sha(value):return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None
def hashed(value):return hashlib.sha256(canonical_json(value)).hexdigest()
def bounded_text(value,maximum,*,nullable=False,identifier=False):
    if nullable and value is None:return
    if type(value) is not str or len(value)>maximum or '\0' in value:fail('CANDIDATE_FIELD_INVALID')
    if identifier and (not value.strip() or value!=value.strip() or re.search('[\x00-\x1f]',value)):fail('CANDIDATE_FIELD_INVALID')
def add_id(digest,value):
    raw=value.encode('utf-8');digest.update(len(raw).to_bytes(8,'big'));digest.update(raw)
def root_next(root,index,inputs,records):return hashed([root,index,inputs,records])
def parse_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:fail('CANDIDATE_CORRUPT')
            result[key]=value
        return result
    try:value=json.loads(raw,object_pairs_hook=pairs)
    except (ValueError,RecursionError,UnicodeError):fail('CANDIDATE_CORRUPT')
    try:
        if canonical_json(value).decode()!=raw:fail('CANDIDATE_CORRUPT')
    except (ValueError,UnicodeError,TypeError):fail('CANDIDATE_CORRUPT')
    return value
def check_profile(p):
    if type(p) is not dict or set(p)!=PROFILE_FIELDS or not all(sha(p[k]) for k in ('snapshot_sha256','schema_sha256','expected_membership_sha256')):fail('CANDIDATE_PROFILE_INVALID')
    bounded_text(p['account_namespace'],256,identifier=True)
    if p['source_kind'] not in ('fictional','unverified_export') or type(p['expected_rows']) is not int or not 0<=p['expected_rows']<=MAX_ROWS or type(p['page_size']) is not int or not 1<=p['page_size']<=2000 or type(p['max_bytes']) is not int or not 65536<=p['max_bytes']<=MAX_BYTES:fail('CANDIDATE_PROFILE_INVALID')
def record_raw(record,profile):
    if type(record) is not dict or set(record)!=RECORD_FIELDS:fail('CANDIDATE_RECORD_INVALID')
    for key in ('platform_message_id','platform_conversation_id'):bounded_text(record[key],256,identifier=True)
    if record['account_namespace']!=profile['account_namespace'] or record['source_kind']!=profile['source_kind'] or record['key_origin']!='platform_claim' or record['revision_state']!='single_candidate' or type(record['observations']) is not int or record['observations']!=1:fail('CANDIDATE_RECORD_INVALID')
    expected_key=json.dumps([profile['account_namespace'],record['platform_conversation_id'],record['platform_message_id']],ensure_ascii=False,separators=(',',':'))
    if record['source_key']!=expected_key or type(record['versions']) is not list or len(record['versions'])!=1:fail('CANDIDATE_RECORD_INVALID')
    v=record['versions'][0]
    if type(v) is not dict or set(v)!=VERSION_FIELDS:fail('CANDIDATE_RECORD_INVALID')
    for key in ('original_body','original_raw_data','original_ref_msg','display_body','reference_display_body'):bounded_text(v[key],MAX_FIELD_CHARS,nullable=True)
    bounded_text(v['platform_sender_id'],512,nullable=True,identifier=True)
    stamp=v['original_timestamp']
    if stamp is not None and (type(stamp) is not str or not re.fullmatch(r'-?\d{1,20}',stamp) or not -(2**63)<=int(stamp)<2**63):fail('CANDIDATE_RECORD_INVALID')
    if v['msg_type'] is not None and (type(v['msg_type']) is not int or abs(v['msg_type'])>9007199254740991):fail('CANDIDATE_RECORD_INVALID')
    if v['direction']!='unknown' or v['identity']!={'state':'unverified','person_key':None} or v['delivery']!={'state':'unknown','send_success':None,'manually_sent':None,'operator_key':None,'attribution':'unknown'}:fail('CANDIDATE_AUTHORITY_REJECTED')
    t=v['timestamp']
    if type(t) is not dict or set(t)!={'state','value','unit','verified'} or t['state'] not in ('parsed','unknown') or t['unit']!='unix_seconds' or t['verified'] is not False:fail('CANDIDATE_AUTHORITY_REJECTED')
    expected_time=float(stamp) if stamp is not None and re.fullmatch(r'\d+',stamp) else float('nan')
    known=math.isfinite(expected_time) and 0<expected_time<=253402300799
    if (known and (t['state']!='parsed' or type(t['value']) not in (int,float) or t['value']!=expected_time)) or (not known and (t['state']!='unknown' or t['value'] is not None)):fail('CANDIDATE_RECORD_INVALID')
    if v['display_visibility_hint'] is not None and type(v['display_visibility_hint']) is not bool or v['display_parse_state'] not in ('unknown','parsed_unverified'):fail('CANDIDATE_RECORD_INVALID')
    if type(v['platform_reference_ids']) is not list or len(v['platform_reference_ids'])>100:fail('CANDIDATE_RECORD_INVALID')
    for key in v['platform_reference_ids']:bounded_text(key,512,identifier=True)
    signature=[v['original_body'],v['original_raw_data'],v['original_ref_msg'],stamp,v['platform_sender_id'],v['msg_type'],'unknown']
    revision=hashlib.sha256(json.dumps(signature,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    if v['body_revision']!=revision:fail('CANDIDATE_RECORD_DIGEST_MISMATCH')
    return {'msg_id':record['platform_message_id'],'conv_id':record['platform_conversation_id'],'sender_uid':v['platform_sender_id'],'content':v['original_body'],'msg_type':v['msg_type'],'timestamp':stamp,'raw_data':v['original_raw_data'],'ref_msg':v['original_ref_msg']}

class _Lease:
    """Deny DELETE/replacement; mutex serializes this tool's writers.

    Native/foreign SQLite writers are not authenticated away. Change detection,
    content checks, and independent snapshot replay are separate safeguards.
    """
    def __init__(self,path,*,directory=False,writable=False):self.path=path;self.directory=directory;self.writable=writable;self.handle=None;self.mutex=None;self.writer_key=None
    def __enter__(self):
        if os.name!='nt':fail('CANDIDATE_WINDOWS_REQUIRED')
        k=ctypes.WinDLL('kernel32',use_last_error=True);self.k=k
        k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];k.CreateFileW.restype=wintypes.HANDLE
        k.CloseHandle.argtypes=[wintypes.HANDLE];k.CloseHandle.restype=wintypes.BOOL
        k.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR];k.CreateMutexW.restype=wintypes.HANDLE
        k.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];k.WaitForSingleObject.restype=wintypes.DWORD
        k.ReleaseMutex.argtypes=[wintypes.HANDLE];k.ReleaseMutex.restype=wintypes.BOOL
        if self.writable and not self.directory:
            stat=self.path.stat();writer_key=(stat.st_dev,stat.st_ino)
            with _WRITER_LOCK:
                if writer_key in _WRITERS:fail('CANDIDATE_WRITER_IN_USE')
                _WRITERS.add(writer_key);self.writer_key=writer_key
            name='Local\\DFAExportCandidates'+hashlib.sha256(str(writer_key).encode()).hexdigest()
            self.mutex=k.CreateMutexW(None,False,name)
            if not self.mutex or k.WaitForSingleObject(self.mutex,0) not in (0,0x80):
                if self.mutex:k.CloseHandle(self.mutex);self.mutex=None
                with _WRITER_LOCK:_WRITERS.discard(self.writer_key)
                self.writer_key=None
                fail('CANDIDATE_WRITER_IN_USE')
        self.handle=k.CreateFileW(str(self.path),0x80000000,3 if self.writable or self.directory else 1,None,3,0x02000000 if self.directory else 0x00200000,None)
        if self.handle==ctypes.c_void_p(-1).value:self.handle=None;self.__exit__();fail('CANDIDATE_LEASE_UNAVAILABLE')
        return self
    def __exit__(self,*args):
        if self.handle is not None:self.k.CloseHandle(self.handle);self.handle=None
        if self.mutex is not None:self.k.ReleaseMutex(self.mutex);self.k.CloseHandle(self.mutex);self.mutex=None
        if self.writer_key is not None:
            with _WRITER_LOCK:_WRITERS.discard(self.writer_key)
            self.writer_key=None

class ExportCandidateStore:
    def __init__(self,directory,*,expected_profile=None):
        self.directory=Path(directory);self.path=self.directory/'candidates.sqlite3';self.conn=None;self.leases=[];self.writable=False;self.closed=False;self.meta=None;self._stamp=None;self._dv=None;self.poisoned=False
        if expected_profile is not None:check_profile(expected_profile)
        self.expected_profile=dict(expected_profile) if expected_profile is not None else None
        if not self.directory.is_absolute() or '..' in self.directory.parts or not self.directory.is_relative_to(WORKSPACE) or not re.fullmatch('[A-Za-z0-9_.-]{1,80}',self.directory.name):fail('CANDIDATE_PATH_REJECTED')
    @classmethod
    def create(cls,directory,*,snapshot_sha256,schema_sha256,account_namespace,source_kind,expected_rows,expected_membership_sha256,page_size,max_bytes=MAX_BYTES,fixture_mode=False):
        profile={k:v for k,v in locals().copy().items() if k in PROFILE_FIELDS};check_profile(profile);obj=cls(directory,expected_profile=profile)
        if type(fixture_mode) is not bool:fail('CANDIDATE_PROFILE_INVALID')
        reject_links(obj.directory)
        if not obj.directory.parent.is_dir():fail('CANDIDATE_PARENT_REQUIRED')
        require_efs(obj.directory.parent)
        if obj.directory.exists():fail('CANDIDATE_EXISTS')
        obj.directory.mkdir(mode=0o700);require_efs(obj.directory,encrypt=True)
        with obj.path.open('xb') as stream:
            require_efs(obj.path);marker=os.urandom(32);stream.write(marker);stream.flush();os.fsync(stream.fileno())
        with obj.path.open('r+b') as stream:
            if stream.read()!=marker:fail('CANDIDATE_STORAGE_UNAVAILABLE')
            stream.seek(0);stream.truncate();stream.flush();os.fsync(stream.fileno())
        conn=sqlite3.connect(obj.path,isolation_level=None)
        try:
            conn.execute('PRAGMA journal_mode=DELETE');conn.execute('PRAGMA synchronous=FULL');conn.execute('BEGIN IMMEDIATE')
            for sql in DDL.values():conn.execute(sql)
            meta={**profile,'format':FORMAT,'store_id':uuid.uuid4().hex,'created_at_utc':datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),'state':'staging','revision':0,'pages':0,'rows':0,'content_root':hashed(ROOT_SEED)}
            conn.execute('INSERT INTO candidate_meta VALUES(1,?)',(canonical_json(meta).decode(),));conn.commit()
        finally:conn.close()
        return obj.open(writable=True)
    def _paths(self):
        reject_links(self.directory)
        if not self.directory.is_dir():fail('CANDIDATE_UNINITIALIZED')
        names={p.name for p in self.directory.iterdir()}
        if names-{'candidates.sqlite3','candidates.sqlite3-journal'} or 'candidates.sqlite3' not in names:fail('CANDIDATE_FILE_SET_INVALID')
        require_efs(self.directory)
        for p in self.directory.iterdir():
            reject_links(p)
            if not p.is_file() or p.stat().st_nlink!=1:fail('CANDIDATE_PATH_REJECTED')
            require_efs(p)
        s=self.path.stat();d=self.directory.stat()
        if s.st_size>MAX_BYTES:fail('CANDIDATE_STORAGE_BUDGET')
        return (d.st_dev,d.st_ino,s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
    def open(self,*,writable=False):
        if self.conn is not None or self.closed:fail('CANDIDATE_ALREADY_OPEN')
        if writable and self.expected_profile is None:fail('CANDIDATE_EXTERNAL_PROFILE_REQUIRED')
        self._paths();self.writable=writable
        try:
            self.leases=[_Lease(self.directory,directory=True,writable=writable).__enter__()]
            self.leases.append(_Lease(self.path,writable=writable).__enter__());self.identity=self._paths()[:4]
            self.conn=sqlite3.connect(self.path.as_uri()+('?mode=rw' if writable else '?mode=ro'),uri=True,isolation_level=None,timeout=1)
            self.conn.enable_load_extension(False);self.conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,MAX_PAGE_BYTES);self.conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,16384)
            for sql in ('PRAGMA trusted_schema=OFF','PRAGMA foreign_keys=ON','PRAGMA temp_store=MEMORY','PRAGMA mmap_size=0','PRAGMA cache_size=-8192','PRAGMA synchronous=FULL'):self.conn.execute(sql)
            if writable:self.conn.execute('PRAGMA max_page_count='+str(self.expected_profile['max_bytes']//self.conn.execute('PRAGMA page_size').fetchone()[0]))
            if self.conn.execute('PRAGMA journal_mode').fetchone()!=('delete',):fail('CANDIDATE_SCHEMA_INVALID')
            self._validate_all();return self
        except BaseException:self.close();raise
    def _meta(self):
        row=self.conn.execute('SELECT singleton,value FROM candidate_meta').fetchall()
        if len(row)!=1 or row[0][0]!=1 or len(row[0][1].encode())>4096:fail('CANDIDATE_CORRUPT')
        m=parse_json(row[0][1])
        if type(m) is not dict or set(m)!=META_FIELDS:fail('CANDIDATE_CORRUPT')
        check_profile({k:m[k] for k in PROFILE_FIELDS})
        if self.expected_profile is not None and any(m[k]!=self.expected_profile[k] for k in PROFILE_FIELDS):fail('CANDIDATE_PROFILE_MISMATCH')
        if m['format']!=FORMAT or type(m['store_id']) is not str or not re.fullmatch('[0-9a-f]{32}',m['store_id']) or m['state'] not in ('staging','ready_for_mapping') or not sha(m['content_root']):fail('CANDIDATE_CORRUPT')
        try:
            if datetime.strptime(m['created_at_utc'],'%Y-%m-%dT%H:%M:%SZ').strftime('%Y-%m-%dT%H:%M:%SZ')!=m['created_at_utc']:fail('CANDIDATE_CORRUPT')
        except (ValueError,TypeError):fail('CANDIDATE_CORRUPT')
        for k in ('pages','rows','revision'):
            if type(m[k]) is not int or not 0<=m[k]<=MAX_ROWS+1:fail('CANDIDATE_CORRUPT')
        if m['rows']>m['expected_rows'] or m['revision']!=m['pages']+int(m['state']=='ready_for_mapping'):fail('CANDIDATE_CORRUPT')
        return m
    def _checkpoint(self):self._stamp=self._paths();self._dv=self.conn.execute('PRAGMA data_version').fetchone()[0]
    def _transaction_unchanged(self):
        if self._paths()!=self._stamp or self.conn.execute('PRAGMA data_version').fetchone()[0]!=self._dv:self.poisoned=True;fail('CANDIDATE_CHANGED')
    def _available(self,*,write=False):
        if self.conn is None or self.closed or self.poisoned:fail('CANDIDATE_NOT_OPEN')
        stamp=self._paths()
        if stamp[:4]!=self.identity:fail('CANDIDATE_REPLACED')
        if stamp!=self._stamp or self.conn.execute('PRAGMA data_version').fetchone()[0]!=self._dv:self.poisoned=True;fail('CANDIDATE_CHANGED')
        if write and not self.writable:fail('CANDIDATE_READ_ONLY')
    def _validate_all(self):
        before=self._paths()
        if hasattr(self,'identity') and before[:4]!=self.identity:fail('CANDIDATE_REPLACED')
        started=time.monotonic();self.conn.set_progress_handler(lambda:int(time.monotonic()-started>120),1000)
        try:
            if self.conn.execute('SELECT count(*) FROM sqlite_schema').fetchone()[0]!=5:fail('CANDIDATE_SCHEMA_INVALID')
            objects=self.conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema').fetchall()
            expected={( 'table',k,k,v) for k,v in DDL.items()}|{('index','sqlite_autoindex_candidate_record_1','candidate_record',None),('index','sqlite_autoindex_candidate_record_2','candidate_record',None)}
            if len(objects)!=5 or set(objects)!=expected or self.conn.execute('PRAGMA quick_check(1)').fetchall()!=[('ok',)]:fail('CANDIDATE_SCHEMA_INVALID')
            self.meta=self._meta()
            actual_count=self.conn.execute('SELECT count(*) FROM candidate_record').fetchone()[0]
            if before[4]>self.meta['max_bytes'] or actual_count>MAX_ROWS:fail('CANDIDATE_STORAGE_BUDGET')
            if self.meta['pages']>max(1,(self.meta['expected_rows']+self.meta['page_size']-1)//self.meta['page_size']):fail('CANDIDATE_CORRUPT')
            digest=hashlib.sha256(MEMBERSHIP_SEED);last=None;root=hashed(ROOT_SEED);total=0;pages=0
            for receipt in self.conn.execute('SELECT * FROM candidate_page ORDER BY page_index'):
                pages+=1
                if receipt[0]!=pages:fail('CANDIDATE_CORRUPT')
                records=self._records(pages);raw=self._raw_page(records,self.meta,last,total)
                if receipt[1:]!=(len(raw),hashed(raw),hashed(records),raw[0]['msg_id'] if raw else None,raw[-1]['msg_id'] if raw else None):fail('CANDIDATE_RECEIPT_MISMATCH')
                for r in raw:add_id(digest,r['msg_id'])
                last=raw[-1]['msg_id'] if raw else last;total+=len(raw);root=root_next(root,pages,receipt[2],receipt[3])
            if actual_count!=total or (pages,total,root)!=(self.meta['pages'],self.meta['rows'],self.meta['content_root']):fail('CANDIDATE_CORRUPT')
            if self.meta['state']=='ready_for_mapping' and (total!=self.meta['expected_rows'] or digest.hexdigest()!=self.meta['expected_membership_sha256']):fail('CANDIDATE_CORRUPT')
            self.digest=digest;self.last_id=last
        except sqlite3.Error:fail('CANDIDATE_CORRUPT')
        finally:self.conn.set_progress_handler(None,0)
        if self._paths()!=before:fail('CANDIDATE_CHANGED')
        self._checkpoint()
    def _records(self,index):
        count,size=self.conn.execute('SELECT count(*),coalesce(sum(length(CAST(record_json AS BLOB))),0) FROM candidate_record WHERE page_index=?',(index,)).fetchone()
        if count>2000 or size>MAX_PAGE_BYTES:fail('CANDIDATE_PAGE_BUDGET')
        rows=self.conn.execute('SELECT ordinal,source_key,version_digest,record_json FROM candidate_record WHERE page_index=? ORDER BY ordinal',(index,)).fetchall();records=[]
        for ordinal,key,version,raw in rows:
            record=parse_json(raw)
            record_raw(record,self.meta)
            if ordinal!=len(records) or key!=record.get('source_key') or version!=record.get('versions',[{}])[0].get('body_revision'):fail('CANDIDATE_CORRUPT')
            records.append(record)
        return records
    def _raw_page(self,records,profile,last,total):
        if type(records) is not list or len(records)!=min(profile['page_size'],profile['expected_rows']-total):fail('CANDIDATE_PAGE_BUDGET')
        result=[];charged=4096
        for record in records:
            raw=record_raw(record,profile)
            charged+=len(canonical_json(record))+1
            if charged>MAX_PAGE_BYTES:fail('CANDIDATE_PAGE_BUDGET')
            if last is not None and raw['msg_id'].encode()<=last.encode():fail('CANDIDATE_KEYSET_INVALID')
            last=raw['msg_id'];result.append(raw)
        return result
    def status(self):
        if self.conn is None:
            if not self.directory.exists():return {'format':FORMAT,'state':'uninitialized','persisted':False,'business_import_committed':False}
            with ExportCandidateStore(self.directory,expected_profile=self.expected_profile).open() as reader:return reader.status()
        self._available()
        return {**self.meta,'persisted':True,'business_import_committed':False,'source_permission':'not_assessed','source_approved':False,'account_binding':'unverified','coverage':'unknown','physical_purge_claim':False,'external_profile_verified':self.expected_profile is not None,'complete':self.meta['state']=='ready_for_mapping'}
    def append_page(self,page_index,records,*,input_digest,expected_revision):
        if getattr(self,'_review_session',None) is not None:fail('CANDIDATE_REVIEW_ACTIVE')
        self._available(write=True)
        if self.conn.in_transaction:fail('CANDIDATE_OPERATION_IN_PROGRESS')
        if type(page_index) is not int or not 1<=page_index<=MAX_ROWS+1 or not sha(input_digest) or type(expected_revision) is not int or expected_revision!=self.meta['revision']:fail('CANDIDATE_REVISION_STALE')
        if page_index<=self.meta['pages']:
            old=self.conn.execute('SELECT input_digest,record_digest,row_count FROM candidate_page WHERE page_index=?',(page_index,)).fetchone()
            previous=self.conn.execute('SELECT last_id FROM candidate_page WHERE page_index=?',(page_index-1,)).fetchone()
            duplicate_raw=self._raw_page(records,self.meta,previous[0] if previous else None,(page_index-1)*self.meta['page_size'])
            if hashed(duplicate_raw)!=input_digest:fail('CANDIDATE_INPUT_DIGEST_MISMATCH')
            if old[:2]!=(input_digest,hashed(records)):fail('CANDIDATE_PAGE_CONFLICT')
            return {'status':'duplicate','page_index':page_index,'revision':self.meta['revision'],'rows':old[2],'input_digest':old[0],'record_digest':old[1],'persisted':True}
        if self.meta['state']!='staging' or page_index!=self.meta['pages']+1 or self.meta['pages'] and self.meta['rows']==self.meta['expected_rows']:fail('CANDIDATE_PAGE_SEQUENCE_INVALID')
        raw=self._raw_page(records,self.meta,self.last_id,self.meta['rows'])
        if hashed(raw)!=input_digest:fail('CANDIDATE_INPUT_DIGEST_MISMATCH')
        record_digest=hashed(records);digest=self.digest.copy()
        for r in raw:add_id(digest,r['msg_id'])
        new={**self.meta,'pages':page_index,'rows':self.meta['rows']+len(raw),'revision':self.meta['revision']+1,'content_root':root_next(self.meta['content_root'],page_index,input_digest,record_digest)}
        try:
            self.conn.execute('BEGIN IMMEDIATE')
            self._transaction_unchanged()
            if self._meta()!=self.meta:fail('CANDIDATE_REVISION_STALE')
            self.conn.execute('INSERT INTO candidate_page VALUES(?,?,?,?,?,?)',(page_index,len(raw),input_digest,record_digest,raw[0]['msg_id'] if raw else None,raw[-1]['msg_id'] if raw else None))
            self.conn.executemany('INSERT INTO candidate_record VALUES(?,?,?,?,?)',((page_index,index,r['source_key'],r['versions'][0]['body_revision'],canonical_json(r).decode()) for index,r in enumerate(records)))
            if self.conn.execute('PRAGMA page_count').fetchone()[0]*self.conn.execute('PRAGMA page_size').fetchone()[0]>new['max_bytes']:fail('CANDIDATE_STORAGE_BUDGET')
            self._paths();self.conn.execute('UPDATE candidate_meta SET value=? WHERE singleton=1',(canonical_json(new).decode(),));self.conn.commit()
        except BaseException as exc:
            if self.conn.in_transaction:self.conn.rollback()
            if not self.poisoned:self._checkpoint()
            if isinstance(exc,sqlite3.Error):fail('CANDIDATE_STORAGE_BUDGET' if getattr(exc,'sqlite_errorcode',None)==sqlite3.SQLITE_FULL else 'CANDIDATE_STORAGE_UNAVAILABLE')
            raise
        self.meta=new;self.digest=digest;self.last_id=raw[-1]['msg_id'] if raw else self.last_id;self._checkpoint()
        return {'status':'appended','page_index':page_index,'revision':new['revision'],'rows':len(raw),'input_digest':input_digest,'record_digest':record_digest,'persisted':True}
    def page_preview(self,page_index):
        self._available()
        if type(page_index) is not int or not 1<=page_index<=self.meta['pages']:fail('CANDIDATE_PAGE_NOT_FOUND')
        return {'format':'douyin-chat-export.message_rows.v1','status':'offline_preview','persisted':False,'account_namespace':self.meta['account_namespace'],'source_kind':self.meta['source_kind'],'account_binding':'external_unverified','coverage':{'state':'unknown'},'capabilities':{'source':'unverified','adapter':False,'send':False},'records':self._records(page_index)}
    def page_quote(self,page_index):
        self._available()
        if type(page_index) is not int or not 1<=page_index<=self.meta['pages']:fail('CANDIDATE_PAGE_NOT_FOUND')
        receipt=self.conn.execute('SELECT input_digest,record_digest,row_count FROM candidate_page WHERE page_index=?',(page_index,)).fetchone()
        value={'store_id':self.meta['store_id'],'revision':self.meta['revision'],'state':self.meta['state'],'snapshot_sha256':self.meta['snapshot_sha256'],'content_root':self.meta['content_root'],'page_index':page_index,'input_digest':receipt[0],'record_digest':receipt[1],'rows':receipt[2]}
        return {**value,'quote_hash':hashed(value)}
    @contextmanager
    def locked_page_preview(self,page_index):
        self._available()
        if self.conn.in_transaction:fail('CANDIDATE_OPERATION_IN_PROGRESS')
        if type(page_index) is not int or not 1<=page_index<=self.meta['pages']:fail('CANDIDATE_PAGE_NOT_FOUND')
        try:
            self.conn.execute('BEGIN IMMEDIATE' if self.writable else 'BEGIN')
            self._transaction_unchanged()
            before=self._meta()
            records=self._records(page_index)
            previous=self.conn.execute('SELECT last_id FROM candidate_page WHERE page_index=?',(page_index-1,)).fetchone()
            raw=self._raw_page(records,before,previous[0] if previous else None,(page_index-1)*before['page_size'])
            receipt=self.conn.execute('SELECT row_count,input_digest,record_digest,first_id,last_id FROM candidate_page WHERE page_index=?',(page_index,)).fetchone()
            if receipt!=(len(raw),hashed(raw),hashed(records),raw[0]['msg_id'] if raw else None,raw[-1]['msg_id'] if raw else None):fail('CANDIDATE_RECEIPT_MISMATCH')
            yield {'status':self.status(),'preview':self.page_preview(page_index),'quote':self.page_quote(page_index),'input_digest':receipt[1],'record_digest':receipt[2]}
            self._transaction_unchanged()
            if self._meta()!=before:fail('CANDIDATE_CHANGED')
        finally:
            if self.conn.in_transaction:self.conn.rollback()
    def finalize(self,snapshot_dir,*,expected_revision):
        if getattr(self,'_review_session',None) is not None:fail('CANDIDATE_REVIEW_ACTIVE')
        self._available(write=True)
        if self.conn.in_transaction:fail('CANDIDATE_OPERATION_IN_PROGRESS')
        if type(expected_revision) is not int or expected_revision!=self.meta['revision']:fail('CANDIDATE_REVISION_STALE')
        self._validate_all()
        if self.meta['rows']!=self.meta['expected_rows'] or self.digest.hexdigest()!=self.meta['expected_membership_sha256']:fail('CANDIDATE_INCOMPLETE')
        from tools.large_export_reader import LargeExportReader
        profile=dict(self.meta);cursor=None;pages=0
        with LargeExportReader(snapshot_dir,expected_snapshot_sha256=profile['snapshot_sha256'],account_namespace=profile['account_namespace'],source_kind=profile['source_kind']) as reader:
            if reader.summary()['schema_sha256']!=profile['schema_sha256']:fail('CANDIDATE_SNAPSHOT_MISMATCH')
            while True:
                page=reader.page(cursor=cursor,limit=profile['page_size']);pages+=1
                receipt=self.conn.execute('SELECT input_digest,row_count FROM candidate_page WHERE page_index=?',(pages,)).fetchone()
                if receipt!=(hashed(page['rows']),len(page['rows'])):fail('CANDIDATE_SOURCE_MISMATCH')
                if page['complete']:break
                cursor=page['next_cursor']
            if pages!=profile['pages'] or reader.count!=profile['expected_rows'] or reader.digest.hexdigest()!=profile['expected_membership_sha256'] or not reader.final_verified:fail('CANDIDATE_SNAPSHOT_MISMATCH')
        # Reader context final verification and clean close precede ready.
        self._validate_all()
        if self.meta!=profile:fail('CANDIDATE_CHANGED')
        if self.meta['state']=='ready_for_mapping':return self.status()
        try:
            self.conn.execute('BEGIN IMMEDIATE')
            self._transaction_unchanged();self._validate_all()
            if self._meta()!=profile:fail('CANDIDATE_CHANGED')
            new={**profile,'state':'ready_for_mapping','revision':profile['revision']+1}
            self.conn.execute('UPDATE candidate_meta SET value=? WHERE singleton=1',(canonical_json(new).decode(),));self.conn.commit()
        except BaseException:
            if self.conn.in_transaction:self.conn.rollback()
            raise
        self.meta=new;self._checkpoint();return self.status()
    def review_session(self,snapshot_dir,*,scope_key,permission_check,max_seconds=600,clock=None,fixture_mode=False):
        return CandidateReviewSession(self,snapshot_dir,scope_key=scope_key,permission_check=permission_check,max_seconds=max_seconds,clock=clock,fixture_mode=fixture_mode)
    def close(self):
        error=None
        review=getattr(self,'_review_session',None)
        if review is not None and not review.closed:
            review._poison()
            try:review._release()
            except BaseException as caught:error=caught
        try:
            if self.conn is not None:self.conn.close()
        except BaseException as caught:
            if error is None:error=caught
        finally:self.conn=None
        for lease in reversed(self.leases):
            try:lease.__exit__()
            except BaseException as caught:
                if error is None:error=caught
        self.leases=[];self.closed=True
        if error is not None:raise error
    def __enter__(self):return self if self.conn is not None else self.open()
    def __exit__(self,*args):self.close()

ExportCandidatesStore=ExportCandidateStore

MAX_REVIEW_PROOF_BYTES=32*1024**2
class CandidateReviewSession:
    """One independently verified source replay with retained read-only leases.

    The caller owns the already-open pinned candidate connection/read lease or
    writer/mutex. This context owns only
its source reader. No timer, renewal, candidate write, trust upgrade or receipt.
"""
    def __init__(self,store,snapshot_dir,*,scope_key,permission_check,max_seconds=600,clock=None,fixture_mode=False):
        if not isinstance(store,ExportCandidateStore) or not sha(scope_key) or not callable(permission_check):fail('CANDIDATE_REVIEW_ARGUMENTS')
        if type(max_seconds) not in (int,float) or not math.isfinite(max_seconds) or not 0<max_seconds<=1800:fail('CANDIDATE_REVIEW_TIMEOUT_INVALID')
        if type(fixture_mode) is not bool or clock is not None and (not fixture_mode or not callable(clock)):fail('CANDIDATE_REVIEW_CLOCK_FIXTURE_ONLY')
        self.store=store;self.snapshot_dir=Path(snapshot_dir);self.scope_key=scope_key;self.permission_check=permission_check;self.max_seconds=max_seconds
        self.session_id=uuid.uuid4().hex
        self.clock=clock or time.monotonic;self.reader=None;self.entered=False;self.closed=False;self.poisoned=False;self.started=None;self.last_clock=None;self.proofs={};self.thread=None
    def _now(self):
        value=self.clock()
        if type(value) not in (int,float) or not math.isfinite(value) or self.last_clock is not None and value<self.last_clock:fail('CANDIDATE_REVIEW_CLOCK_INVALID')
        self.last_clock=value;return value
    def _poison(self):
        self.poisoned=True
        if self.reader is not None:self.reader.failed=True
    def _permission(self):
        if self.permission_check()!=self.scope_key:fail('CANDIDATE_REVIEW_SCOPE_CHANGED')
    def _guard(self,scope_key=None,*,initializing=False):
        try:
            if self.closed or self.poisoned or not self.entered:fail('CANDIDATE_REVIEW_UNAVAILABLE')
            if threading.get_ident()!=self.thread or scope_key is not None and scope_key!=self.scope_key:fail('CANDIDATE_REVIEW_SCOPE_CHANGED')
            self._permission()
            now=self._now()
            if initializing:
                if now-self.initialized_at>600:fail('CANDIDATE_REVIEW_INITIALIZATION_TIMEOUT')
            elif self.started is None or now-self.started>=self.max_seconds:fail('CANDIDATE_REVIEW_EXPIRED')
            self.store._available()
            if self.store.conn is not self.connection or self.store.conn.total_changes!=self.total_changes or self.store._paths()!=self.candidate_stamp or self.store.conn.execute('PRAGMA data_version').fetchone()[0]!=self.data_version or self.store._meta()!=self.metadata:fail('CANDIDATE_REVIEW_CANDIDATE_CHANGED')
            if getattr(self.store,'_review_session',None) is not self:fail('CANDIDATE_REVIEW_UNAVAILABLE')
            if self.reader is not None:self.reader.summary()
        except BaseException:self._poison();raise
    def __enter__(self):
        try:
            if self.entered or self.closed or getattr(self.store,'_review_session',None) is not None:fail('CANDIDATE_REVIEW_UNAVAILABLE')
            self.store._available()
            if self.store.conn.in_transaction:fail('CANDIDATE_OPERATION_IN_PROGRESS')
            if self.store.expected_profile is None or self.store.meta['state']!='ready_for_mapping':fail('CANDIDATE_REVIEW_PINNED_READY_REQUIRED')
            self._permission() # Always before reading the source filesystem.
            self.initialized_at=self._now();self.thread=threading.get_ident()
            self.store._validate_all()
            self.metadata=dict(self.store.meta);self.connection=self.store.conn;self.total_changes=self.connection.total_changes
            self.candidate_stamp=self.store._paths();self.data_version=self.connection.execute('PRAGMA data_version').fetchone()[0]
            self.store._review_session=self;self.entered=True;self._guard(initializing=True)
            from tools.large_export_reader import LargeExportReader
            self.reader=LargeExportReader(self.snapshot_dir,expected_snapshot_sha256=self.metadata['snapshot_sha256'],account_namespace=self.metadata['account_namespace'],source_kind=self.metadata['source_kind'])
            self.reader.__enter__();self._guard(initializing=True)
            if self.reader.summary()['schema_sha256']!=self.metadata['schema_sha256']:fail('CANDIDATE_REVIEW_SOURCE_CHANGED')
            cursor=None;charged=0;pages=0
            while True:
                self._guard(initializing=True)
                page=self.reader.page(cursor=cursor,limit=self.metadata['page_size']);pages+=1
                receipt=self.connection.execute('SELECT input_digest,record_digest,row_count FROM candidate_page WHERE page_index=?',(pages,)).fetchone()
                if receipt is None or receipt[0]!=hashed(page['rows']) or receipt[2]!=len(page['rows']):fail('CANDIDATE_REVIEW_SOURCE_CHANGED')
                charged+=len(canonical_json([pages,*receipt]))+256
                if charged>MAX_REVIEW_PROOF_BYTES:fail('CANDIDATE_REVIEW_PROOF_BUDGET')
                self.proofs[pages]=receipt;self._guard(initializing=True)
                if page['complete']:break
                cursor=page['next_cursor']
            if pages!=self.metadata['pages'] or self.reader.count!=self.metadata['expected_rows'] or self.reader.digest.hexdigest()!=self.metadata['expected_membership_sha256'] or not self.reader.final_verified:fail('CANDIDATE_REVIEW_SOURCE_CHANGED')
            self._guard(initializing=True);self.started=self._now();return self
        except BaseException:
            self._poison();self._release();raise
    def assert_active(self,*,store,scope_key,snapshot_dir):
        if store is not self.store or Path(snapshot_dir)!=self.snapshot_dir:self._poison();fail('CANDIDATE_REVIEW_SCOPE_CHANGED')
        self._guard(scope_key)
    @contextmanager
    def locked_page_preview(self,page_index,*,scope_key):
        try:
            self._guard(scope_key)
            with self.store.locked_page_preview(page_index) as view:
                proof=self.proofs.get(page_index)
                if proof!=(view['input_digest'],view['record_digest'],view['quote']['rows']):fail('CANDIDATE_REVIEW_PAGE_CHANGED')
                self._guard(scope_key);yield view;self._guard(scope_key)
            self._guard(scope_key)
        except BaseException:self._poison();raise
    def _release(self):
        try:
            if self.reader is not None:self.reader.close()
        finally:
            if getattr(self.store,'_review_session',None) is self:self.store._review_session=None
            self.closed=True;self.proofs.clear()
    def close(self):
        if self.closed:return
        try:
            if not self.poisoned:
                self._guard();self.reader.verify_final();self._guard()
        except BaseException:self._poison();raise
        finally:self._release()
    def __exit__(self,kind,value,tb):
        if kind is not None:self._poison()
        self.close()
