"""Bounded host-only ChatLab document import through existing pending receipts.

No account enrollment, native identity grant, HTTP entry, collection or model.
Original documents are validated/grouped in full; business pages retain their
document hash/global row identities. Receipt-based replay never changes layout.
"""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

from .auth import Principal
from .common import AppError, canonical_json
from .db import digest, stable_key
from .file_import import FileImportService, MAX_PREVIEW_BYTES, MAX_VERSIONS, MAX_CANDIDATE_BYTES, PROTOCOL, _json_bytes
from .import_binding import ImportBindingService

ROOT = Path(__file__).resolve().parents[1]
PAGE_FORMAT = 'chatlab-document-pages-1'
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
CAPACITY_ERRORS = {'IMPORT_BATCH_OUTPUT_LIMIT','IMPORT_PREVIEW_LIMIT','IMPORT_JSON_LIMIT','IMPORT_VERSIONS_LIMIT'}

def _check(value, code):
    if not value: raise AppError(409, code, '旧聊天分批接收未通过检查；原件和已提交批次保留。')

def _hash(value): return hashlib.sha256(value).hexdigest()

def _nodes(value):
    if isinstance(value, dict): return 1 + sum(1 + _nodes(v) for v in value.values())
    if isinstance(value, list): return 1 + sum(_nodes(v) for v in value)
    return 1

def normalize_document(raw, namespace, node_exe, input_file):
    _check(type(raw) is bytes and len(raw) <= MAX_DOCUMENT_BYTES, 'CHATLAB_DOCUMENT_LIMIT')
    _check(type(input_file) is dict and input_file.get('read_mode') in ('explicit_read_only','locked_zip_member_read_only')
        and input_file == {'sha256':_hash(raw), 'size_bytes':len(raw),
        'read_mode':input_file.get('read_mode'), 'source_approved':False}, 'CHATLAB_DOCUMENT_DIGEST_MISMATCH')
    node = Path(node_exe)
    from .backup_scratch import reject_links
    reject_links(node)
    _check(node.is_absolute() and node.is_file() and node.name.lower()=='node.exe', 'CHATLAB_NODE_REQUIRED')
    env = {k:os.environ[k] for k in ('SystemRoot','WINDIR','SYSTEMROOT') if k in os.environ}
    try:
        request = canonical_json({'text':raw.decode('utf-8','strict'),'namespace':namespace})
        result = subprocess.run([str(node),str(ROOT/'tools/chatlab_document_worker.mjs')],input=request,
            capture_output=True,timeout=60,cwd=ROOT,env=env,creationflags=0x08000000 if os.name=='nt' else 0)
        _check(not result.stderr and len(result.stdout)<=128*1024*1024, 'CHATLAB_WORKER_FAILED')
        reply = json.loads(result.stdout)
        if not reply.get('ok'):
            code = reply.get('code','document_validation_failed')
            safe = code.upper() if isinstance(code,str) and re.fullmatch('[a-z_]{1,80}',code) else 'DOCUMENT_VALIDATION_FAILED'
            raise AppError(422,'CHATLAB_'+safe,'旧聊天文件格式检查未通过，未提交任何页面。')
        _check(result.returncode==0,'CHATLAB_WORKER_FAILED')
        doc = reply['document']
    except (OSError,ValueError,UnicodeError,subprocess.SubprocessError):
        raise AppError(422,'CHATLAB_WORKER_FAILED','旧聊天本机归一化未完成，未提交页面。') from None
    _check(doc['format']=='chatlab-validated-document-1' and doc['base']['file_metadata']['text_sha256']==_hash(raw), 'CHATLAB_DOCUMENT_DIGEST_MISMATCH')
    doc['base']['input_file']=dict(input_file)
    if doc['legacy_preview'] is not None: doc['legacy_preview']['input_file']=dict(input_file)
    return doc

def _mapping(binding, preview):
    return {**binding,'records':{row['source_key']:{} for row in preview['records']}}

def _batch_id(preview, mapping, principal):
    return stable_key('file-import-batch',principal.device_key,mapping['account_key'],mapping['source_id'],
        mapping['source_version'],mapping['account_namespace'],digest(preview),digest(mapping))

def _record_cost(record, metadata, binding):
    """Same expanded-byte accounting as FileImportService, with fixed-length IDs.

    The planning metadata uses six-digit page positions/counts, an upper bound
    for this reader's <=100000 records. Hash/UUID contents do not affect lengths.
    """
    cost = 0
    batch = stable_key('file-import-batch','budget')
    claims = {k:v for k,v in record.items() if k!='versions'}
    for version in record['versions']:
        body = version['original_body']
        _check(isinstance(body,str) and '\x00' not in body and len(body.encode('utf-8'))<=262144,'IMPORT_ORIGINAL_BODY_INVALID')
        opaque={'import_protocol':PROTOCOL,'batch_id':batch,'account_namespace':binding['account_namespace'],
            'source_key':record['source_key'],'record':claims,'version':version,'mapping':{},'preview_metadata':metadata}
        raw=_json_bytes(opaque,MAX_CANDIDATE_BYTES).decode('utf-8')
        observation={'observation_id':stable_key('file-import-record','budget'),'source_id':binding['source_id'],
            'source_version':binding['source_version'],'account_key':binding['account_key'],'body':body,
            'kind':'unknown','direction':'unknown','platform_success':'unknown','time_precision':'unknown',
            'occurred_lower_utc':None,'occurred_upper_utc':None,'coverage':{'state':'unknown'},
            'platform_message_key':record['platform_message_id'] if record['key_origin']=='platform_claim' else None,
            'platform_conversation_key':None,'export_candidate_json':raw}
        cost += 3*len(canonical_json(observation))+1024
    return cost

def build_pages(document, binding):
    """Pure deterministic packing; never split a message's conflicting versions."""
    metadata={**document['base'],'chatlab_page':{'format':PAGE_FORMAT,'index':999999,'count':999999,
        'source_rows':document['source_rows']}}
    base_nodes=_nodes(metadata)+2
    base_bytes=len(canonical_json(metadata))+32
    mapping_base=len(canonical_json({**binding,'records':{}}))
    groups=[];page=[];versions=cost=nodes=size=mapping_size=0
    for record in document['records']:
        next_versions=len(record['versions'])
        next_cost=_record_cost(record,metadata,binding)
        next_nodes=_nodes(record)
        next_size=len(canonical_json(record))+1
        next_mapping=len(canonical_json({record['source_key']:{}}))+1
        _check(next_versions<=MAX_VERSIONS and next_cost<=MAX_PREVIEW_BYTES and
            next_nodes+base_nodes<=200000 and next_size+base_bytes<=MAX_PREVIEW_BYTES and
            mapping_base+next_mapping<=2*1024*1024, 'CHATLAB_SINGLE_RECORD_LIMIT')
        if page and (versions+next_versions>MAX_VERSIONS or cost+next_cost>MAX_PREVIEW_BYTES or
                     nodes+next_nodes+base_nodes>200000 or size+next_size+base_bytes>MAX_PREVIEW_BYTES or
                     mapping_size+next_mapping+mapping_base>2*1024*1024):
            groups.append(page);page=[];versions=cost=nodes=size=mapping_size=0
        page.append(record);versions+=next_versions;cost+=next_cost;nodes+=next_nodes;size+=next_size
        mapping_size+=next_mapping
    if page: groups.append(page)
    pages=[]
    for index,records in enumerate(groups):
        preview={**document['base'],'chatlab_page':{'format':PAGE_FORMAT,'index':index,'count':len(groups),
            'source_rows':document['source_rows']},'records':records}
        _json_bytes(preview)
        _json_bytes(_mapping(binding,preview),2*1024*1024)
        pages.append(preview)
    return pages

class ChatlabPendingImport:
    def __init__(self,service):
        _check(isinstance(service,FileImportService),'CHATLAB_SERVICE_REQUIRED')
        self.service=service;self.db=service.db;self.bindings=ImportBindingService(service)

    def import_document(self,raw,input_file,binding,principal,*,node_exe,reauthorize,check_storage,
                        verify_source,commit_guard,after_page=None):
        """Host seam: all guard callbacks must be supplied by the owning host.

        Pages commit independently. An interrupted call is resumed with exactly
        the same file, binding and algorithm; old pages reconcile as duplicates.
        """
        _check(all(callable(x) for x in (reauthorize,check_storage,verify_source,commit_guard)), 'CHATLAB_HOST_GUARDS_REQUIRED')
        def current():
            actual=reauthorize(principal)
            _check(isinstance(actual,Principal) and actual==principal,'CHATLAB_AUTH_CHANGED')
            check_storage();verify_source()
            return self.bindings.check_target(binding,actual)
        target=current()
        document=normalize_document(raw,binding['account_namespace'],node_exe,input_file)
        _check(current()['registration_fingerprint']==target['registration_fingerprint'],'CHATLAB_REGISTRY_CHANGED')
        if document['source_rows']==0:
            return {'status':'empty','source_rows':0,'versions':0,'pages':0,'committed_pages':0,
                'duplicate_pages':0,'authority':'pending_only','document_sha256':input_file['sha256']}
        pages=None
        if document['legacy_preview'] is not None:
            old=document['legacy_preview']
            try:
                self.service.plan(old,_mapping(binding,old),principal)
                pages=[old]
            except AppError as error:
                if error.code not in CAPACITY_ERRORS: raise
        if pages is None: pages=build_pages(document,binding)
        mappings=[_mapping(binding,p) for p in pages]
        # Full service-contract preflight of every page, before the first write.
        # Commit still replans, because authorization/source state may change.
        for preview,mapping in zip(pages,mappings):
            self.service.plan(preview,mapping,principal)
        expected={_batch_id(p,m,principal) for p,m in zip(pages,mappings)}
        prefix=json.dumps([binding['account_namespace'],'chatlab-document',input_file['sha256']],ensure_ascii=False,separators=(',',':'))[:-1]+','
        with self.db.lock:
            existing={r[0] for r in self.db.conn.execute('''SELECT DISTINCT i.batch_id FROM file_import_item i
                JOIN file_import_batch b USING(batch_id) WHERE b.account_key=? AND b.source_id=? AND b.source_version=?
                AND b.account_namespace=? AND b.device_key=? AND substr(i.source_key,1,?)=?''',
                (binding['account_key'],binding['source_id'],binding['source_version'],binding['account_namespace'],principal.device_key,len(prefix),prefix))}
        _check(existing.issubset(expected),'CHATLAB_EXISTING_LAYOUT_CONFLICT')
        result={'status':'complete','source_rows':document['source_rows'],
            'versions':sum(len(r['versions']) for r in document['records']),'pages':len(pages),
            'committed_pages':0,'duplicate_pages':0,'authority':'pending_only','document_sha256':input_file['sha256']}
        for index,(preview,mapping) in enumerate(zip(pages,mappings)):
            # Same DB -> auth -> transaction order as the existing host import.
            # Authentication remains held until the durable outer COMMIT.
            with self.db.lock, commit_guard(), self.db.transaction():
                _check(current()['registration_fingerprint']==target['registration_fingerprint'],'CHATLAB_REGISTRY_CHANGED')
                plan=self.service.plan(preview,mapping,principal)
                saved=self.service.commit(preview,mapping,plan['plan_hash'],principal)
                _check(not saved.get('withdrawn',False),'CHATLAB_SOURCE_WITHDRAWN')
                _check(current()['registration_fingerprint']==target['registration_fingerprint'],'CHATLAB_REGISTRY_CHANGED')
            result['duplicate_pages' if saved['status']=='duplicate' else 'committed_pages']+=1
            if after_page is not None: after_page(index,preview,saved)
        return result

@contextmanager
def opened_chatlab_document(source,expected_source_sha256,*,document_sha256,member_index=None):
    """Read one explicit file or ZIP member without extraction or modifications."""
    from tools.large_export_snapshot import LockedRead
    for value in (expected_source_sha256,document_sha256):
        _check(isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value),'CHATLAB_SOURCE_HASH_REQUIRED')
    path=Path(source)
    _check(path.is_absolute(),'CHATLAB_ABSOLUTE_SOURCE_REQUIRED')
    with LockedRead(path) as lease:
        _check(lease.initial['bytes']<=256*1024*1024,'CHATLAB_SOURCE_LIMIT')
        content=b''.join(bytes(block) for block in lease.blocks())
        _check(_hash(content)==expected_source_sha256,'CHATLAB_SOURCE_CHANGED')
        if member_index is not None:
            _check(type(member_index) is int and 1<=member_index<=10000,'CHATLAB_MEMBER_SELECTION_INVALID')
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries=[entry for entry in archive.infolist() if not entry.is_dir()]
                _check(len(entries)<=10000 and len({e.filename for e in entries})==len(entries) and member_index<=len(entries),'CHATLAB_MEMBER_SELECTION_INVALID')
                entry=entries[member_index-1]
                _check(entry.file_size<=MAX_DOCUMENT_BYTES and entry.filename.lower().endswith('.jsonl'),'CHATLAB_DOCUMENT_LIMIT')
                raw=archive.read(entry)
        else: raw=content
        _check(len(raw)<=MAX_DOCUMENT_BYTES and _hash(raw)==document_sha256,'CHATLAB_DOCUMENT_DIGEST_MISMATCH')
        def verify(): _check(lease.info()==lease.initial,'CHATLAB_SOURCE_CHANGED')
        yield raw,{'sha256':document_sha256,'size_bytes':len(raw),
            'read_mode':'explicit_read_only' if member_index is None else 'locked_zip_member_read_only','source_approved':False},verify
        _check(lease.hash_again()['sha256']==expected_source_sha256,'CHATLAB_SOURCE_CHANGED')


def import_chatlab_file(app,token,binding,*,source,expected_source_sha256,document_sha256,
                        member_index=None,node_exe,powershell_exe):
    """Explicit protected host entry, with existing source selection and login.

    The approval is a bounded file/member allowlist, not a grant of identity or
    conversation authority. Raw values remain in the local protected workspace.
    """
    from .dsh_app import Application
    from .local_pending_import import check_import_storage
    from .backup_scratch import reject_links
    _check(isinstance(app,Application) and type(binding) is dict,'CHATLAB_APPLICATION_REQUIRED')
    account=binding.get('account_key')
    def authenticate(_=None):
        current=app.auth.authenticate(token,scope='identities:write',account_key=account)
        _check(current.role=='manager','CHATLAB_MANAGER_REQUIRED')
        return current
    principal=authenticate()
    check_import_storage(app,powershell_exe)
    target=ImportBindingService(app.file_import).check_target(binding,principal)
    with app.db.lock:
        owner=app.db.conn.execute('SELECT platform,platform_uid FROM own_account WHERE account_key=?',(account,)).fetchone()
        _check(owner and owner['platform']=='douyin' and re.fullmatch('[0-9]{1,128}',owner['platform_uid'])
            and stable_key('account','douyin',owner['platform_uid'])==account,'CHATLAB_OWNER_UID_REQUIRED')
    path=Path(source)
    _check(path.is_absolute(),'CHATLAB_ABSOLUTE_SOURCE_REQUIRED')
    reject_links(path)
    path=path.resolve()
    _check(not path.is_relative_to(app.paths.root.resolve()) and
        not app.paths.root.resolve().is_relative_to(path.parent),'CHATLAB_SOURCE_SEPARATE_REQUIRED')
    approval=json.loads(target['source']['evidence_json']).get('chatlab_import')
    expected={'format':'dfa-approved-chatlab-import-1','source_path':str(path),
        'source_sha256':expected_source_sha256,'account_key':account,'account_namespace':binding['account_namespace']}
    _check(type(approval) is dict and set(approval)=={*expected,'documents'} and
        all(approval[k]==v for k,v in expected.items()),'CHATLAB_SELECTION_NOT_REGISTERED')
    documents=approval['documents']
    _check(type(documents) is list and 1<=len(documents)<=10000,'CHATLAB_SELECTION_NOT_REGISTERED')
    indexes=set()
    for item in documents:
        _check(type(item) is dict and set(item)=={'sha256','member_index'} and
            isinstance(item['sha256'],str) and re.fullmatch('[0-9a-f]{64}',item['sha256']) and
            (item['member_index'] is None or type(item['member_index']) is int and 1<=item['member_index']<=10000),
            'CHATLAB_SELECTION_NOT_REGISTERED')
        _check(item['member_index'] not in indexes,'CHATLAB_SELECTION_NOT_REGISTERED')
        indexes.add(item['member_index'])
    _check(not (None in indexes and len(indexes)>1) and
        {'sha256':document_sha256,'member_index':member_index} in documents,'CHATLAB_SELECTION_NOT_REGISTERED')
    @contextmanager
    def guard():
        with app.auth.authorized(token,scope='identities:write',account_key=account) as current:
            _check(current==principal,'CHATLAB_AUTH_CHANGED')
            yield
    # The same registration fingerprint is checked before reading, after the
    # bounded normalization, and at both ends of each page transaction.
    def storage():
        check_import_storage(app,powershell_exe)
        _check(ImportBindingService(app.file_import).check_target(binding,principal)['registration_fingerprint']==
            target['registration_fingerprint'],'CHATLAB_REGISTRY_CHANGED')
    storage()
    with opened_chatlab_document(path,expected_source_sha256,document_sha256=document_sha256,
            member_index=member_index) as (raw,input_file,verify_source):
        return ChatlabPendingImport(app.file_import).import_document(raw,input_file,binding,principal,
            node_exe=node_exe,reauthorize=authenticate,check_storage=storage,verify_source=verify_source,commit_guard=guard)


def import_registered_chatlab_document(data_root,operator,binding,**kwargs):
    from .local_pending_import import registered_import_application
    with registered_import_application(data_root,operator,binding,kwargs['powershell_exe']) as (app,token):
        return import_chatlab_file(app,token,binding,**kwargs)
