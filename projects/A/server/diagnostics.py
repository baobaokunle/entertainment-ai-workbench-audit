"""Explicit EFS fictional diagnostic store: finite metadata, no chat content.

Construction and absent reads do not initialize anything. No timer, automatic
expiry, transport endpoint or secure-erasure claim is provided.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import threading

from .auth import ID_RE, Principal
from .backup_scratch import ProtectedScratch, reject_links, require_efs
from .common import AppError, canonical_json

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = Path(__file__).resolve().parents[1]
FORMAT = 'dfa-finite-diagnostics-1'
DEFAULT_MAX_ROWS = 5000
DEFAULT_MAX_BYTES = 16 * 1048576
MAX_ROWS = 20000
MAX_BYTES = 64 * 1048576
MAX_RESPONSE_BYTES = 65536
ERROR_CATALOG = {
    'SYNC_RETRY': ('sync', 'warning'), 'SYNC_HOLD': ('sync', 'error'), 'SYNC_OK': ('sync', 'info'),
    'STORAGE_BUSY': ('storage', 'warning'), 'STORAGE_LIMIT': ('storage', 'error'), 'STORAGE_OK': ('storage', 'info'),
    'BACKUP_HOLD': ('backup', 'error'), 'BACKUP_OK': ('backup', 'info'),
    'DIAGNOSTICS_OK': ('diagnostics', 'info'), 'DIAGNOSTICS_LIMIT': ('diagnostics', 'warning'),
    'API_BAD_REQUEST': ('api','warning'), 'API_UNAUTHORIZED': ('api','warning'), 'API_FORBIDDEN': ('api','warning'),
    'API_CONFLICT': ('api','warning'), 'API_LIMIT': ('api','warning'), 'API_UNAVAILABLE': ('api','error'),
    'API_INTERNAL_ERROR': ('api','error'), 'COLLECTION_BLOCKED': ('collection','warning'),
    'ACTIVITY_GAP': ('activities','warning'), 'MODEL_UNAVAILABLE': ('model','error'),
}
METRICS = {'latency_ms': 86400000, 'bytes': 1000000000, 'rows': 1000000}
METRIC_COLUMNS = tuple(f'{name}_{suffix}' for name in METRICS for suffix in ('count','total','min','max'))
BUCKET_COLUMNS = ('minute','component','code','level','count','first_us','last_us') + METRIC_COLUMNS
META_KEYS = frozenset({'format','device_key','database_id','revision','enabled','retention_days',
                       'maintenance_enabled','max_rows','max_bytes'})
META_SQL = 'CREATE TABLE diag_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)'
BUCKET_SQL = ('CREATE TABLE diag_bucket(minute INTEGER NOT NULL,component TEXT NOT NULL,code TEXT NOT NULL,'
              'level TEXT NOT NULL,count INTEGER NOT NULL,first_us INTEGER NOT NULL,last_us INTEGER NOT NULL,' +
              ','.join(f'{col} INTEGER NOT NULL' for col in METRIC_COLUMNS) + ',PRIMARY KEY(minute,component,code,level))')
INDEX_SQL = 'CREATE INDEX diag_time ON diag_bucket(last_us)'
SCHEMA = {'diag_meta': META_SQL, 'diag_bucket': BUCKET_SQL, 'diag_time': INDEX_SQL}
MIN_US = 1577836800000000
MAX_US = 4102444800000000
MAX_NUMBER = 2**53-1


def _fail(code='DIAGNOSTICS_INVALID',status=422):
    raise AppError(status,code,'Finite local diagnostic metadata requirements were not met.')


def _integer(value,minimum,maximum):
    if type(value) is not int or not minimum <= value <= maximum:_fail()
    return value


def _utc(value):
    if not isinstance(value,str) or len(value)!=20:_fail('DIAGNOSTICS_UTC_INVALID')
    try:parsed=datetime.strptime(value,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:_fail('DIAGNOSTICS_UTC_INVALID')
    if parsed.strftime('%Y-%m-%dT%H:%M:%SZ')!=value:_fail('DIAGNOSTICS_UTC_INVALID')
    return parsed


def _iso(microseconds):
    return datetime.fromtimestamp(microseconds/1000000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _policy(meta):
    return {'enabled':meta['enabled']=='1','retention_days':14,'maintenance_enabled':meta['maintenance_enabled']=='1',
            'max_rows':int(meta['max_rows']),'max_bytes':int(meta['max_bytes'])}


class DiagnosticsStore:
    def __init__(self,path,device_key,*,clock=None,runtime_root=None):
        self.path=Path(path)
        if not self.path.is_absolute() or self.path.name!='events.sqlite3':_fail('DIAGNOSTICS_PATH',403)
        if not isinstance(device_key,str) or not ID_RE.fullmatch(device_key):_fail()
        # Lexical containment only: constructor does not touch the filesystem.
        if '..' in self.path.parts:_fail('DIAGNOSTICS_PATH',403)
        if runtime_root is None:
            if not self.path.is_relative_to(WORKSPACE_ROOT):_fail('DIAGNOSTICS_PATH',403)
        else:
            root=Path(runtime_root)
            if (not root.is_absolute() or root.drive.upper()!='F:' or '..' in root.parts or
                    any(':' in part for part in root.parts[1:]) or
                    root.is_relative_to(SOURCE_ROOT) or SOURCE_ROOT.is_relative_to(root) or
                    self.path!=root/'diagnostics'/'events.sqlite3'):_fail('DIAGNOSTICS_PATH',403)
        self.device_key=device_key
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.lock=threading.RLock()
        self.conn=None;self.identity=None;self._write_connection=False
        self._validated_version=None;self._cached_meta=None;self._cursors={}
        self.initialization_stage='complete'
        self._batch=None

    @classmethod
    def initialize(cls,path,device_key,*,fixture_mode=False):
        if fixture_mode is not True:_fail('DIAGNOSTICS_FIXTURE_ONLY',403)
        store=cls(path,device_key)
        store._safe_path(missing=True)
        if not store.path.parent.is_dir():_fail('DIAGNOSTICS_PARENT_REQUIRED')
        if store.path.exists():_fail('DIAGNOSTICS_EXISTS',409)
        # ProtectedScratch verifies actual current-user EFS read/write before any
        # SQLite plaintext is written. Parent and final pathname are not created.
        require_efs(store.path.parent,encrypt=True)
        published=False;expected_identity=None;expected_digest=None
        try:
            with ProtectedScratch(store.path.parent) as scratch:
                conn=sqlite3.connect(scratch.database,isolation_level=None)
                try:
                    conn.execute('PRAGMA journal_mode=DELETE');conn.execute('PRAGMA temp_store=MEMORY')
                    conn.execute('PRAGMA synchronous=FULL');conn.execute('BEGIN IMMEDIATE')
                    for sql in SCHEMA.values():conn.execute(sql)
                    meta={'format':FORMAT,'device_key':device_key,'database_id':secrets.token_hex(16),
                          'revision':'0','enabled':'0','retention_days':'14','maintenance_enabled':'0',
                          'max_rows':str(DEFAULT_MAX_ROWS),'max_bytes':str(DEFAULT_MAX_BYTES)}
                    conn.executemany('INSERT INTO diag_meta VALUES(?,?)',meta.items());conn.commit()
                finally:conn.close()
                require_efs(scratch.database)
                stat=scratch.database.stat();expected_identity=(stat.st_dev,stat.st_ino)
                expected_digest=hashlib.sha256(scratch.database.read_bytes()).digest()
                store._safe_path(missing=True)
                # Windows rename fails if another initializer won publication.
                try:os.rename(scratch.database,store.path)
                except FileExistsError:_fail('DIAGNOSTICS_EXISTS',409)
                published=True;require_efs(store.path)
        except Exception:
            if not published:raise
            # Never remove an already published target or a concurrent winner.
            # A verified complete disabled database can survive scratch cleanup
            # failure, explicitly marked for review on this initialization result.
            try:
                store._safe_path()
                stat=store.path.stat()
                if (stat.st_dev,stat.st_ino)!=expected_identity or hashlib.sha256(store.path.read_bytes()).digest()!=expected_digest:raise ValueError()
                require_efs(store.path)
            except Exception:_fail('DIAGNOSTICS_INIT_REQUIRES_REVIEW',503)
            store.initialization_stage='requires_review'
        return store

    def close(self):
        with self.lock:
            self._no_batch()
            if self.conn is not None:self.conn.close();self.conn=None

    def _no_batch(self):
        with self.lock:
            if self._batch is not None:
                self._batch['poison']=True;_fail('DIAGNOSTICS_BATCH_OPERATION',409)

    def _safe_path(self,*,missing=False,own_journal=False):
        reject_links(self.path)
        for suffix in ('-wal','-shm','-journal'):
            side=self.path.with_name(self.path.name+suffix)
            if side.exists() or side.is_symlink():
                if not own_journal or suffix!='-journal':_fail('DIAGNOSTICS_SIDECAR',503)
                reject_links(side)
                if not side.is_file() or side.stat().st_nlink!=1:_fail('DIAGNOSTICS_SIDECAR',503)
                require_efs(side)
        if not self.path.exists():
            if missing:return False
            _fail('DIAGNOSTICS_UNINITIALIZED',409)
        stat=self.path.stat()
        if not self.path.is_file() or stat.st_nlink!=1:_fail('DIAGNOSTICS_PATH',403)
        if self.identity is not None and self.identity!=(stat.st_dev,stat.st_ino):_fail('DIAGNOSTICS_REPLACED',409)
        if stat.st_size>MAX_BYTES:_fail('DIAGNOSTICS_BUDGET',413)
        return True

    def _permission(self,principal,scope):
        if not isinstance(principal,Principal) or principal.device_key!=self.device_key:_fail('DIAGNOSTICS_FORBIDDEN',403)
        if scope=='read':
            needed='diagnostics:read' if principal.role=='manager' else 'diagnostics:self' if principal.role=='baobao' else None
            if needed is None:_fail('DIAGNOSTICS_FORBIDDEN',403)
        else:
            if principal.role!='manager':_fail('DIAGNOSTICS_FORBIDDEN',403)
            needed='diagnostics:'+scope
        principal.require(needed)

    def _open(self,write=False):
        if not self._safe_path(missing=True):return False
        require_efs(self.path)
        # Rollback journals must inherit verified EFS before SQLite writes them.
        require_efs(self.path.parent)
        if self.conn is None or write and not self._write_connection:
            self.close()
            self.identity=(self.path.stat().st_dev,self.path.stat().st_ino)
            self.conn=sqlite3.connect(self.path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,
                                     isolation_level=None,check_same_thread=False)
            self.conn.row_factory=sqlite3.Row
            self.conn.execute('PRAGMA trusted_schema=OFF');self.conn.execute('PRAGMA temp_store=MEMORY')
            self.conn.execute('PRAGMA busy_timeout=5000')
            if write:self.conn.execute('PRAGMA synchronous=FULL')
            self._write_connection=write;self._validated_version=None
        return True

    @contextmanager
    def _transaction(self,write=False,*,record=False):
        with self.lock:
            if self._batch is not None:
                if not write or not record:
                    self._batch['poison']=True;_fail('DIAGNOSTICS_BATCH_OPERATION',409)
                try:
                    self._safe_path(own_journal=True);require_efs(self.path);require_efs(self.path.parent)
                    self._validate();yield self.conn
                except BaseException:
                    self._batch['poison']=True;raise
                return
            try:
                if not self._open(write):_fail('DIAGNOSTICS_UNINITIALIZED',409)
                self.conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                self._safe_path()
                self._validate()
                yield self.conn
                self._safe_path(own_journal=write)
                self.conn.commit()
            except sqlite3.Error:
                if self.conn is not None:self.conn.rollback()
                _fail('DIAGNOSTICS_INVALID',503)
            except BaseException:
                if self.conn is not None:self.conn.rollback()
                raise

    def _validate(self):
        conn=self.conn
        version=conn.execute('PRAGMA data_version').fetchone()[0]
        if conn.execute('SELECT count(*) FROM sqlite_schema WHERE length(sql)>4000 OR length(name)>128 OR length(tbl_name)>128').fetchone()[0]:_fail('DIAGNOSTICS_SCHEMA',503)
        schema=conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema LIMIT 12').fetchall()
        expected={('table',name,name,sql) for name,sql in SCHEMA.items() if name!='diag_time'}|{('index','diag_time','diag_bucket',INDEX_SQL),
            ('index','sqlite_autoindex_diag_meta_1','diag_meta',None),('index','sqlite_autoindex_diag_bucket_1','diag_bucket',None)}
        if {tuple(row) for row in schema}!=expected or len(schema)!=len(expected):_fail('DIAGNOSTICS_SCHEMA',503)
        if conn.execute('SELECT count(*) FROM diag_meta WHERE typeof(key)!=\'text\' OR typeof(value)!=\'text\' OR length(key)>32 OR length(value)>128').fetchone()[0]:_fail()
        meta_rows=conn.execute('SELECT key,value FROM diag_meta LIMIT 11').fetchall()
        meta=dict(meta_rows)
        if len(meta_rows)!=len(META_KEYS) or set(meta)!=META_KEYS or meta['format']!=FORMAT or meta['device_key']!=self.device_key:_fail()
        if len(meta['database_id'])!=32 or any(c not in '0123456789abcdef' for c in meta['database_id']):_fail()
        for key in ('revision','max_rows','max_bytes'):
            value=meta[key]
            if not isinstance(value,str) or len(value)>16 or not value.isascii() or not value.isdigit() or str(int(value))!=value:_fail()
        _integer(int(meta['revision']),0,MAX_NUMBER)
        _integer(int(meta['max_rows']),1,MAX_ROWS);_integer(int(meta['max_bytes']),65536,MAX_BYTES)
        if meta['enabled'] not in ('0','1') or meta['maintenance_enabled'] not in ('0','1') or meta['retention_days']!='14':_fail()
        pages=conn.execute('PRAGMA page_count').fetchone()[0];size=conn.execute('PRAGMA page_size').fetchone()[0]
        if size not in (512,1024,2048,4096,8192,16384,32768,65536) or pages*size>int(meta['max_bytes']):_fail('DIAGNOSTICS_BUDGET',413)
        if version!=self._validated_version or meta!=self._cached_meta:
            if conn.execute('SELECT count(*) FROM diag_bucket').fetchone()[0]>int(meta['max_rows']):_fail('DIAGNOSTICS_BUDGET',413)
            conditions=["typeof(component)!='text' OR length(component)>32", "typeof(code)!='text' OR length(code)>64", "typeof(level)!='text' OR length(level)>16"]
            conditions.extend(f"typeof({name})!='integer'" for name in ('minute','count','first_us','last_us',*METRIC_COLUMNS))
            if conn.execute('SELECT count(*) FROM diag_bucket WHERE '+' OR '.join(conditions)).fetchone()[0]:_fail()
            rows=conn.execute('SELECT * FROM diag_bucket LIMIT ?',(MAX_ROWS+1,)).fetchall()
            if len(rows)>int(meta['max_rows']):_fail('DIAGNOSTICS_BUDGET',413)
            for row in rows:self._validate_row(row)
            if conn.execute('PRAGMA quick_check(1)').fetchone()[0]!='ok':_fail('DIAGNOSTICS_INVALID',503)
            self._validated_version=version;self._cached_meta=meta
        return meta

    def _validate_row(self,row):
        if row['code'] not in ERROR_CATALOG or ERROR_CATALOG[row['code']]!=(row['component'],row['level']):_fail()
        for key in ('minute','count','first_us','last_us',*METRIC_COLUMNS):
            if type(row[key]) is not int:_fail()
        if not MIN_US<=row['first_us']<=row['last_us']<MAX_US or row['minute']%60 or row['minute']!=row['first_us']//60000000*60 or row['minute']!=row['last_us']//60000000*60:_fail()
        _integer(row['count'],1,1000000000)
        for name,bound in METRICS.items():
            count,total,low,high=(row[f'{name}_{suffix}'] for suffix in ('count','total','min','max'))
            _integer(count,0,row['count']);_integer(total,0,MAX_NUMBER)
            if count==0:
                if (total,low,high)!=(0,0,0):_fail()
            elif not 0<=low<=high<=bound or not low*count<=total<=high*count:_fail()

    def _now(self):
        now=self.clock()
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset()!=timedelta(0):_fail('DIAGNOSTICS_CLOCK')
        us=int(now.timestamp()*1000000)
        _integer(us,MIN_US,MAX_US-1)
        return us

    def _revision(self,meta):
        revision=int(meta['revision'])+1;_integer(revision,1,MAX_NUMBER)
        self.conn.execute('UPDATE diag_meta SET value=? WHERE key=\'revision\'',(str(revision),))
        self._cached_meta=None
        return revision

    def status(self,principal,*,device_key):
        self._no_batch()
        self._permission(principal,'read')
        if device_key!=self.device_key:_fail('DIAGNOSTICS_FORBIDDEN',403)
        with self.lock:
            if not self._safe_path(missing=True):
                return {'state':'uninitialized','device_key':device_key,'policy':{'enabled':False,'retention_days':14,'maintenance_enabled':False,
                    'max_rows':DEFAULT_MAX_ROWS,'max_bytes':DEFAULT_MAX_BYTES},'revision':None,'counts':{'buckets':0,'events':0},
                    'storage':{'database_bytes':0,'page_count':0,'page_size':0},'coverage':'observed_only','auto_expiry':False}
            with self._transaction():
                meta=self._validate();count,total=self.conn.execute('SELECT count(*),coalesce(sum(count),0) FROM diag_bucket').fetchone()
                pages=self.conn.execute('PRAGMA page_count').fetchone()[0];size=self.conn.execute('PRAGMA page_size').fetchone()[0]
                return {'state':'ready','device_key':device_key,'policy':_policy(meta),'revision':int(meta['revision']),
                        'counts':{'buckets':count,'events':total},'storage':{'database_bytes':pages*size,'page_count':pages,'page_size':size},
                        'coverage':'observed_only','auto_expiry':False}

    def configure(self,principal,*,expected_revision,enabled,retention_days=14,maintenance_enabled=False,
                  max_rows=DEFAULT_MAX_ROWS,max_bytes=DEFAULT_MAX_BYTES):
        self._no_batch()
        self._permission(principal,'configure')
        _integer(expected_revision,0,MAX_NUMBER);_integer(retention_days,14,14)
        _integer(max_rows,1,MAX_ROWS);_integer(max_bytes,65536,MAX_BYTES)
        if type(enabled) is not bool or type(maintenance_enabled) is not bool:_fail()
        with self._transaction(True):
            meta=self._validate()
            if int(meta['revision'])!=expected_revision:_fail('DIAGNOSTICS_POLICY_STALE',409)
            count=self.conn.execute('SELECT count(*) FROM diag_bucket').fetchone()[0]
            size=self.conn.execute('PRAGMA page_count').fetchone()[0]*self.conn.execute('PRAGMA page_size').fetchone()[0]
            if count>max_rows or size>max_bytes:_fail('DIAGNOSTICS_BUDGET',413)
            self.conn.executemany('UPDATE diag_meta SET value=? WHERE key=?',[(str(int(enabled)),'enabled'),(str(int(maintenance_enabled)),'maintenance_enabled'),
                (str(max_rows),'max_rows'),(str(max_bytes),'max_bytes')])
            revision=self._revision(meta)
        return {'state':'ready','revision':revision,'policy':{'enabled':enabled,'retention_days':14,'maintenance_enabled':maintenance_enabled,
                'max_rows':max_rows,'max_bytes':max_bytes},'auto_expiry':False,'physical_purged':False}

    def record_code(self,code,metrics=None):
        with self.lock:
            try:
                if self._batch is not None:
                    self._batch['calls']+=1
                    if self._batch['calls']>100:_fail('DIAGNOSTICS_BATCH_LIMIT',413)
                return self._record_code(code,metrics)
            except BaseException:
                if self._batch is not None:self._batch['poison']=True
                raise

    def _record_code(self,code,metrics):
        if not isinstance(code,str) or code not in ERROR_CATALOG:_fail('DIAGNOSTICS_CATALOG')
        if metrics is None:metrics={}
        if not isinstance(metrics,dict) or set(metrics)-set(METRICS):_fail('DIAGNOSTICS_METRIC')
        metrics=dict(metrics)
        for name,value in metrics.items():_integer(value,0,METRICS[name])
        with self.lock:
            if self._batch is None and not self._safe_path(missing=True):return {'recorded':False,'state':'uninitialized'}
            with self._transaction(True,record=True):
                meta=self._validate()
                if meta['enabled']!='1':return {'recorded':False,'state':'disabled'}
                now=self._now();minute=now//60000000*60;component,level=ERROR_CATALOG[code]
                key=(minute,component,code,level)
                old=self.conn.execute('SELECT * FROM diag_bucket WHERE minute=? AND component=? AND code=? AND level=?',key).fetchone()
                if old is None and self.conn.execute('SELECT count(*) FROM diag_bucket').fetchone()[0]>=int(meta['max_rows']):_fail('DIAGNOSTICS_BUDGET',413)
                values=dict(old) if old else dict(zip(BUCKET_COLUMNS,(*key,0,now,now,*([0]*len(METRIC_COLUMNS)))))
                values['count']+=1;values['first_us']=min(values['first_us'],now);values['last_us']=max(values['last_us'],now)
                for name,value in metrics.items():
                    if values[name+'_count']==0:values[name+'_min']=value
                    else:values[name+'_min']=min(values[name+'_min'],value)
                    values[name+'_max']=max(values[name+'_max'],value);values[name+'_count']+=1;values[name+'_total']+=value
                self._validate_row(values)
                self.conn.execute('INSERT OR REPLACE INTO diag_bucket VALUES('+','.join('?' for _ in BUCKET_COLUMNS)+')',tuple(values[name] for name in BUCKET_COLUMNS))
                revision=self._revision(meta)
                # Page budget includes table and index allocation; rollback keeps
                # every previously retained row when a whole record exceeds it.
                if self.conn.execute('PRAGMA page_count').fetchone()[0]*self.conn.execute('PRAGMA page_size').fetchone()[0]>int(meta['max_bytes']):_fail('DIAGNOSTICS_BUDGET',413)
                self._cached_meta=dict(meta,revision=str(revision));self._validated_version=self.conn.execute('PRAGMA data_version').fetchone()[0]
                return {'recorded':True,'state':'ready','revision':revision,'durable':self._batch is None}

    @contextmanager
    def batch_records(self,*,fixture_mode=False):
        with self.lock:
            if self._batch is not None:
                self._batch['poison']=True;_fail('DIAGNOSTICS_BATCH_OPERATION',409)
            if fixture_mode is not True:_fail('DIAGNOSTICS_FIXTURE_ONLY',403)
            with self._transaction(True):
                self._batch={'poison':False,'calls':0}
                try:
                    yield self
                    if self._batch['poison']:_fail('DIAGNOSTICS_BATCH_POISONED',409)
                finally:
                    self._batch=None;self._cached_meta=None;self._validated_version=None

    def query(self,principal,*,device_key,start_utc,end_utc,cursor=None,limit=50):
        self._no_batch()
        self._permission(principal,'read')
        if device_key!=self.device_key:_fail('DIAGNOSTICS_FORBIDDEN',403)
        start,end=_utc(start_utc),_utc(end_utc)
        if start.second or end.second or not timedelta(0)<end-start<=timedelta(days=14):_fail('DIAGNOSTICS_WINDOW')
        _integer(limit,1,200)
        if cursor is not None and (not isinstance(cursor,str) or len(cursor)!=43):_fail('DIAGNOSTICS_CURSOR',409)
        with self.lock:
            if not self._safe_path(missing=True):
                if cursor is not None:_fail('DIAGNOSTICS_SNAPSHOT_STALE',409)
                return {'state':'uninitialized','device_key':device_key,'items':[],'next_cursor':None,'snapshot':None,'revision':None,
                        'window':{'start_utc':start_utc,'end_utc':end_utc},'coverage':'observed_only','auto_expiry':False}
            with self._transaction():
                meta=self._validate()
                all_rows=self.conn.execute('SELECT * FROM diag_bucket ORDER BY minute,component,code,level LIMIT ?',(MAX_ROWS+1,)).fetchall()
                fingerprint=hashlib.sha256(canonical_json({'meta':meta,'rows':[tuple(row) for row in all_rows],
                    'identity':self.identity,'data_version':self.conn.execute('PRAGMA data_version').fetchone()[0]})).hexdigest()
                offset=0;binding=(device_key,start_utc,end_utc,limit)
                if cursor is not None:
                    token=self._cursors.get(cursor)
                    if token is None or token['binding']!=binding:_fail('DIAGNOSTICS_CURSOR',409)
                    if token['fingerprint']!=fingerprint:_fail('DIAGNOSTICS_SNAPSHOT_STALE',409)
                    offset=token['offset']
                rows=[row for row in all_rows if start.timestamp()<=row['minute']<end.timestamp()]
                selected=rows[offset:offset+limit];items=[]
                for row in selected:
                    summary={name:{suffix:row[name+'_'+suffix] for suffix in ('count','total','min','max')} for name in METRICS}
                    items.append({'minute_utc':_iso(row['minute']*1000000),'component':row['component'],'code':row['code'],'level':row['level'],
                        'count':row['count'],'first_utc':_iso(row['first_us']),'last_utc':_iso(row['last_us']),'metrics':summary})
                next_cursor=None
                if offset+limit<len(rows):
                    if len(self._cursors)>=128:_fail('DIAGNOSTICS_CURSOR_BUDGET',413)
                    next_cursor=secrets.token_urlsafe(32)
                result={'state':'ready','device_key':device_key,'items':items,'next_cursor':next_cursor,'snapshot':fingerprint,'revision':int(meta['revision']),
                    'window':{'start_utc':start_utc,'end_utc':end_utc},'coverage':'observed_only','auto_expiry':False}
                if len(canonical_json(result))>MAX_RESPONSE_BYTES:_fail('DIAGNOSTICS_RESPONSE_BUDGET',413)
                if next_cursor:self._cursors[next_cursor]={'binding':binding,'fingerprint':fingerprint,'offset':offset+limit}
                return result

    def maintain(self,principal,*,expected_revision):
        self._no_batch()
        self._permission(principal,'maintain');_integer(expected_revision,0,MAX_NUMBER)
        with self._transaction(True):
            meta=self._validate()
            if int(meta['revision'])!=expected_revision:_fail('DIAGNOSTICS_POLICY_STALE',409)
            if meta['maintenance_enabled']!='1':_fail('DIAGNOSTICS_MAINTENANCE_DISABLED',409)
            cutoff=self._now()-14*86400*1000000
            count=self.conn.execute('SELECT count(*) FROM diag_bucket WHERE last_us<?',(cutoff,)).fetchone()[0]
            if count:
                self.conn.execute('DELETE FROM diag_bucket WHERE last_us<?',(cutoff,));revision=self._revision(meta)
            else:revision=int(meta['revision'])
            return {'removed_buckets':count,'cutoff_utc':_iso(cutoff),'revision':revision,'auto_expiry':False,
                    'physical_purged':False,'scope':'local_diagnostic_buckets_only'}
