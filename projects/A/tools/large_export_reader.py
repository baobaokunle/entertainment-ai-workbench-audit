"""Bounded forward-only reader of an independently pinned, clean EFS snapshot.

No original database connection, discovery, account/HTTP access or persistence.
Worker requests are strictly one-at-a-time framed JSON; rows stay internal to the
local normalization bridge. Public CLI summaries are produced by that bridge.
"""
import argparse,hashlib,json,math,os,re,secrets,sqlite3,sys,time
from contextlib import ExitStack
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'vendor')]
from tools.large_export_snapshot import LockedRead,load_manifest

WORKSPACE=ROOT.parent
MAX_PAGE_ROWS=2000
MAX_PAGE_BYTES=8*1024*1024
MAX_FIELD_CHARS=256000
# Three selected original text fields at maximum UTF8 width plus finite IDs.
# SQLite applies this byte limit to values AND the whole encoded row.
SQLITE_LENGTH_BYTES=3*4*MAX_FIELD_CHARS+65536
SELECTED=('msg_id','conv_id','sender_uid','content','msg_type','timestamp','raw_data','ref_msg')
TEXT_FIELDS=('msg_id','conv_id','sender_uid','content','raw_data','ref_msg')
MEMBERSHIP_SEED=b'message-id-membership-v1\x00'

class ReaderError(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)

def fail(code):raise ReaderError(code)
def canonical(value):return json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':')).encode('utf-8')
def identity(path):
    info=path.stat()
    if path.is_symlink() or getattr(path,'is_junction',lambda:False)() or not path.is_file() or info.st_nlink!=1:fail('SNAPSHOT_PATH_REJECTED')
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns
def add_membership(digest,message_id):
    encoded=message_id.encode('utf-8');digest.update(len(encoded).to_bytes(8,'big'));digest.update(encoded)

class LargeExportReader:
    def __init__(self,snapshot_dir,*,expected_snapshot_sha256,account_namespace,source_kind='unverified_export',query_seconds=2.0,query_steps=1000000,page_bytes=MAX_PAGE_BYTES):
        self.directory=Path(snapshot_dir)
        if not self.directory.is_absolute() or '..' in self.directory.parts or not self.directory.is_relative_to(WORKSPACE):fail('SNAPSHOT_WORKSPACE_REQUIRED')
        if not isinstance(expected_snapshot_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_snapshot_sha256):fail('EXPECTED_SNAPSHOT_SHA256_REQUIRED')
        if not isinstance(account_namespace,str) or not account_namespace or account_namespace!=account_namespace.strip() or len(account_namespace)>256 or re.search('[\x00-\x1f]',account_namespace):fail('INVALID_NAMESPACE')
        if source_kind not in ('fictional','unverified_export'):fail('SOURCE_KIND_UNVERIFIED_ONLY')
        if type(query_seconds) not in (int,float) or not math.isfinite(query_seconds) or not 0<query_seconds<=2:fail('INVALID_QUERY_BUDGET')
        if type(query_steps) is not int or not 0<query_steps<=1000000 or type(page_bytes) is not int or not 0<page_bytes<=MAX_PAGE_BYTES:fail('INVALID_QUERY_BUDGET')
        self.expected_hash=expected_snapshot_sha256;self.namespace=account_namespace;self.source_kind=source_kind
        self.query_seconds=query_seconds;self.query_steps=query_steps;self.page_bytes=page_bytes
        self.stack=ExitStack();self.conn=None;self.opened=False;self.closed=False;self.failed=False
        self.count=0;self.page_index=0;self.last_id=None;self.token=None;self.page_limit=None;self.complete=False;self.final_verified=False
        self.digest=hashlib.sha256(MEMBERSHIP_SEED)
    def __enter__(self):
        try:
            self._paths()
            self.db_file=self.directory/'snapshot.sqlite3';self.manifest_file=self.directory/'manifest.json'
            self.db_lease=self.stack.enter_context(LockedRead(self.db_file));self.manifest_lease=self.stack.enter_context(LockedRead(self.manifest_file))
            self.identities=(identity(self.db_file),identity(self.manifest_file))
            self.manifest=load_manifest(self.directory,expected_snapshot_sha256=self.expected_hash)
            self.manifest_hash=self.manifest_lease.hash_again()
            self.conn=sqlite3.connect(self.db_file.as_uri()+'?mode=ro&immutable=1',uri=True,isolation_level=None)
            self.conn.enable_load_extension(False)
            for sql in ('PRAGMA query_only=ON','PRAGMA trusted_schema=OFF','PRAGMA temp_store=MEMORY','PRAGMA mmap_size=0','PRAGMA cache_size=-8192'):self.conn.execute(sql)
            # load_manifest already checks actual schema, aggregates and ID digest
            # under the leases held above. Do not rescan a large file twice.
            self.conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,SQLITE_LENGTH_BYTES)
            self.conn.set_authorizer(self._authorize)
            self.opened=True;self._unchanged();return self
        except BaseException:
            self.failed=True;self._release();raise
    def _paths(self):
        for part in (self.directory,*self.directory.parents):
            if part.is_symlink() or getattr(part,'is_junction',lambda:False)():fail('SNAPSHOT_PATH_REJECTED')
        if not self.directory.is_dir() or {item.name for item in self.directory.iterdir()}!={'snapshot.sqlite3','manifest.json'}:fail('SNAPSHOT_FILE_SET_INVALID')
    def _unchanged(self):
        self._paths()
        if (identity(self.db_file),identity(self.manifest_file))!=self.identities:fail('SNAPSHOT_CHANGED')
        if self.db_lease.info()!=self.db_lease.initial or self.manifest_lease.info()!=self.manifest_lease.initial:fail('SNAPSHOT_CHANGED')
    def _authorize(self,action,arg1,arg2,database,origin):
        if origin is not None:return sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_READ and database=='main' and arg1=='messages' and arg2 in SELECTED:return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    def _available(self):
        if not self.opened or self.closed or self.failed:fail('READER_UNAVAILABLE')
        self._unchanged()
    def summary(self):
        self._available()
        # Decimal timestamps avoid JSON number rounding. No source paths or IDs.
        counts=json.loads(json.dumps(self.manifest['aggregates']))
        for key in ('timestamp_integer_min','timestamp_integer_max'):
            if counts['messages'][key] is not None:counts['messages'][key]=str(counts['messages'][key])
        return {'format':'dfa-large-export-summary-1','snapshot_sha256':self.expected_hash,'schema_sha256':self.manifest['schema_sha256'],
            'counts':counts,'scanned_rows':self.count,'pages':self.page_index,'complete':self.complete,
            'membership_sha256':self.digest.hexdigest() if self.complete else None,'source_kind':self.source_kind,
            'source_permission':'not_assessed','source_approved':False,'account_binding':'unverified','coverage':'unknown',
            'persisted':False,'business_import_committed':False,'physical_purge_claim':False}
    @staticmethod
    def _row(values):
        row=dict(zip(SELECTED,values))
        for key in TEXT_FIELDS:
            value=row[key]
            if value is not None and (not isinstance(value,str) or len(value)>MAX_FIELD_CHARS):fail('SOURCE_FIELD_TYPE_OR_BUDGET')
            if isinstance(value,str) and '\0' in value:fail('SOURCE_NUL_REJECTED')
        for key in ('msg_id','conv_id'):
            if not isinstance(row[key],str) or not row[key].strip() or len(row[key])>256 or re.search('[\x00-\x1f]',row[key]):fail('SOURCE_ID_INVALID')
        if row['sender_uid'] is not None and (len(row['sender_uid'])>512 or re.search('[\x00-\x1f]',row['sender_uid'])):fail('SOURCE_ID_INVALID')
        for key in ('msg_type','timestamp'):
            if row[key] is not None and type(row[key]) is not int:fail('SOURCE_NUMERIC_TYPE_INVALID')
        if row['msg_type'] is not None and abs(row['msg_type'])>9007199254740991:fail('SOURCE_NUMERIC_TYPE_INVALID')
        if row['timestamp'] is not None:row['timestamp']=str(row['timestamp'])
        return row
    def page(self,*,cursor=None,limit=200):
        try:
            self._available()
            if type(limit) is not int or not 1<=limit<=MAX_PAGE_ROWS:fail('PAGE_LIMIT_INVALID')
            if self.complete or self.page_index and (cursor!=self.token or limit!=self.page_limit) or not self.page_index and cursor is not None:fail('CURSOR_INVALID_OR_REUSED')
            deadline=time.monotonic()+self.query_seconds;steps=0
            def progress():
                nonlocal steps
                steps+=1
                return int(steps>self.query_steps or time.monotonic()>deadline)
            self.conn.set_progress_handler(progress,1)
            sql='SELECT '+','.join(SELECTED)+' FROM messages'
            parameters=[]
            if self.last_id is not None:sql+=' WHERE msg_id COLLATE BINARY > ?';parameters.append(self.last_id)
            sql+=' ORDER BY msg_id COLLATE BINARY LIMIT ?';parameters.append(limit)
            rows=[];used=0;digest=self.digest.copy();last=self.last_id
            try:
                for values in self.conn.execute(sql,parameters):
                    if time.monotonic()>deadline:fail('PAGE_QUERY_BUDGET')
                    row=self._row(values)
                    if last is not None and row['msg_id'].encode('utf-8')<=last.encode('utf-8'):fail('KEYSET_ORDER_INVALID')
                    last=row['msg_id'];add_membership(digest,last)
                    used+=len(canonical(row))+1
                    if used>self.page_bytes:fail('PAGE_RESPONSE_BUDGET')
                    rows.append(row)
            except sqlite3.Error:fail('PAGE_SQL_OR_QUERY_BUDGET')
            finally:self.conn.set_progress_handler(None,0)
            count=self.count+len(rows);total=self.manifest['aggregates']['messages']['rows']
            if count>total or not rows and count!=total:fail('SCAN_COUNT_MISMATCH')
            complete=count==total
            if complete and digest.hexdigest()!=self.manifest['aggregates']['messages']['message_id_set_sha256']:fail('SCAN_MEMBERSHIP_MISMATCH')
            token=None if complete else secrets.token_urlsafe(32)
            result={'format':'dfa-large-export-page-1','snapshot_sha256':self.expected_hash,'account_namespace':self.namespace,'source_kind':self.source_kind,
                'page_index':self.page_index+1,'rows':rows,'scanned_rows':count,'total_rows':total,'next_cursor':token,'complete':complete,
                'membership_sha256':digest.hexdigest() if complete else None,'source_approved':False,'account_binding':'unverified','coverage':'unknown','persisted':False}
            # This budgets the ENTIRE worker envelope, not just row strings.
            if len(canonical({'ok':True,'data':result}))>self.page_bytes:fail('PAGE_RESPONSE_BUDGET')
            if time.monotonic()>deadline:fail('PAGE_QUERY_BUDGET')
            self._unchanged()
            if complete:self.verify_final()
            self.count=count;self.page_index+=1;self.last_id=last;self.digest=digest;self.token=token;self.page_limit=limit;self.complete=complete
            return result
        except BaseException:self.failed=True;raise
    def verify_final(self):
        self._unchanged()
        if self.db_lease.hash_again()!=self.manifest['snapshot'] or self.manifest_lease.hash_again()!=self.manifest_hash:fail('SNAPSHOT_FINAL_FINGERPRINT_MISMATCH')
        self._unchanged();self.final_verified=True
    def _release(self):
        try:
            if self.conn is not None:self.conn.close();self.conn=None
        finally:self.stack.close();self.closed=True
    def close(self):
        try:
            if self.opened and not self.failed and not self.final_verified:self.verify_final()
        finally:self._release()
    def __exit__(self,kind,value,tb):
        if kind is not None:self.failed=True
        self.close()

def safe_error(error):
    code=getattr(error,'code',None)
    return code if isinstance(code,str) and re.fullmatch('[A-Z][A-Z0-9_]{1,79}',code) else 'LARGE_EXPORT_READER_FAILED'
def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:fail('WORKER_REQUEST_INVALID')
        result[key]=value
    return result
def frame(data):
    blob=canonical(data)
    if len(blob)>MAX_PAGE_BYTES:fail('PAGE_RESPONSE_BUDGET')
    sys.stdout.buffer.write(blob+b'\n');sys.stdout.buffer.flush()
def worker(args):
    with LargeExportReader(args.snapshot_dir,expected_snapshot_sha256=args.expected_snapshot_sha256,account_namespace=args.namespace,source_kind=args.source_kind) as reader:
        while True:
            line=sys.stdin.buffer.readline(4097)
            if not line:return 0
            if len(line)>4096 or not line.endswith(b'\n'):fail('WORKER_REQUEST_INVALID')
            try:request=json.loads(line.decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:fail('WORKER_REQUEST_INVALID'))
            except (ValueError,UnicodeError):fail('WORKER_REQUEST_INVALID')
            if not isinstance(request,dict):fail('WORKER_REQUEST_INVALID')
            if request=={'op':'summary'}:frame({'ok':True,'data':reader.summary()})
            elif request=={'op':'close'}:
                reader.close();frame({'ok':True,'data':{'closed':True}});return 0
            elif request.get('op')=='page' and set(request)=={'op','cursor','limit'}:frame({'ok':True,'data':reader.page(cursor=request['cursor'],limit=request['limit'])})
            else:fail('WORKER_REQUEST_INVALID')
def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self,message):fail('WORKER_ARGUMENTS_INVALID')
    parser=Parser(description=__doc__)
    parser.add_argument('--snapshot-dir',required=True);parser.add_argument('--expected-snapshot-sha256',required=True);parser.add_argument('--namespace',required=True)
    parser.add_argument('--source-kind',choices=('fictional','unverified_export'),default='unverified_export');parser.add_argument('--worker',action='store_true',required=True)
    try:
        args=parser.parse_args(argv)
        return worker(args)
    except Exception as error:
        # No traceback, path, exception repr, arbitrary message, ID or body.
        try:frame({'ok':False,'error':{'code':safe_error(error),'complete':False}})
        except Exception:pass
        return 2
if __name__=='__main__':raise SystemExit(main())
