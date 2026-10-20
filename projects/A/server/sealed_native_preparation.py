"""Explicit E3 snapshot preparation; no page, messages or automatic submission.

The host separately reviews a pinned plan and an existing fact-adoption record.
DPAPI custody and newly calculated hashes are not independent fact approval.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from .common import AppError, canonical_json
from .db import Database, digest, stable_key
from .local_device_pairing import _write_new
from .local_workspace_preparation import local_f_path, _shell_path, _acl, _file, inspect_prepared_directory
from .native_registration import _record, _review_basis, _text
from .native_runtime import policy_valid, timestamp
from .paths import Paths
from .security import DPAPIProtector
from .settings import validate_settings

PLAN_FIELDS = {'format','evidence_sha256','evidence_bytes','evidence_expires_at','observed_at',
    'fact_review_id','fact_review_sha256','device_key','operator_key','extension_id','review_id','review_reason',
    'runtime_expires_at','account_namespace','owner_provider_namespace','provider_namespace','source_slug'}


def fail(code='NATIVE_PREPARATION_INVALID'):
    raise AppError(400, code, '身份登记准备条件不符，已有资料保留。')


def strict_json(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: fail()
            result[key]=value
        return result
    def invalid(_): fail()
    try:
        if type(raw) is not bytes or not 0<len(raw)<=65536 or raw.startswith(b'\xef\xbb\xbf'): fail()
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=unique,parse_constant=invalid)
        pending=[(value,0)];visited=0
        while pending:
            item,depth=pending.pop();visited+=1
            if depth>32 or visited>4096: fail()
            if type(item) is dict: pending.extend((child,depth+1) for child in item.values())
            elif type(item) is list: pending.extend((child,depth+1) for child in item)
        return value
    except (ValueError,UnicodeError,RecursionError):
        fail()


def validate_plan(plan, adoption, *, now=None):
    now=now or datetime.now(timezone.utc)
    if type(plan) is not dict or set(plan)!=PLAN_FIELDS or plan['format']!='native-sealed-preparation-plan-1': fail()
    for key in PLAN_FIELDS-{'evidence_bytes','format','evidence_expires_at','observed_at','runtime_expires_at','review_reason'}:
        if not _text(plan[key],60 if key=='source_slug' else 200): fail()
    if any(not re.fullmatch('[0-9a-f]{64}',plan[k]) for k in ('evidence_sha256','fact_review_sha256')): fail()
    if type(plan['evidence_bytes']) is not int or not 1<=plan['evidence_bytes']<=65536: fail()
    if not re.fullmatch('[a-p]{32}',plan['extension_id']): fail()
    expiry=timestamp(plan['evidence_expires_at']); runtime=timestamp(plan['runtime_expires_at'])
    if not now<runtime<=min(expiry,now+timedelta(hours=24)) or timestamp(plan['observed_at'])>now: fail('NATIVE_PREPARATION_EXPIRED')
    if (type(adoption) is not dict or adoption.get('format')!='task001-evidence-adoption-review-1'
            or adoption.get('status')!='accepted_for_limited_historical_sample_facts'
            or adoption.get('task_item_1_fact_review_complete') is not True
            or adoption.get('scope',{}).get('native_snapshot_at')!=plan['observed_at']
            or adoption.get('scope',{}).get('owner_alias')!='O1'
            or 'R1' not in adoption.get('scope',{}).get('peer_aliases',[])
            or adoption.get('scope',{}).get('type_semantics_build',{}).get('enum')!={'ONE_TO_ONE_CHAT':1,'GROUP_CHAT':2}
            or adoption.get('sealed_refs_cited_not_opened',{}).get('native',{}).get('sha256')!=plan['evidence_sha256']
            or adoption.get('original_retention_deadline_unchanged')!=plan['evidence_expires_at']): fail('NATIVE_PREPARATION_REVIEW_MISMATCH')


def build_setup(document,plan,adoption,*,now=None):
    """Select scalar fields from the reviewed R1 snapshot; never return its blob."""
    validate_plan(plan,adoption,now=now)
    try:
        fields=document['sources']['R1']['fields']
        backend=document['sources']['backend']['fields']
        owner,peer,sec,conversation=(fields[k] for k in ('loggedUserId','peerUserId','peerSecUid','id'))
        if (type(fields['type']) is not int or fields['type']!=1 or type(owner) is not str or
                type(peer) is not str or not re.fullmatch('[0-9]{1,256}',owner) or
                not re.fullmatch('[0-9]{1,256}',peer) or owner==peer or backend['user_id_str']!=owner): fail()
    except (KeyError,TypeError):
        fail()
    account=stable_key('account','douyin',owner);slug=plan['source_slug']
    metadata_id='native-metadata-'+slug
    obs={'format':'native-identity-observation-1','observation_id':'e3-r1-'+slug,'account_key':account,
        'owner_uid':owner,'peer_uid':peer,'peer_sec_uid':sec,'platform_conversation_key':conversation,
        'conversation_type':'private','snapshot_sha256':plan['evidence_sha256'],'snapshot_record_ref':'/sources/R1',
        'observed_at':plan['observed_at'],'fact_review_id':plan['fact_review_id'],'fact_review_sha256':plan['fact_review_sha256'],
        'scope':'douyin_web_private_snapshot'}
    metadata={'source_id':metadata_id,'source_version':'1','source_kind':'qualified_adapter','verified':True,
        'capabilities':{'native_identity_metadata':True,'account_keys':[account]},'evidence':{'native_identity_observations':[obs]}}
    record={'format':'native-profile-metadata-binding-1','account_key':account,'owner_uid':owner,'peer_uid':peer,
        'peer_sec_uid':sec,'platform_conversation_key':conversation,'conversation_type':'private','native_device_key':plan['device_key'],
        'observation_key':stable_key('native_observation',metadata_id,'1',obs['observation_id']),
        'basis':'independent_current_registration','metadata_source_id':metadata_id,'metadata_source_version':'1',
        'metadata_source_digest':digest(metadata)}
    registration={'account_key':account,'source_id':'native-reviewed-'+slug,'source_version':'1',
        'approval_source_id':'native-approval-'+slug,'approval_source_version':'1'}
    review={'format':'native-profile-review-basis-1','fact_review':{'review_id':plan['fact_review_id'],'sha256':plan['fact_review_sha256']},
        'evidence_refs':[{'sha256':plan['evidence_sha256'],'record_ref':'/sources/R1'}],
        'execution_review':{'review_id':plan['review_id'],'record_digest':digest(record),'operator_key':plan['operator_key'],
            'purpose':'approve_current_local_registration','reason':plan['review_reason'],'registration':registration}}
    policy={'source_id':'native-runtime-'+slug,'source_version':'1','reader':'native-store-metadata-1',
        **{key:plan[key] for key in ('account_namespace','owner_provider_namespace','provider_namespace')},'expires_at':plan['runtime_expires_at']}
    if not _record(record,account,plan['device_key']) or not _review_basis(review,record,registration): fail()
    policy_valid(policy)
    return {'format':'local-native-setup-1',**registration,'record':record,'review_basis':review,'observation':obs,
        'identity_source':{'source_id':'native-identity-'+slug,'source_version':'1'},'runtime_policy':policy}


def prepare_registration(data_root,plan_file,reviewed_plan_sha256,evidence_file,fact_review_file,powershell_exe):
    """Create only an empty protected workspace, normal auth and two requests.

    Does not submit sources, open HTTP, enroll a browser, or start any worker.
    Interrupted files stay in place and block retries; no overwrite/recovery.
    """
    from tools.large_export_snapshot import LockedRead
    root=local_f_path(data_root);paths=Paths.from_root(str(root));shell=_shell_path(powershell_exe)
    directories=inspect_prepared_directory(root,shell)
    plan_path=local_f_path(plan_file);evidence_path=local_f_path(evidence_file);fact_path=local_f_path(fact_review_file)
    inputs=(plan_path,evidence_path,fact_path)
    if len(set(inputs))!=3 or any(p.is_relative_to(root) for p in inputs): fail()
    if type(reviewed_plan_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',reviewed_plan_sha256): fail()
    for directory in ('runtime','keys','staging','backups','journal','sync/incoming','sync/outgoing'):
        if any((root/directory).iterdir()): fail('NATIVE_PREPARATION_NEW_REQUIRED')
    for name in ('config.json','config.tmp','sync-config.json'):
        if (root/name).exists(): fail('NATIVE_PREPARATION_NEW_REQUIRED')
    setup_path=root/'validation'/'native-setup.json';revoke_path=root/'validation'/'native-revoke.json'
    if setup_path.exists() or revoke_path.exists(): fail('NATIVE_PREPARATION_NEW_REQUIRED')
    with ExitStack() as stack:
        readers=[stack.enter_context(LockedRead(p)) for p in inputs]
        if any(not 0<reader.initial['bytes']<=65536 for reader in readers): fail()
        raw_plan,encrypted,raw_fact=[b''.join(bytes(block) for block in reader.blocks()) for reader in readers]
        if hashlib.sha256(raw_plan).hexdigest()!=reviewed_plan_sha256: fail('NATIVE_PREPARATION_PLAN_CHANGED')
        plan=strict_json(raw_plan);adoption=strict_json(raw_fact)
        validate_plan(plan,adoption)
        if (len(encrypted)!=plan['evidence_bytes'] or hashlib.sha256(encrypted).hexdigest()!=plan['evidence_sha256'] or
                hashlib.sha256(raw_fact).hexdigest()!=plan['fact_review_sha256']): fail('NATIVE_PREPARATION_SOURCE_CHANGED')
        _acl(shell,[evidence_path.parent,evidence_path])
        # E3 is a raw Windows CurrentUser DPAPI blob, not A's framed DPAPI file.
        document=strict_json(DPAPIProtector()._crypt(encrypted,decrypt=True))
        setup=build_setup(document,plan,adoption)
        del document
        config=validate_settings({'device_key':plan['device_key'],'device_role':'manager',
            'extension_ids':[plan['extension_id']],'source_mode':'unverified'})
        revoke={'format':'local-native-runtime-revoke-1','account_key':setup['account_key'],
            'operator_key':plan['operator_key'],'source_id':setup['runtime_policy']['source_id'],'source_version':'1'}
        raw_setup=canonical_json(setup);raw_revoke=canonical_json(revoke)
        if len(raw_setup)>32768 or len(raw_revoke)>32768: fail()
        # Recheck protected destinations immediately before first exclusive write.
        inspect_prepared_directory(root,shell)
        _write_new(paths.settings,canonical_json(config));_file(paths.settings)
        _write_new(paths.db,b'');_file(paths.db)
        database=Database(paths.db,create=True)
        try:
            database.set_runtime_identity(plan['device_key'])
            from .migrations import install_candidate_features
            from .file_import import initialize_import_schema
            install_candidate_features(database);initialize_import_schema(database)
            active=[paths.db]+[p for suffix in ('-wal','-shm','-journal') if (p:=Path(str(paths.db)+suffix)).exists()]
            for path in active: _file(path)
            _acl(shell,directories+active+[paths.settings])
        finally:
            database.close()
        from .workbench_pairing_entry import prepare_workbench_pairing
        pairing=prepare_workbench_pairing(paths,config,plan['operator_key'],[setup['account_key']],plan['extension_id'])
        if pairing['existing_registration']: fail('NATIVE_PREPARATION_NEW_REQUIRED')
        _write_new(setup_path,raw_setup);_file(setup_path)
        _write_new(revoke_path,raw_revoke);_file(revoke_path)
        from .local_pending_import import check_import_workspace
        if check_import_workspace(paths,shell,prepared_login=True)!=config: fail()
        _acl(shell,[setup_path,revoke_path,evidence_path.parent,evidence_path])
        if any(reader.info()!=reader.initial for reader in readers): fail('NATIVE_PREPARATION_SOURCE_CHANGED')
        validate_plan(plan,adoption)
        return {'format':'native-registration-prepared-1','prepared':True,'account_count':1,'peer_count':1,
            'empty_database_initialized':True,'normal_local_enrollment_prepared':True,'sources_submitted':False,
            'plan_sha256':reviewed_plan_sha256,'evidence_sha256':plan['evidence_sha256'],
            'setup_sha256':hashlib.sha256(raw_setup).hexdigest(),'revoke_sha256':hashlib.sha256(raw_revoke).hexdigest(),
            'runtime_expires_at':plan['runtime_expires_at'],'chat_bodies_read':False,'history_container_opened':False,
            'browser_operated':False,'service_started':False,'general_writes_enabled':False,'current_page_verified':False}
