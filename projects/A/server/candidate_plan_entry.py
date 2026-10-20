"""Explicit read-only page plan; no candidate creation, stage, commit or trust upgrade."""
import hashlib,hmac,json
from pathlib import Path
from .backup_scratch import reject_links
from .common import AppError
from .db import digest
from .export_candidates import ExportCandidateStore,CandidateError,check_profile
from .export_candidate_import import CandidateImportBridge
from .import_binding import ImportBindingService

def read_independent_profile(path,candidate_dir,snapshot_dir,*,expected_sha256=None):
    path=Path(path)
    if not path.is_absolute():raise AppError(400,'CANDIDATE_PROFILE_PATH_REQUIRED','独立固定profile需要明确绝对文件路径')
    reject_links(path);resolved=path.resolve()
    if any(resolved.is_relative_to(Path(root).resolve()) for root in (candidate_dir,snapshot_dir)):
        raise AppError(400,'CANDIDATE_PROFILE_NOT_INDEPENDENT','外部固定profile不能取自候选自身或来源目录')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('duplicate profile key')
            value[key]=item
        return value
    try:
        with path.open('rb') as stream:raw=stream.read(8193)
        if len(raw)>8192:raise ValueError('profile budget')
        if expected_sha256 is not None and not hmac.compare_digest(hashlib.sha256(raw).hexdigest(),expected_sha256):
            raise AppError(409,'CANDIDATE_PROFILE_CHANGED','独立profile文件已变化；未生成新计划')
        profile=json.loads(raw.decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        check_profile(profile)
    except CandidateError:raise
    except (OSError,ValueError,UnicodeError,RecursionError):
        raise AppError(400,'CANDIDATE_PROFILE_INVALID','独立固定profile不可读取或未通过有限结构核验') from None
    return profile

def plan_unknown_page(app,binding,principal,*,candidate_dir,snapshot_dir,profile_file,page_index,reauthorize,page_count=1,profile_sha256=None):
    if not app.read_only or not app.db.read_only:
        raise AppError(400,'CANDIDATE_PLAN_READ_ONLY_REQUIRED','分页计划需业务只读连接，未保存任何业务')
    if app.file_import is None:raise AppError(409,'IMPORT_MIGRATION_REQUIRED','已有导入schema尚未初始化；不自动迁移')
    if not callable(reauthorize):raise AppError(400,'CANDIDATE_PLAN_AUTH_REQUIRED','只读计划需持续核验已有认证')
    if type(page_index) is not int or page_index<1:raise AppError(400,'CANDIDATE_PAGE_INVALID','明确页号必须为正整数')
    if type(page_count) is not int or not 1<=page_count<=20:raise AppError(400,'CANDIDATE_PAGE_RANGE_INVALID','一次只读审查需明确1至20页')
    if profile_sha256 is not None and (type(profile_sha256) is not str or len(profile_sha256)!=64 or any(c not in '0123456789abcdef' for c in profile_sha256)):
        raise AppError(400,'CANDIDATE_PROFILE_HASH_REQUIRED','固定profile摘要格式无效')
    service=ImportBindingService(app.file_import)
    # Authenticate the target before profile/candidate/snapshot filesystem access.
    current=reauthorize(principal)
    if current!=principal:raise AppError(403,'CANDIDATE_IMPORT_AUTH_CHANGED','本机认证已变化')
    service.check_target(binding,current)
    for value in (candidate_dir,snapshot_dir):
        if not Path(value).is_absolute():raise AppError(400,'CANDIDATE_PATH_REQUIRED','候选与来源需要明确绝对目录')
        reject_links(value)
    store=None
    try:
        profile=read_independent_profile(profile_file,candidate_dir,snapshot_dir,expected_sha256=profile_sha256)
        if profile['source_kind']!='unverified_export' or profile['account_namespace']!=binding['account_namespace']:
            raise AppError(409,'CANDIDATE_IMPORT_REVIEW_PROFILE','独立profile与所选未核验来源范围不一致')
        store=ExportCandidateStore(candidate_dir,expected_profile=profile).open(writable=False)
        binding_plan=service.plan_for_store(binding,current,store)
        if page_index+page_count-1>binding_plan['candidate_head']['pages']:
            raise AppError(400,'CANDIDATE_PAGE_RANGE_INVALID','所选页范围超过完整候选；不重复来源重放')
        bridge=CandidateImportBridge(store,app.file_import,snapshot_dir=snapshot_dir)
        pages=[]
        with bridge.start_review_session(binding,current,reauthorize=reauthorize) as review:
            for selected_page in range(page_index,page_index+page_count):
                # Keep a short coherent business snapshot per page. A single
                # long transaction would hide external registration revocation.
                with app.db.lock:
                    if app.db.conn.in_transaction:raise AppError(409,'CANDIDATE_PLAN_BUSY','业务读取事务已占用，未生成新计划')
                    app.db.conn.execute('BEGIN')
                    try:
                        service.verify_store_plan(binding,reauthorize(principal),store,binding_plan['binding_quote'])
                        with review.locked_page_preview(selected_page,scope_key=review.scope_key) as view:
                            mapping={**binding,'records':{row['source_key']:{} for row in view['preview']['records']}}
                        plan=bridge.plan_page(selected_page,mapping,principal,session=review)
                    finally:app.db.conn.rollback()
                counts=plan['business_plan']['counts']
                pages.append({'page_index':selected_page,
                    'counts':{key:counts[key] for key in ('records','versions','conflicts','pending')},
                    'page_quote_hash':plan['quote_hash'],'business_plan_hash':plan['business_plan']['plan_hash']})
        # Success is released only after final source verification on context exit.
        result={
            'snapshot_sha256':profile['snapshot_sha256'],'profile_digest':digest(profile),
            'binding_scope_digest':digest(binding),'binding_quote':binding_plan['binding_quote'],
            'authority':'pending_only','record_identity_mapping':'explicit_unknown',
            'source_revalidated':True,'candidate_database_read_only':True,'business_database_read_only':True,
            'source_approved':False,'account_ownership':'unknown','private_chat_scope':'unknown',
            'persisted':False,'business_import_committed':False,'commit_authorized':False,
            'real_write_enabled':False,'two_database_atomicity_claim':False}
        if page_count==1:
            result.update({'format':'dfa-candidate-page-plan-cli-1',**pages[0]})
        else:
            result.update({'format':'dfa-candidate-page-range-plan-cli-1','first_page_index':page_index,
                'page_count':page_count,'pages':pages,'counts':{
                    key:sum(page['counts'][key] for page in pages) for key in ('records','versions','conflicts','pending')},
                'counts_scope':'selected_pages_only'})
        return result
    except CandidateError as error:
        raise AppError(409,error.code,'候选或独立来源审查未通过；未发布成功计划、未保存业务') from None
    finally:
        if store is not None:store.close()
