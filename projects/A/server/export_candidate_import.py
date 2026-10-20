"""Explicit page planning and fictional-only pending business submission.

The business batch is the receipt of truth. This bridge does not mark the
independent candidate store committed, claim a two-database transaction, open
production writes, register identities, or activate trusted message facts.
"""
import re
from .common import AppError
from .db import digest
from .export_candidates import ExportCandidateStore,CandidateReviewSession,CandidateError
from .auth import Principal
from .file_import import FileImportService


class CandidateImportBridge:
    def __init__(self,store,service,*,snapshot_dir,fictional_runtime=False):
        if not isinstance(store,ExportCandidateStore) or not isinstance(service,FileImportService):
            raise AppError(400,'CANDIDATE_IMPORT_COMPONENT_INVALID','Explicit candidate and import services are required.')
        if type(fictional_runtime) is not bool:
            raise AppError(400,'CANDIDATE_IMPORT_MODE_INVALID','An explicit runtime mode is required.')
        self.store,self.service=store,service
        self.snapshot_dir,self.fictional_runtime=snapshot_dir,fictional_runtime

    def _ready(self,session=None,*,mapping=None,principal=None):
        if session is not None:
            try:
                if not isinstance(session,CandidateReviewSession) or getattr(session,'_bridge_service',None) is not self.service:
                    raise AppError(409,'CANDIDATE_IMPORT_REVIEW_REQUIRED','Use the review session created by this business service.')
                binding={key:mapping[key] for key in ('account_key','source_id','source_version','account_namespace')}
                scope=self._review_scope(binding,principal)
                session.assert_active(store=self.store,scope_key=scope,snapshot_dir=self.snapshot_dir)
                return
            except BaseException:
                if isinstance(session,CandidateReviewSession):session._poison()
                raise
        if self.store.expected_profile is None:
            raise AppError(409,'CANDIDATE_IMPORT_EXTERNAL_PROFILE_REQUIRED','A separately pinned snapshot profile is required.')
        status=self.store.status()
        if status['state']!='ready_for_mapping':
            raise AppError(409,'CANDIDATE_IMPORT_INCOMPLETE','All candidates must be staged before mapping.')
        # Explicit source replay; ready metadata alone is not a source proof.
        self.store.finalize(self.snapshot_dir,expected_revision=status['revision'])

    def _authorize(self,mapping,principal,session=None):
        try:
            if type(mapping) is not dict or type(mapping.get('account_key')) is not str:
                raise AppError(400,'IMPORT_MAPPING_INVALID','An explicit account mapping is required.')
            self.service._authorize(principal,mapping['account_key'])
        except BaseException:
            if isinstance(session,CandidateReviewSession):session._poison()
            raise

    def _review_scope(self,binding,principal):
        from .import_binding import ImportBindingService
        target=ImportBindingService(self.service).check_target(binding,principal)
        return digest({'format':'dfa-candidate-review-scope-1','binding':target['binding'],
            'registration_fingerprint':target['registration_fingerprint'],'principal':principal.as_dict()})

    def start_review_session(self,binding,principal,*,max_seconds=600,reauthorize=None,clock=None,fixture_mode=False):
        if reauthorize is not None and not callable(reauthorize):
            raise AppError(400,'CANDIDATE_IMPORT_REAUTH_INVALID','Reauthorization must be an internal callable.')
        def current():
            actual=reauthorize(principal) if reauthorize is not None else principal
            if not isinstance(actual,Principal) or actual!=principal:
                raise AppError(403,'CANDIDATE_IMPORT_AUTH_CHANGED','The enrolled principal or session changed.')
            return actual
        binding= dict(binding) if type(binding) is dict else binding
        # check_target authenticates registered account/source/caps/namespace
        # before this context can open any candidate source file.
        scope=self._review_scope(binding,current())
        if self.store.expected_profile is None or self.store.expected_profile['account_namespace']!=binding['account_namespace'] or self.store.expected_profile['source_kind']!='unverified_export':
            raise AppError(409,'CANDIDATE_IMPORT_REVIEW_PROFILE','The pinned candidate profile and selected namespace differ.')
        def permission():return self._review_scope(binding,current())
        review=self.store.review_session(self.snapshot_dir,scope_key=scope,permission_check=permission,max_seconds=max_seconds,clock=clock,fixture_mode=fixture_mode)
        review._bridge_service=self.service
        return review

    def _page_view(self,page_index,session):
        return self.store.locked_page_preview(page_index) if session is None else session.locked_page_preview(page_index,scope_key=session.scope_key)

    def _source(self,mapping):
        if type(mapping) is not dict:
            raise AppError(400,'IMPORT_MAPPING_INVALID','Explicit mapping is required.')
        source=self.service.identities.get_source(mapping.get('source_id'),mapping.get('source_version'))
        if not source or source['source_kind']!='unverified_export' or source['verified']:
            raise AppError(409,'CANDIDATE_IMPORT_SOURCE_REQUIRED','Select a dedicated unverified export source.')
        caps=source['capabilities']
        if type(caps) is not dict or set(caps)-{'account_keys'}:
            raise AppError(409,'CANDIDATE_IMPORT_SOURCE_CAPABILITIES','Export sources must not grant fact or action capabilities.')

    @staticmethod
    def _quote(view,mapping,business_plan_hash,session=None):
        value={'format':'dfa-candidate-import-quote-1','candidate':view['quote'],
            'mapping_digest':digest(mapping),'business_plan_hash':business_plan_hash}
        if session is not None:value['review_session']={'session_id':session.session_id,'scope_key':session.scope_key}
        return digest(value)

    def _plan(self,view,mapping,principal):
        self._source(mapping)
        return self.service.plan(view['preview'],mapping,principal)

    def plan_page(self,page_index,mapping,principal,*,session=None):
        self._authorize(mapping,principal,session)
        self._ready(session,mapping=mapping,principal=principal)
        with self._page_view(page_index,session) as view:
            plan=self._plan(view,mapping,principal)
            return {'format':'dfa-candidate-import-plan-1','page_index':page_index,
                'candidate_quote':view['quote'],'business_plan':plan,
                'quote_hash':self._quote(view,mapping,plan['plan_hash'],session),
                'persisted':False,'business_import_committed':False,'authority':'pending_only',
                'real_write_enabled':False}

    def commit_page(self,page_index,mapping,expected_quote_hash,principal,*,session=None):
        if not self.fictional_runtime:
            raise AppError(403,'CANDIDATE_IMPORT_REAL_WRITE_DISABLED','Candidate business submission remains disabled for real runtime.')
        self._authorize(mapping,principal,session)
        if type(expected_quote_hash) is not str or not re.fullmatch('[0-9a-f]{64}',expected_quote_hash):
            raise AppError(400,'CANDIDATE_IMPORT_QUOTE_REQUIRED','The reviewed page quote is required.')
        self._ready(session,mapping=mapping,principal=principal)
        # Candidate checks finish before the business outer transaction commits.
        # The candidate transaction is read-only and writes no business receipt.
        with self.service.db.transaction() as conn:
            with self._page_view(page_index,session) as view:
                plan=self._plan(view,mapping,principal)
                accepted={self._quote(view,mapping,plan['plan_hash'],session):plan['plan_hash']}
                old=conn.execute('SELECT plan_hash FROM file_import_batch WHERE batch_id=?',
                    (plan['batch_id'],)).fetchone()
                if old:
                    accepted[self._quote(view,mapping,old['plan_hash'],session)]=old['plan_hash']
                if expected_quote_hash not in accepted:
                    raise AppError(409,'CANDIDATE_IMPORT_QUOTE_STALE','Candidate, mapping, authorization or business state changed.')
                result=self.service.commit(view['preview'],mapping,accepted[expected_quote_hash],principal)
                return {'format':'dfa-candidate-import-result-1','page_index':page_index,
                    'batch_id':result['batch_id'],'status':result['status'],'persisted':True,
                    'business_import_committed':True,'authority':'pending_only',
                    'counts':result['counts'],'withdrawn':result.get('withdrawn',False),
                    'withdrawal_scope':result['withdrawal_scope'],
                    'candidate_store_marked_committed':False,'two_database_atomicity_claim':False}

    def reconcile_page(self,page_index,mapping,principal,*,session=None):
        """Derive status from the stable business batch, never a local ack bit."""
        self._authorize(mapping,principal,session)
        self._ready(session,mapping=mapping,principal=principal)
        with self.service.db.lock:
            conn=self.service.db.conn
            owned=not conn.in_transaction
            if owned:conn.execute('BEGIN')
            try:
                with self._page_view(page_index,session) as view:
                    plan=self._plan(view,mapping,principal)
                    row=conn.execute('SELECT status FROM file_import_batch WHERE batch_id=?',
                        (plan['batch_id'],)).fetchone()
                    observed=0
                    if row:
                        observed=conn.execute('''SELECT COUNT(*) FROM file_import_item i
                            JOIN message_observation o ON o.observation_key=i.observation_key
                            WHERE i.batch_id=? AND o.account_key=? AND o.device_key=?
                            AND o.source_id=? AND o.source_version=?''',
                            (plan['batch_id'],mapping['account_key'],principal.device_key,
                             mapping['source_id'],mapping['source_version'])).fetchone()[0]
                        if observed!=plan['counts']['versions']:
                            raise AppError(409,'CANDIDATE_IMPORT_RECEIPT_INCOMPLETE','Business receipt and stored observations disagree.')
                    return {'format':'dfa-candidate-import-reconciliation-1','page_index':page_index,
                        'batch_id':plan['batch_id'],'business_status':row['status'] if row else 'not_committed',
                        'business_import_committed':bool(row),'observation_count':observed,
                        'authority':'pending_only','persisted':bool(row),
                        'candidate_store_marked_committed':False,'two_database_atomicity_claim':False}
            finally:
                if owned:conn.execute('ROLLBACK')
