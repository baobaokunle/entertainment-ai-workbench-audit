"""D5 isolated fixture restoration; no product CLI/API, activation or purge.

The caller must independently trust both the profile and exact latest log head.
This module cannot establish checkpoint freshness after whole-machine rollback.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager

from .backup import BackupService
from . import backup_stream
from .backup_scratch import ProtectedScratch, reject_links, require_efs
from .common import AppError, canonical_json
from .db import Database
from .deletion_intents import AuthenticatedDeletionLog
from .local_deletion import install_schema, LogicalDeletionCoordinator, require_ready


def _hash(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


class DeletionRestoreCoordinator:
    """Known-head fictional recovery only. Default construction is forbidden."""
    def __init__(self,workspace_root,*,backup_key,log,fixture_mode=False):
        if fixture_mode is not True or not isinstance(log,AuthenticatedDeletionLog):
            raise AppError(403,'DELETION_FIXTURE_ONLY','An explicit authenticated fictional restoration context is required.')
        if not isinstance(backup_key,bytes) or len(backup_key)!=32:
            raise AppError(422,'invalid_key','An explicit independent backup key is required.')
        self.root=self._absolute(workspace_root)
        if not self.root.is_dir():raise AppError(422,'DELETION_RESTORE_ROOT','Existing explicit workspace root required.')
        self.log,self.backup_key=log,backup_key
        self._workspace(log.path,file=True)

    @staticmethod
    def _absolute(value):
        if not isinstance(value,(str,Path)) or not str(value):raise AppError(422,'DELETION_RESTORE_PATH','An explicit absolute path is required.')
        raw=Path(value)
        if not raw.is_absolute():raise AppError(422,'DELETION_RESTORE_PATH','Only explicit absolute paths are accepted.')
        return reject_links(raw).resolve()

    def _workspace(self,value,*,file=False):
        path=self._absolute(value)
        if path==self.root or not path.is_relative_to(self.root):
            raise AppError(403,'DELETION_RESTORE_PATH','Fixture paths must remain inside the explicit workspace root.')
        if file and (not path.is_file() or path.stat().st_nlink!=1):
            raise AppError(422,'DELETION_RESTORE_PATH','A single owned fixture file without hard links is required.')
        return path

    def _destination(self,value):
        path=self._workspace(value)
        if not path.parent.is_dir() or path.suffix.lower() not in ('.db','.sqlite','.sqlite3'):
            raise AppError(422,'DELETION_RESTORE_PATH','New SQLite target in an existing workspace directory required.')
        if path.exists() or any(Path(str(path)+suffix).exists() or Path(str(path)+suffix).is_symlink() or getattr(Path(str(path)+suffix),'is_junction',lambda:False)() for suffix in ('-wal','-shm','-journal')):
            raise AppError(409,'DELETION_RESTORE_EXISTS','Restoration cannot overwrite a target or its sidecars.')
        return path

    @staticmethod
    def _profile(value):
        fields={'device_key','account_keys','source_versions','backup_id'}
        if not isinstance(value,dict) or set(value)!=fields:
            raise AppError(422,'DELETION_RESTORE_PROFILE','Explicit finite independently confirmed profile required.')
        text=lambda item,limit:isinstance(item,str) and 0<len(item)<=limit and '\x00' not in item
        accounts=value['account_keys'];sources=value['source_versions'];bid=value['backup_id']
        if (not text(value['device_key'],200) or not isinstance(accounts,list) or len(accounts)>1000
                or any(not text(item,200) for item in accounts) or accounts!=sorted(set(accounts))
                or not isinstance(sources,list) or len(sources)>10000
                or any(not isinstance(row,dict) or set(row)!={'source_id','source_version'}
                       or not text(row['source_id'],200) or not text(row['source_version'],80) for row in sources)
                or [(r['source_id'],r['source_version']) for r in sources]!=sorted(set((r['source_id'],r['source_version']) for r in sources))
                or not isinstance(bid,str) or len(bid)!=32 or any(c not in '0123456789abcdef' for c in bid)):
            raise AppError(422,'DELETION_RESTORE_PROFILE','Sorted unique bounded account/source registration and V2 backup ID required.')
        return json.loads(canonical_json(value))

    @staticmethod
    def _records_match(records,profile):
        if any(row['intent']['device_key']!=profile['device_key'] or row['intent']['account_key'] not in profile['account_keys'] for row in records):
            raise AppError(409,'DELETION_RESTORE_PROFILE','Independent log targets belong to another device or account profile.')

    @staticmethod
    def _manifest_match(manifest,profile):
        if manifest.get('version')!=2:
            raise AppError(422,'DELETION_RESTORE_V2_REQUIRED','Only authenticated V2 backups are supported by this fixture recovery.')
        if manifest.get('backup_id')!=profile['backup_id'] or manifest.get('source_versions')!=profile['source_versions']:
            raise AppError(409,'DELETION_RESTORE_PROFILE','Backup/source registration differs from independently confirmed profile.')
        if any(name.lower().startswith('local_deletion_') for name in manifest['row_counts']):
            raise AppError(503,'DELETION_RESTORE_OLD_BACKUP_REQUIRED','This fixture direction requires a pre-deletion backup without fixture tables.')

    @staticmethod
    def _database_match(db,profile):
        meta=dict(db.conn.execute('SELECT key,value FROM app_meta'))
        accounts=[row[0] for row in db.conn.execute('SELECT account_key FROM own_account ORDER BY account_key')]
        counts=BackupService._inspect(db.conn)
        if meta.get('device_key')!=profile['device_key'] or accounts!=profile['account_keys'] or BackupService._source_versions(db.conn,counts)!=profile['source_versions']:
            raise AppError(409,'DELETION_RESTORE_PROFILE','Restored device/accounts/source registration does not match the independently confirmed old profile.')

    @staticmethod
    def _artifact(path):
        stamp=path.stat();sha=hashlib.sha256();deadline=time.monotonic()+BackupService.MAX_SECONDS
        if stamp.st_size>backup_stream.MAX_FILE_BYTES:raise AppError(413,'DELETION_RESTORE_BUDGET','Backup source exceeds the existing finite V2 size budget.')
        with path.open('rb') as source:
            for chunk in iter(lambda:source.read(1024*1024),b''):
                if time.monotonic()>deadline:raise AppError(503,'DELETION_RESTORE_BUDGET','Fixture source verification time budget exceeded.')
                sha.update(chunk)
        after=path.stat()
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_nlink)
        if identity(stamp)!=identity(after):raise AppError(409,'DELETION_RESTORE_SOURCE_CHANGED','Backup source changed during verification.')
        return identity(after),sha.hexdigest()

    @staticmethod
    def _publish(candidate,destination):
        BackupService._publish_new(candidate,destination)

    @staticmethod
    def _release_reservation(log):
        log.conn.execute('ROLLBACK')

    @contextmanager
    def _private_scratch(self,outcome):
        try:
            with ProtectedScratch(self.root) as scratch:yield scratch
        except Exception:
            if not outcome['published']:raise
            # Atomic publication already completed: cleanup is a follow-up,
            # never a false failed-restore response with a complete new target.
            outcome['cleanup_pending'].append('private_scratch_exit')

    def restore(self,backup_file,destination,*,expected_head,expected_profile):
        if not _hash(expected_head):raise AppError(422,'DELETION_HEAD_REQUIRED','Caller must independently supply the exact latest authenticated log head.')
        profile=self._profile(expected_profile);profile_bytes=canonical_json(profile)
        # Authenticate the complete independent log before creating any scratch.
        self._workspace(self.log.path,file=True)
        records=self.log.read(expected_head=expected_head)
        self._records_match(records,profile)
        backup=self._workspace(backup_file,file=True)
        target=self._destination(destination)
        original=self._artifact(backup)
        db=None;outcome={'published':False,'cleanup_pending':[]}
        with self._private_scratch(outcome) as scratch:
            candidate=scratch.root/'restored-fixture.sqlite3'
            # Detached new source context; never bypass a live fixture database.
            service=BackupService(scratch.root/'absent-detached-source.sqlite3',backup.parent,key=self.backup_key)
            try:
                manifest=service.verify(backup)
                self._manifest_match(manifest,profile)
                restored=service.restore(backup,candidate)
                self._manifest_match(restored,profile)
                require_efs(candidate)
                db=Database(candidate,create=False)
                self._database_match(db,profile)
                install_schema(db,fixture_mode=True)
                gate=LogicalDeletionCoordinator(db,self.log,expected_head,fixture_mode=True)
                gate.recover()
                require_ready(db)
                # Lock order matches D2: business DB then log. SQLite IMMEDIATE
                # also blocks appends from distinct log objects/connections.
                with db.lock,self.log.lock:
                    require_ready(db)
                    self.log.conn.execute('BEGIN IMMEDIATE')
                    try:
                        latest=self.log._read(expected_head)
                        self._records_match(latest,profile)
                        self._workspace(self.log.path,file=True)
                        self._workspace(backup,file=True)
                        if self._artifact(backup)!=original or canonical_json(expected_profile)!=profile_bytes:
                            raise AppError(409,'DELETION_RESTORE_SOURCE_CHANGED','Backup source or caller profile changed before publication.')
                        self._manifest_match(manifest,profile)
                        self._database_match(db,profile)
                        persisted=dict(db.conn.execute('SELECT key,value FROM local_deletion_meta'))
                        if not gate.ready or persisted.get('head')!=expected_head:
                            raise AppError(503,'LOCAL_DELETION_HOLD','Complete recovered suppression must remain ready before publication.')
                        self._destination(target)
                        db.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                        db.conn.execute('PRAGMA journal_mode=DELETE')
                        require_efs(candidate)
                        db.close();db=None
                        self._destination(target)
                        self._publish(candidate,target)
                        outcome['published']=True
                    finally:
                        try:self._release_reservation(self.log)  # Read reservation; never modify source log.
                        except Exception:
                            if not outcome['published']:raise
                            outcome['cleanup_pending'].append('log_reservation_release')
            finally:
                if db is not None:db.close()
                # Fixed freshly owned scratch paths only; no recursive deletion.
                for suffix in ('','-wal','-shm','-journal'):
                    try:Path(str(candidate)+suffix).unlink(missing_ok=True)
                    except OSError:
                        if not outcome['published']:raise
                        outcome['cleanup_pending'].append('owned_candidate_cleanup')
        return {'destination':str(target),'restored':True,'activated':False,'fixture_only':True,
                'deletion_replay_performed':True,'expected_head':expected_head,'backup_id':profile['backup_id'],
                'copies_state':'copies_pending','physical_purged':False,'business_body_removed':False,
                'execution_available':False,'product_entry_enabled':False,'trusted_head_freshness_proven':False,
                'cleanup_state':'requires_review' if outcome['cleanup_pending'] else 'completed',
                'cleanup_pending':sorted(set(outcome['cleanup_pending']))}
