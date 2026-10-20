"""Read-only registered targets and candidate quotes. No enrollment or commit."""
from contextlib import contextmanager
import hmac,json,re
from .auth import Principal
from .common import AppError
from .db import digest
from .export_candidates import ExportCandidateStore,PROFILE_FIELDS
from .file_import import FileImportService
from .identity import require_text
from .local_deletion import guarded

BINDING_FIELDS={'account_key','source_id','source_version','account_namespace'}
def principal_fingerprint(principal):
    return digest(principal.as_dict())

class ImportBindingService:
    def __init__(self,service):
        if not isinstance(service,FileImportService):
            raise AppError(400,'IMPORT_BINDING_SERVICE_INVALID','An explicit file import service is required.')
        self.service,self.db=service,service.db

    def _authorize(self,principal,account):
        self.service._authorize(principal,account)

    @contextmanager
    def _read(self):
        with self.db.lock:
            owned=not self.db.conn.in_transaction
            if owned:self.db.conn.execute('BEGIN')
            try:yield self.db.conn
            finally:
                if owned:self.db.conn.execute('ROLLBACK')

    @staticmethod
    def _binding(binding):
        if type(binding) is not dict or set(binding)!=BINDING_FIELDS:
            raise AppError(400,'IMPORT_BINDING_INVALID','Select exactly account, source, version and namespace.')
        for name,maximum in [('account_key',200),('source_id',200),('source_version',80),('account_namespace',256)]:
            require_text(binding[name],name,maximum)

    @staticmethod
    def _caps(source,account):
        try:caps=json.loads(source['capabilities_json'])
        except (ValueError,TypeError):
            raise AppError(409,'IMPORT_BINDING_REGISTRY_INVALID','Registered source metadata is invalid.') from None
        if source['source_kind']!='unverified_export' or source['verified'] or type(caps) is not dict or set(caps)!={'account_keys'}:
            raise AppError(409,'IMPORT_BINDING_SOURCE_REQUIRED','Select a dedicated unverified export source without fact or action grants.')
        if 'account_keys' in caps:
            values=caps['account_keys']
            if type(values) is not list or any(type(v) is not str for v in values):
                raise AppError(409,'IMPORT_BINDING_REGISTRY_INVALID','Registered account restrictions are invalid.')
            if account not in values:
                raise AppError(403,'IMPORT_SOURCE_ACCOUNT_DENIED','The selected source is restricted to other accounts.')
        return caps

    @guarded
    def options(self,account_key,principal):
        self._authorize(principal,account_key)
        require_text(account_key,'account_key',200)
        with self._read() as conn:
            self.service._schema()
            account=conn.execute('SELECT account_key,platform_uid,verified FROM own_account WHERE account_key=?',(account_key,)).fetchone()
            if not account:raise AppError(409,'IMPORT_REGISTRATION_REQUIRED','The selected account must already be registered.')
            # Finite complete registry inspection: never silently truncate choices.
            rows=conn.execute("SELECT * FROM source WHERE source_kind='unverified_export' AND verified=0 ORDER BY source_id,source_version LIMIT 1001").fetchall()
            if len(rows)>1000:raise AppError(413,'IMPORT_BINDING_OPTIONS_LIMIT','Source registry exceeds the complete choice budget.')
            sources=[]
            for row in rows:
                source=dict(row)
                try:self._caps(source,account_key)
                except AppError as error:
                    if error.code in ('IMPORT_SOURCE_ACCOUNT_DENIED','IMPORT_BINDING_SOURCE_REQUIRED'):continue
                    raise
                if self.service.identities.origin_withdrawn(principal.device_key,account_key,row['source_id'],row['source_version']):continue
                sources.append({key:row[key] for key in ('source_id','source_version','source_kind','verified')})
            if len(sources)>100:raise AppError(413,'IMPORT_BINDING_OPTIONS_LIMIT','Source choices exceed the complete response budget.')
            return {'format':'dfa-import-binding-options-1','account':dict(account),'sources':sources,
                'complete_registry_choices':True,'persisted':False,'business_import_committed':False,
                'real_write_enabled':False,'source_approved':False,'account_binding':'explicit_selection_required'}

    @guarded
    def check_target(self,binding,principal):
        # Permission is checked before any candidate/snapshot IO or registry scan.
        account=binding.get('account_key') if type(binding) is dict else None
        self._authorize(principal,account)
        self._binding(binding)
        with self._read() as conn:
            self.service._schema()
            account=conn.execute('SELECT * FROM own_account WHERE account_key=?',(binding['account_key'],)).fetchone()
            source=conn.execute('SELECT * FROM source WHERE source_id=? AND source_version=?',(binding['source_id'],binding['source_version'])).fetchone()
            if not account or not source:raise AppError(409,'IMPORT_REGISTRATION_REQUIRED','Account and source must already be registered.')
            account,source=dict(account),dict(source);self._caps(source,binding['account_key'])
            self.service._dedicated(binding['source_id'],binding['source_version'])
            target=tuple(binding[key] for key in ('account_key','source_id','source_version'))
            permanent=conn.execute('SELECT * FROM file_import_binding WHERE account_namespace=?',(binding['account_namespace'],)).fetchone()
            if permanent and tuple(permanent[key] for key in ('account_key','source_id','source_version'))!=target:
                raise AppError(409,'IMPORT_NAMESPACE_BOUND','The namespace is permanently bound to another target.')
            if self.service._namespace_conflict(binding['account_namespace'],target):
                raise AppError(409,'IMPORT_NAMESPACE_BOUND','Received observations already bind the namespace to another target.')
            if self.service.identities.origin_withdrawn(principal.device_key,*target):
                raise AppError(422,'SOURCE_WITHDRAWN','This origin source was withdrawn; select a new registered version.')
            version=conn.execute("SELECT value FROM app_meta WHERE key='data_version'").fetchone()
            return {'binding':dict(binding),'account':account,'source':source,
                'permanent_binding':dict(permanent) if permanent else None,
                'registration_fingerprint':digest({'binding':binding,'account':account,'source':source}),
                'business_data_version':version[0] if version else None,'withdrawn':False}

    def inspect(self,binding,principal):
        target=self.check_target(binding,principal)
        quote=digest({'format':'dfa-import-registration-quote-1','target':target,
            'principal':principal_fingerprint(principal)})
        return {'format':'dfa-import-registration-plan-1','binding':target['binding'],
            'account':{k:target['account'][k] for k in ('account_key','platform_uid','verified')},
            'source':{k:target['source'][k] for k in ('source_id','source_version','source_kind','verified')},
            'namespace_already_bound':target['permanent_binding'] is not None,
            'registration_quote':quote,'persisted':False,'business_import_committed':False,
            'candidate_metadata_only':True,'source_revalidated':False,'commit_authorized':False,
            'real_write_enabled':False,'authority':'pending_only'}

    def plan_for_store(self,binding,principal,store):
        self._authorize(principal,binding.get('account_key') if type(binding) is dict else None)
        target=self.check_target(binding,principal)
        if not isinstance(store,ExportCandidateStore) or store.expected_profile is None:
            raise AppError(409,'IMPORT_BINDING_PROFILE_REQUIRED','An independently pinned candidate profile is required.')
        status=store.status()
        if status.get('state')!='ready_for_mapping':raise AppError(409,'IMPORT_BINDING_INCOMPLETE','All candidates must be staged before binding review.')
        profile={key:status[key] for key in PROFILE_FIELDS}
        if profile!=store.expected_profile or profile['source_kind']!='unverified_export' or profile['account_namespace']!=binding['account_namespace']:
            raise AppError(409,'IMPORT_BINDING_PROFILE_MISMATCH','Candidate profile and selected target differ.')
        head={key:status[key] for key in ('store_id','state','revision','pages','rows','content_root')}
        payload={'format':'dfa-import-binding-quote-1','target':target,'candidate_profile':profile,
            'candidate_head':head,'principal':principal_fingerprint(principal)}
        return {'format':'dfa-import-binding-plan-1','binding':dict(binding),'candidate_profile':profile,
            'candidate_head':head,'binding_quote':digest(payload),'registration_fingerprint':target['registration_fingerprint'],
            'persisted':False,'business_import_committed':False,'candidate_metadata_only':True,
            'source_revalidated':False,'commit_authorized':False,'real_write_enabled':False,'authority':'pending_only'}

    def verify_store_plan(self,binding,principal,store,expected_quote):
        self._authorize(principal,binding.get('account_key') if type(binding) is dict else None)
        if type(expected_quote) is not str or not re.fullmatch('[0-9a-f]{64}',expected_quote):
            raise AppError(400,'IMPORT_BINDING_QUOTE_REQUIRED','The reviewed binding quote is required.')
        plan=self.plan_for_store(binding,principal,store)
        if not hmac.compare_digest(plan['binding_quote'],expected_quote):
            raise AppError(409,'IMPORT_BINDING_QUOTE_STALE','Candidate, registration, authorization or business state changed.')
        return plan
