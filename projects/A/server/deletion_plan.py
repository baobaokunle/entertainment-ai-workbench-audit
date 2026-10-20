"""Read-only necessary-deletion inventory. No deletion or physical-purge claim."""
from pathlib import Path
from .common import AppError


class DeletionPlanner:
    MAX_IDENTIFIERS=5000

    def __init__(self,db,root):
        raw=Path(root)
        if not raw.is_absolute():raise AppError(422,'DELETION_ROOT_INVALID','明确的隔离资料根目录必需')
        for path in (raw,*raw.parents):
            if path.is_symlink() or getattr(path,'is_junction',lambda:False)():raise AppError(422,'DELETION_ROOT_INVALID','副本盘点不能穿过链接或junction')
        self.root=raw.resolve();self.db=db
        if not db.path.is_relative_to(self.root):raise AppError(422,'DELETION_ROOT_INVALID','盘点数据库必须位于指定资料根目录')

    def _files(self,relative):
        directory=self.root/relative
        if directory.is_symlink() or getattr(directory,'is_junction',lambda:False)():raise AppError(422,'DELETION_PATH_LINK','盘点目录不能含链接或junction')
        if not directory.exists():return {'owned_files':0,'classification':'directory_absent','content_inspected':False}
        count=0;pending=[directory]
        while pending:
            path=pending.pop()
            if path.is_symlink() or getattr(path,'is_junction',lambda:False)():raise AppError(422,'DELETION_PATH_LINK','盘点目录不能含链接或junction')
            if path.is_dir():pending.extend(path.iterdir())
            elif path.is_file():count+=1
            if count+len(pending)>10000:raise AppError(413,'DELETION_INVENTORY_LIMIT','副本数量需分批审查')
        return {'owned_files':count,'classification':'owned_content_unclassified','encryption':'not_inspected','content_inspected':False}

    def plan(self,account_key,message_key,principal):
        principal.require('history:read',account_key);principal.require('identities:write',account_key)
        if principal.role!='manager':raise AppError(403,'MANAGER_REQUIRED','必要删除盘点仅限本机管理端')
        if not isinstance(message_key,str) or not 1<=len(message_key)<=200:raise AppError(422,'DELETION_TARGET_INVALID','需要单条稳定消息键')
        with self.db.lock:
            conn=self.db.conn
            message=conn.execute('SELECT account_key,person_key,platform_message_key FROM message WHERE message_key=?',(message_key,)).fetchone()
            if not message or message['account_key']!=account_key:raise AppError(404,'DELETION_TARGET_UNKNOWN','指定账号内未找到该消息')
            tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            counts={}
            def count(name,where,params):
                counts[name]=conn.execute('SELECT COUNT(*) FROM '+name+' WHERE '+where,params).fetchone()[0] if name in tables else None
            count('message_revision','message_key=?',(message_key,))
            count('message_observation','account_key=? AND message_key=?',(account_key,message_key))
            count('history_gram','message_key=?',(message_key,))
            count('history_watermark','message_key=?',(message_key,))
            count('send_link','message_key=?',(message_key,))
            counts['manual_action_linked']=conn.execute('SELECT COUNT(*) FROM manual_action a JOIN send_link l ON l.action_key=a.action_key WHERE l.message_key=?',(message_key,)).fetchone()[0]
            observations=[r[0] for r in conn.execute('SELECT observation_key FROM message_observation WHERE account_key=? AND message_key=? LIMIT ?',(account_key,message_key,self.MAX_IDENTIFIERS+1))]
            jobs=[]
            if 'ai_job' in tables:
                jobs=[r[0] for r in conn.execute("SELECT job_key FROM ai_job WHERE account_key=? AND EXISTS(SELECT 1 FROM json_each(ai_job.context_json,'$.messages') x WHERE json_extract(x.value,'$.message_key')=?) LIMIT ?",(account_key,message_key,self.MAX_IDENTIFIERS+1))]
            targets=set([message_key,*observations,*jobs]);truncated=len(targets)>self.MAX_IDENTIFIERS
            targets=sorted(targets)[:self.MAX_IDENTIFIERS];events=[]
            for offset in range(0,len(targets),400):
                block=targets[offset:offset+400];marks=','.join('?' for _ in block)
                events.extend(r[0] for r in conn.execute('SELECT event_id FROM domain_event WHERE account_key=? AND target_key IN ('+marks+') LIMIT ?',(account_key,*block,self.MAX_IDENTIFIERS+1)))
                if len(events)>self.MAX_IDENTIFIERS:truncated=True;events=events[:self.MAX_IDENTIFIERS];break
            counts['unlinked_observations_needing_review']=conn.execute('SELECT COUNT(*) FROM message_observation WHERE account_key=? AND message_key IS NULL',(account_key,)).fetchone()[0]
            counts['ai_job_linked']=len(jobs)
            if 'causal_revision' in tables:
                counts['person_profile_revisions_needing_excerpt_review']=conn.execute("SELECT COUNT(*) FROM causal_revision WHERE account_key=? AND target_key=? AND (field IN ('note','birthday','anniversary') OR field GLOB 'free_term.*' OR field GLOB 'entitlement.*')",(account_key,message['person_key'])).fetchone()[0]
            else:counts['person_profile_revisions_needing_excerpt_review']=None
            counts['domain_event_linked']=len(events)
            counts['outbox_linked']=0
            counts['publication_packages_linked']=0 if 'sync_publication_event' in tables else None
            counts['received_packages_linked']=0 if 'sync_package_event' in tables else None
            publications=set();receipts=set()
            for offset in range(0,len(events),400):
                block=events[offset:offset+400];marks=','.join('?' for _ in block)
                counts['outbox_linked']+=conn.execute('SELECT COUNT(*) FROM outbox WHERE event_id IN ('+marks+')',block).fetchone()[0]
                if 'sync_publication_event' in tables:publications.update(r[0] for r in conn.execute('SELECT package_id FROM sync_publication_event WHERE event_id IN ('+marks+')',block))
            if 'sync_received' in tables:
                received_ids=[]
                for offset in range(0,len(targets),400):
                    block=targets[offset:offset+400];marks=','.join('?' for _ in block)
                    received_ids.extend(r[0] for r in conn.execute("SELECT event_id FROM sync_received WHERE json_extract(event_json,'$.account_key')=? AND json_extract(event_json,'$.target_key') IN ("+marks+') LIMIT ?',(account_key,*block,self.MAX_IDENTIFIERS+1)))
                    if len(received_ids)>self.MAX_IDENTIFIERS:truncated=True;received_ids=received_ids[:self.MAX_IDENTIFIERS];break
                counts['sync_received_linked']=len(received_ids)
                if 'sync_package_event' in tables:
                    for offset in range(0,len(received_ids),400):
                        block=received_ids[offset:offset+400];marks=','.join('?' for _ in block)
                        receipts.update(r[0] for r in conn.execute('SELECT package_id FROM sync_package_event WHERE event_id IN ('+marks+')',block))
            else:counts['sync_received_linked']=None
            if counts['publication_packages_linked'] is not None:counts['publication_packages_linked']=len(publications)
            if counts['received_packages_linked'] is not None:counts['received_packages_linked']=len(receipts)
            counts['legacy_records_needing_review']=conn.execute('SELECT COUNT(*) FROM legacy_record').fetchone()[0]
            tombstone=conn.execute('SELECT deletion_state FROM deletion_tombstone WHERE target_key=?',(message_key,)).fetchone()
        files={name:self._files(relative) for name,relative in [('staging','staging'),('transport','sync'),('recovery_journal','journal'),('backups','backups')]}
        return {'format':'dfa-deletion-plan-1','mode':'read_only_inventory','account_key':account_key,'message_key':message_key,
                'state':tombstone[0] if tombstone else 'not_requested','copies_state':'copies_pending','counts':counts,'owned_directories':files,
                'complete_inventory':False,'identifier_limit_reached':truncated,'body_exported':False,'deleted':False,'physical_purged':False,
                'unresolved':['unlinked_observations_and_profile_excerpts','encrypted_packages_and_journal_need_authenticated_record_scan','peer_business_copy_and_dedicated_deletion_confirmation','backup_restore_must_reapply_external_authenticated_deletion_log','sqlite_wal_free_pages_and_external_copies','all_remaining_records_in_mixed_packages_must_be_preserved'],
                'execution_available':False}
