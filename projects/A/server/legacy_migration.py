"""Read-only legacy rehearsal: preserve evidence in isolation, never promote guesses.

Only generated fixture files are exercised by this candidate's test suite. Running
against a real snapshot requires a later approved migration workflow.
"""
from pathlib import Path
import base64
import hashlib
import json
import sqlite3

from .common import AppError, canonical_json, utc_now
from .db import stable_key, digest

# Each old field is preserved losslessly in original_json. No runtime business table
# receives rows; classification makes non-promoted fields explicit for later review.
LEGACY_MAPPING = {
 'own_account':('identity_pending','external_id/nickname cannot verify a platform account'),
 'contact':('identity_pending','display_name/tier/default tags do not establish verified identity or entitlement'),
 'identity_key':('identity_pending','legacy platform-global key requires account-scoped proof'),
 'name_history':('display_only','names never resolve identity'),
 'conversation':('identity_pending','external_id/contact_id need verified account and object mapping'),
 'message':('message_pending','sent_at may be inferred; out direction does not prove success or actor'),
 'transcript_revision':('body_pending','preserve original text and revision; no automatic reliable transcript'),
 'rank_history':('metric_pending','date is not a verified metric window'),
 'rank_snapshot':('metric_pending','name/confidence/default zero are not stable identity or complete coverage'),
 'gift_contribution':('metric_pending','old default zero and cumulative values are unqualified'),
 'live_session_scrape':('metric_pending','time-derived session key is not a verified platform session'),
 'profile_field':('profile_pending','inferred/default confidence never upgrades to an explicit fact'),
 'open_question':('disabled_legacy','unsupported old derived question feature remains isolated'),
 'promise':('disabled_legacy','old done flag is not platform completion'),
 'contact_override':('disabled_legacy','old quota/cadence configuration cannot drive candidate behavior'),
 'reminder':('disabled_legacy','old reminder done does not migrate as completion'),
 'suggestion':('disabled_legacy','old cloud/suggestion/fill state does not enter runtime'),
 'fill_event':('disabled_legacy','fill/copy events are not actual successful sends'),
 'style_sample':('disabled_legacy','old style training feature remains disabled'),
 'ingest_run':('audit_only','old ingest success is not a new-source acceptance'),
 'analysis_run':('audit_only','old analysis success is not model or rule verification'),
 'app_event':('audit_only','old audit messages retained in isolation'),
}


def _json_safe(value):
    if isinstance(value,bytes): return {'legacy_blob_base64':base64.b64encode(value).decode('ascii')}
    return value


class LegacyMigration:
    def __init__(self,db): self.db=db

    def rehearse(self,source_path,*,source_label='generated_fixture'):
        source=Path(source_path).resolve()
        if source==self.db.path or not source.is_file():
            raise AppError(400,'INVALID_MIGRATION_SOURCE','Migration source must be a separate existing snapshot.')
        # Hash and open read-only: original snapshot bytes and records are never changed.
        snapshot_digest=hashlib.sha256(source.read_bytes()).hexdigest()
        migration_key=stable_key('migration',source_label,snapshot_digest,'legacy-isolation-v1')
        snapshot=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
        snapshot.row_factory=sqlite3.Row
        try:
            if snapshot.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise AppError(409,'LEGACY_INTEGRITY_FAILED','Legacy snapshot integrity check failed.')
            table_names={row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            records=[];counts={};field_mapping={}
            for table,(classification,reason) in LEGACY_MAPPING.items():
                if table not in table_names:
                    counts[table]=0;field_mapping[table]={};continue
                # Table identifier comes only from the fixed allowlist above.
                columns=[row[1] for row in snapshot.execute('PRAGMA table_info("'+table+'")')]
                field_mapping[table]={column:'legacy_record.original_json.'+column for column in columns}
                rows=snapshot.execute('SELECT * FROM "'+table+'"').fetchall();counts[table]=len(rows)
                occurrence={}
                for row in rows:
                    original={k:_json_safe(row[k]) for k in row.keys()}
                    raw=canonical_json(original).decode();hashed=hashlib.sha256(raw.encode()).hexdigest()
                    old_key=str(original.get('id',original.get('contact_id',original.get('session_key',hashed))))
                    occurrence[old_key]=occurrence.get(old_key,0)+1
                    if occurrence[old_key]>1: old_key+=':'+str(occurrence[old_key])
                    reasons=[reason]
                    if table=='message':
                        reasons.extend(['time_unverified','success_unknown','actor_unknown','decryption_not_attempted'])
                    records.append((stable_key('legacy',migration_key,table,old_key),migration_key,table,old_key,raw,hashed,classification,canonical_json(reasons).decode()))
        except sqlite3.DatabaseError:
            raise AppError(409,'LEGACY_READ_FAILED','Legacy snapshot could not be read safely.')
        finally:
            snapshot.close()
        if hashlib.sha256(source.read_bytes()).hexdigest()!=snapshot_digest:
            raise AppError(409,'LEGACY_CHANGED','Legacy snapshot changed during rehearsal.')
        report={'migration_key':migration_key,'source_digest':snapshot_digest,'table_counts':counts,'isolated_count':len(records),'promoted_count':0,'field_mapping':field_mapping,'mapping_version':'legacy-isolation-v1','coverage':'isolation_only'}
        with self.db.transaction() as conn:
            old=conn.execute('SELECT state FROM migration_journal WHERE migration_key=?',(migration_key,)).fetchone()
            if old:
                report['status']='duplicate';return report
            conn.execute('INSERT INTO migration_journal VALUES (?,?,?,?,?,?)',(migration_key,snapshot_digest,'legacy-isolation-v1',canonical_json(report).decode(),'isolated',utc_now()))
            conn.executemany('INSERT INTO legacy_record VALUES (?,?,?,?,?,?,?,?)',records)
            conn.executemany('INSERT INTO legacy_map VALUES (?,?,?,?,?)',[(migration_key,row[2],row[3],row[0],'isolated') for row in records])
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise AppError(409,'MIGRATION_FK_FAILED','Candidate foreign key verification failed.')
        report['status']='isolated'
        return report


def rehearse_legacy(db,source_path,*,source_label='generated_fixture'):
    return LegacyMigration(db).rehearse(source_path,source_label=source_label)
