"""Exact profile-token lookup in a caller-pinned pending artifact.

The host callback authorizes this request, not a person or platform account.
Only root raw_data sender pairs can be returned. All graph conflicts remain
visible, including conflicts contributed by another observation role.
"""
import hashlib
import inspect
import re

from .common import AppError, canonical_json
from .identity_candidate_files import load_index

FORMAT='pending-profile-lookup-1'
MAX_PROVENANCES=64
FLAGS={'verified':False,'source_permission':'not_assessed','account_binding':'unverified',
    'owner_verified':False,'private_context_verified':False,'context_binding':'unverified',
    'latest_head_verified':False,'formal_identity_match':False,'conversation_key':None,
    'business_writes':0,'identity_merges':0}

def _fail(code='PENDING_PROFILE_LOOKUP_INVALID',status=400):
    raise AppError(status,code,'Pending profile lookup could not be verified.')

def _label(value):
    if type(value)is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',value)is None:_fail()

def _dependency(callback,scope):
    if not callable(callback):_fail('PENDING_PROFILE_DEPENDENCY_REQUIRED',403)
    try:result=callback(scope)
    except AppError:raise
    except Exception:_fail('PENDING_PROFILE_DEPENDENCY_REJECTED',403)
    if inspect.isawaitable(result):
        # A host gate must be synchronous. Avoid an unawaited-coroutine warning
        # that would otherwise expose its module path on stderr.
        if inspect.iscoroutine(result):result.close()
        _fail('PENDING_PROFILE_DEPENDENCY_REJECTED',403)
    if result is not None:_fail('PENDING_PROFILE_DEPENDENCY_REJECTED',403)

def lookup_profile_candidate(*,index_path,expected_index_sha256,account_key,
                             provider_namespace,sec_uid,require_dependency):
    """Return zero/pending/conflict metadata; never infer owner or conversation.

    ``require_dependency`` receives the same immutable tuple before file I/O
    and before return: (account_key, namespace, external artifact SHA, sec_uid).
    Normal synchronous return is None; a boolean is not authentication.
    """
    _label(account_key);_label(provider_namespace)
    if type(expected_index_sha256)is not str or re.fullmatch(r'[0-9a-f]{64}',expected_index_sha256)is None:_fail()
    if type(sec_uid)is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,256}',sec_uid)is None:_fail()
    scope=(account_key,provider_namespace,expected_index_sha256,sec_uid)
    _dependency(require_dependency,scope)
    ledger=load_index(index_path,expected_index_sha256)
    match=ledger.lookup(account_key=account_key,provider_namespace=provider_namespace,
        object_kind='person',subject_role='sender',identity_kind='sec_uid',identity_value=sec_uid)
    state=ledger.export_state()
    withdrawn={(r['account_key'],r['provider_namespace'],r['source_id'],r['source_version'])for r in state['withdrawals']}
    roots={}
    for item in state['records']:
        row=item['observation']
        if row['account_key']!=account_key or row['provider_namespace']!=provider_namespace:continue
        if row['subject_role']!='sender'or row['field_scope']!='raw_data'or row['sec_uid']!=sec_uid or row['uid']is None:continue
        origin=(account_key,provider_namespace,row['source_id'],row['source_version'])
        if origin in withdrawn:continue
        key=(row['uid'],row['sec_uid'])
        group=roots.setdefault(key,{'digests':[],'sources':set()})
        group['digests'].append(item['evidence_digest'])
        group['sources'].add((row['source_id'],row['source_version'],row['snapshot_digest']))
        if len(group['sources'])>MAX_PROVENANCES:_fail('PENDING_PROFILE_LOOKUP_LIMIT',413)
    conflict=bool(match['global_identity_conflict'])
    candidates=[]
    for (uid,sec),group in sorted(roots.items()):
        candidates.append({'uid':uid,'sec_uid':sec,'state':'conflict'if conflict else'pending',
            'evidence_count':len(group['digests']),
            'evidence_set_digest':hashlib.sha256(canonical_json(sorted(group['digests']))).hexdigest(),
            'source_refs':[{'source_id':s,'source_version':v,'snapshot_digest':d}for s,v,d in sorted(group['sources'])]})
    result={'format':FORMAT,'status':'conflict'if conflict else('pending'if candidates else'zero'),
        'account_key':account_key,'provider_namespace':provider_namespace,
        'artifact_sha256':expected_index_sha256,'identity_kind':'sec_uid','sec_uid':sec_uid,
        'candidate_count':len(candidates),'candidates':candidates,'global_identity_conflict':conflict,
        'selection_scope':'root_raw_data_sender_only',**FLAGS}
    _dependency(require_dependency,scope)
    return result
