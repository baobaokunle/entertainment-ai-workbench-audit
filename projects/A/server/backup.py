"""Disk SQLite snapshots in verified EFS scratch; streaming authenticated backups.

V2 is bounded to 64 GiB; legacy V1 stays readable without whole-database RAM.
Restore publishes only a fresh candidate, never activating or replacing a DB.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import struct
import time
from .common import AppError,utc_now
from .security import SecureEnvelope
from .backup_scratch import ProtectedScratch,reject_links,require_efs
from . import backup_stream


class BackupService:
    AAD=b'douyin-fan-assistant/sqlite-backup/v1'
    MAX_MANIFEST=backup_stream.MAX_MANIFEST_BYTES
    MAX_SECONDS=1800

    @staticmethod
    def _publish_new(temp,destination):
        if os.name=='nt':os.rename(temp,destination)
        else:os.link(temp,destination)

    def __init__(self,db_path,backup_dir,*,key,protector=None):
        self.db_path=reject_links(db_path).resolve()
        self.backup_dir=reject_links(backup_dir).resolve()
        if not isinstance(key,bytes) or len(key)!=32:raise AppError(422,'invalid_key','A 256-bit backup key is required.')
        self.key=key;self.protector=protector

    @staticmethod
    def _configure(conn):
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.execute('PRAGMA mmap_size=0')
        conn.execute('PRAGMA cache_size=-4096')
        conn.execute('PRAGMA temp_store=MEMORY')

    @staticmethod
    def _inspect(conn):
        from .file_import_index import CORE_INDEXES,register_import_functions,indexes_available,_source_indexes
        names={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_file_%'")}
        app_meta=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'").fetchone()
        marker=conn.execute("SELECT value FROM app_meta WHERE key='file_import_index_version'").fetchone() if app_meta else None
        if not marker and not names.intersection(CORE_INDEXES):return BackupService._inspect_counts(conn)
        # Reopen only committed, disk-backed inspection snapshots. Never alter
        # the caller's authorizer, function registry or schema trust setting.
        if conn.in_transaction:raise AppError(409,'backup_inspection_transaction','Commit the private inspection snapshot before validation.')
        main=[r[2] for r in conn.execute('PRAGMA database_list') if r[1]=='main']
        if len(main)!=1 or not main[0]:raise AppError(422,'backup_inspection_disk_required','Indexed validation requires a disk snapshot.')
        path=reject_links(main[0]).resolve()
        private=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5,isolation_level=None)
        try:
            private.enable_load_extension(False);BackupService._configure(private)
            register_import_functions(private);private.execute('PRAGMA query_only=ON');private.execute('BEGIN')
            if not indexes_available(private):raise AppError(409,'FILE_IMPORT_INDEX_INCOMPATIBLE','Explicit compatible import index migration required.')
            owned=CORE_INDEXES|_source_indexes(private)
            actual=dict(private.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND name LIKE 'idx_file_%'"))
            if any(name not in owned or owned[name]!=sql for name,sql in actual.items()):raise AppError(409,'FILE_IMPORT_INDEX_INCOMPATIBLE','Unknown import index definition cannot be inspected as owned.')
            # Python cannot mark a UDF SQLITE_INNOCUOUS. This exception is
            # private, mode=ro, query-only, and limited to known pure functions.
            # Ordinary application connections keep their existing policy.
            private.execute('PRAGMA trusted_schema=ON')
            functions={'count','like','json_extract',*('dfa_file_candidate_'+suffix+'_v1' for suffix in ('valid','namespace','batch'))}
            def readonly(action,arg1,arg2,database,origin):
                if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ):return sqlite3.SQLITE_OK
                if action==sqlite3.SQLITE_FUNCTION and (arg2 or arg1 or '').lower() in functions:return sqlite3.SQLITE_OK
                if action==sqlite3.SQLITE_PRAGMA and arg1 in ('integrity_check','foreign_key_check') and arg2 is None:return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            private.set_authorizer(readonly)
            try:return BackupService._inspect_counts(private)
            except sqlite3.Error as exc:raise AppError(422,'backup_integrity_failed','Indexed snapshot validation failed without altering evidence.') from exc
        finally:private.close()

    @staticmethod
    def _inspect_counts(conn):
        if [r[0] for r in conn.execute('PRAGMA integrity_check')]!=['ok'] or conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise AppError(422,'backup_integrity_failed','Backup database integrity validation failed.')
        tables=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 2001")]
        if len(tables)>2000:raise AppError(413,'backup_table_limit','Backup table limit exceeded.')
        return {name:conn.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in tables}

    @staticmethod
    def _reject_deletion_fixture(conn):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND lower(name) GLOB 'local_deletion_*' LIMIT 1").fetchone():
            raise AppError(503,'LOCAL_DELETION_BACKUP_HOLD','Deletion fixture backup/restore requires an independent authenticated log and expected-head recovery path; that product path is unavailable.')

    def _reject_live_deletion_fixture(self):
        reject_links(self.db_path)
        if not self.db_path.exists():return
        conn=None
        try:
            conn=sqlite3.connect(self.db_path.as_uri()+'?mode=ro',uri=True,timeout=5)
            self._configure(conn)
            self._reject_deletion_fixture(conn)
        except sqlite3.Error as exc:
            raise AppError(503,'LOCAL_DELETION_BACKUP_HOLD','Current database deletion state could not be checked; restore refused.') from exc
        finally:
            if conn is not None:conn.close()

    @staticmethod
    def _source_versions(conn,counts):
        if 'source' not in counts or not {'source_id','source_version'}<={r[1] for r in conn.execute('PRAGMA table_info(source)')}:return []
        rows=conn.execute('SELECT source_id,source_version FROM source ORDER BY source_id,source_version LIMIT 10001').fetchall()
        if len(rows)>10000:raise AppError(413,'backup_manifest_limit','Source-version manifest limit exceeded.')
        return [dict(source_id=r[0],source_version=r[1]) for r in rows]

    def create(self):
        reject_links(self.db_path);reject_links(self.backup_dir)
        if not self.db_path.is_file():raise AppError(404,'database_missing','The database to back up does not exist.')
        if self.backup_dir==self.db_path.parent:raise AppError(422,'backup_directory_invalid','Backups require a separate directory.')
        temp=None
        try:
            self.backup_dir.mkdir(parents=True,exist_ok=True)
            with ProtectedScratch(self.backup_dir) as scratch:
                source=sqlite3.connect(self.db_path.as_uri()+'?mode=ro',uri=True,timeout=5)
                snapshot=None
                try:
                    snapshot=sqlite3.connect(scratch.database)
                    self._configure(source);self._configure(snapshot)
                    self._reject_deletion_fixture(source)
                    page_size=source.execute('PRAGMA page_size').fetchone()[0]
                    deadline=time.monotonic()+self.MAX_SECONDS
                    def progress(status,remaining,total):
                        if time.monotonic()>deadline:raise AppError(503,'backup_budget_exceeded','Backup snapshot time budget exceeded.')
                        if total*page_size>backup_stream.MAX_DATABASE_BYTES:raise AppError(413,'backup_size_limit','Backup exceeds the 64 GiB limit.')
                    progress(0,0,source.execute('PRAGMA page_count').fetchone()[0])
                    source.backup(snapshot,pages=256,progress=progress,sleep=.01)
                    snapshot.execute('PRAGMA journal_mode=DELETE');snapshot.commit()
                    counts=self._inspect(snapshot)
                    self._reject_deletion_fixture(snapshot)
                    metadata={'backup_id':secrets.token_hex(16),'created_at':utc_now(),'row_counts':counts,
                              'schema_user_version':snapshot.execute('PRAGMA user_version').fetchone()[0],
                              'source_versions':self._source_versions(snapshot,counts)}
                finally:
                    if snapshot is not None:snapshot.close()
                    source.close()
                require_efs(scratch.database)
                filename=metadata['backup_id']+'.dfab';output=self.backup_dir/filename
                temp=self.backup_dir/(filename+'.tmp')
                with scratch.database.open('rb') as plain,temp.open('xb') as encrypted:
                    manifest=backup_stream.write_stream(plain,encrypted,self.key,metadata)
                    encrypted.flush();os.fsync(encrypted.fileno())
                self._publish_new(temp,output)
                return dict(manifest,filename=filename)
        except AppError:raise
        except (OSError,sqlite3.Error,ValueError) as exc:raise AppError(503,'backup_failed','Consistent encrypted backup could not be created.') from exc
        finally:
            if temp is not None:temp.unlink(missing_ok=True)

    def _backup_path(self,value):
        raw=Path(value);path=reject_links(raw if raw.is_absolute() else self.backup_dir/raw).resolve()
        if path.parent!=self.backup_dir or path.suffix!='.dfab':raise AppError(403,'backup_path_forbidden','Backup path is outside the registered backup directory.')
        if not path.is_file() or path.stat().st_size>backup_stream.MAX_FILE_BYTES:raise AppError(422,'backup_file_invalid','Backup file is missing or exceeds the size limit.')
        return path

    def _read_legacy(self,source,target,size):
        """Stream old GCM into EFS; trust nothing before authenticating final tag."""
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        prefix=len(SecureEnvelope.MAGIC)
        if not prefix+28<size<=SecureEnvelope.MAX_BYTES+prefix+28:raise ValueError('legacy size')
        header=source.read(prefix+12)
        if not header.startswith(SecureEnvelope.MAGIC):raise ValueError('legacy magic')
        source.seek(-16,2);tag=source.read(16);source.seek(prefix+12)
        decryptor=Cipher(algorithms.AES(self.key),modes.GCM(header[prefix:],tag)).decryptor()
        decryptor.authenticate_additional_data(SecureEnvelope.MAGIC+self.AAD)
        remaining=size-prefix-28;pending=bytearray();manifest=None;length=None;total=0;sha=hashlib.sha256()
        while remaining:
            encrypted=source.read(min(backup_stream.CHUNK_BYTES,remaining))
            if not encrypted:raise ValueError('legacy truncated')
            remaining-=len(encrypted);plain=decryptor.update(encrypted)
            if manifest is None:
                pending.extend(plain)
                if length is None and len(pending)>=4:
                    length=struct.unpack('>I',pending[:4])[0]
                    if not 0<length<=self.MAX_MANIFEST:raise ValueError('legacy manifest bound')
                if length is None or len(pending)<4+length:continue
                manifest=json.loads(pending[4:4+length]);plain=bytes(pending[4+length:]);pending.clear()
            target.write(plain);sha.update(plain);total+=len(plain)
        decryptor.finalize()
        if (not isinstance(manifest,dict) or manifest.get('format')!='dfa-sqlite-backup' or manifest.get('version')!=1
            or type(manifest.get('database_bytes')) is not int or manifest['database_bytes']!=total
            or not isinstance(manifest.get('database_sha256'),str) or not hmac.compare_digest(manifest['database_sha256'],sha.hexdigest())):raise ValueError('legacy manifest mismatch')
        return manifest

    @contextmanager
    def _verified_snapshot(self,backup_file,parent):
        path=self._backup_path(backup_file)
        try:
            with ProtectedScratch(parent) as scratch:
                with path.open('rb') as encrypted,scratch.database.open('r+b') as plain:
                    magic=encrypted.read(len(backup_stream.MAGIC));encrypted.seek(0)
                    manifest=backup_stream.read_stream(encrypted,plain,self.key) if magic==backup_stream.MAGIC else self._read_legacy(encrypted,plain,path.stat().st_size)
                    plain.flush();os.fsync(plain.fileno())
                    plain.seek(0);header=plain.read(100)
                    if len(header)<100 or header[:16]!=b'SQLite format 3\x00':raise ValueError('sqlite header')
                    # Old serialized WAL snapshots have no associated WAL file.
                    # Only this fully authenticated disposable copy is normalized.
                    if manifest['version']==1:
                        plain.seek(18);plain.write(b'\x01\x01');plain.flush();os.fsync(plain.fileno())
                require_efs(scratch.database)
                conn=sqlite3.connect(scratch.database.as_uri()+'?mode=ro',uri=True)
                try:
                    self._configure(conn);counts=self._inspect(conn)
                    if counts!=manifest.get('row_counts') or conn.execute('PRAGMA user_version').fetchone()[0]!=manifest.get('schema_user_version') or self._source_versions(conn,counts)!=manifest.get('source_versions'):raise ValueError('database manifest mismatch')
                finally:conn.close()
                yield manifest,scratch.database
        except AppError:raise
        except Exception as exc:raise AppError(422,'backup_verification_failed','Backup manifest, authentication or database verification failed.') from exc

    def verify(self,backup_file):
        with self._verified_snapshot(backup_file,self.backup_dir) as (manifest,_):return dict(manifest,verified=True)

    def restore(self,backup_file,destination_path):
        destination=reject_links(destination_path).resolve()
        def unavailable():return destination==self.db_path or destination.exists() or any(Path(str(destination)+s).exists() for s in ('-wal','-shm','-journal'))
        if unavailable():raise AppError(409,'restore_destination_exists','Restore requires a new destination; live overwrite is forbidden.')
        if not destination.parent.is_dir() or destination.suffix.lower() not in ('.db','.sqlite','.sqlite3'):raise AppError(422,'restore_destination_invalid','Restore requires an existing directory and a new SQLite filename.')
        self._reject_live_deletion_fixture()
        with self._verified_snapshot(backup_file,destination.parent) as (manifest,temp):
            # A restored tombstone table alone is not trusted log recovery.
            if any(name.lower().startswith('local_deletion_') for name in manifest['row_counts']):
                raise AppError(503,'LOCAL_DELETION_BACKUP_HOLD','Deletion fixture snapshot restore is unavailable without independent log/expected-head recovery.')
            self._reject_live_deletion_fixture()
            try:
                if unavailable():raise AppError(409,'restore_destination_exists','Restore destination appeared; overwrite refused.')
                self._publish_new(temp,destination)
            except FileExistsError as exc:raise AppError(409,'restore_destination_exists','Restore destination appeared; overwrite refused.') from exc
            except OSError as exc:raise AppError(503,'restore_failed','Restore candidate could not be published.') from exc
        return dict(manifest,restored=True,destination=str(destination),activated=False,
                    deletion_recovery='not_evaluated_without_independent_log',deletion_replay_performed=False)
