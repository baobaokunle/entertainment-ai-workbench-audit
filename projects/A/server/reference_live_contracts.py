"""Offline B05/B06 previews. No network, database, authority or ingestion side effects."""
import hashlib
import json
import re
from .common import AppError, canonical_json
from .message_facts import normalize_time
from .rank_facts import metric

FIELDS = {'contract_version','kind','source_id','source_version','source_account_namespace',
          'account_key','revision_id','source_revision','room_id','captured_at_utc',
          'period','context_before','context_after','ranks','gaps'}
CONTEXT = {'battle_id','battle_id_origin','left_anchor_id','right_anchor_id'}

def fail(code='LIVE_INPUT_INVALID', message='Invalid offline live contract.'):
    raise AppError(422, code, message)

def exact(value, fields):
    if type(value) is not dict or set(value) != fields: fail('LIVE_FIELDS_INVALID')

def text(value, limit=200):
    if type(value) is not str or not value.strip() or len(value)>limit or '\x00' in value: fail()

def identifier(value):
    if type(value) is not str or not re.fullmatch(r'[1-9][0-9]{0,19}', value) or int(value)>18446744073709551615: fail('LIVE_ID_INVALID')

def ordinary(value, depth=0, counter=None):
    """Bound before encoding; rejects cycles, floats and non-JSON objects."""
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if depth>12 or counter[0]>40000: raise AppError(413,'LIVE_LIMIT','Input exceeds finite budget.')
    if value is None or type(value) is bool: return
    if type(value) is int:
        if abs(value)>9007199254740991: fail('LIVE_INTEGER_RANGE')
        return
    if type(value) is str:
        if len(value)>1000: raise AppError(413,'LIVE_LIMIT','String exceeds budget.')
        return
    if type(value) is list:
        if len(value)>10000: raise AppError(413,'LIVE_LIMIT','List exceeds budget.')
        for item in value: ordinary(item,depth+1,counter)
        return
    if type(value) is dict:
        if len(value)>30 or any(type(k) is not str for k in value): fail()
        if any(len(k)>200 for k in value): raise AppError(413,'LIVE_LIMIT','Object key exceeds budget.')
        for item in value.values(): ordinary(item,depth+1,counter)
        return
    fail('LIVE_JSON_INVALID')

def context(value):
    exact(value,CONTEXT)
    for key in ('left_anchor_id','right_anchor_id'): identifier(value[key])
    if value['left_anchor_id']==value['right_anchor_id']: fail('LIVE_SIDE_INVALID')
    if value['battle_id_origin'] not in ('direct','inferred','unknown'): fail()
    if value['battle_id_origin']=='unknown':
        if value['battle_id'] is not None: fail('LIVE_BATTLE_INVALID')
    else: identifier(value['battle_id'])

def normalize_preview(payload, *, previous=None):
    """Return validated copy and ingestion blockers; never promote to accepted facts.

    previous is another raw contract, not caller-supplied metadata. Duplicate and
    correction comparisons cover the full snapshot and source/version/scope.
    """
    ordinary(payload)
    if len(canonical_json(payload))>2*1024*1024: raise AppError(413,'LIVE_LIMIT','Input exceeds byte budget.')
    exact(payload,FIELDS)
    p=json.loads(canonical_json(payload))
    if p['contract_version']!='reference-live-v1' or p['kind'] not in ('contribution_snapshot','pk_snapshot'): fail()
    for key in ('source_id','source_version','source_account_namespace','account_key','revision_id'): text(p[key])
    if type(p['source_revision']) is not int or not 1<=p['source_revision']<=9007199254740991: fail()
    identifier(p['room_id'])
    p['captured_at_utc']=normalize_time(p['captured_at_utc'])
    if not p['captured_at_utc']: fail('LIVE_TIME_INVALID')
    exact(p['period'],{'key','label','start_utc','end_utc','basis'})
    for key in ('key','label'): text(p['period'][key])
    if p['period']['basis'] not in ('current_live_window','current_pk_window'): fail('LIVE_PERIOD_INVALID')
    expected='current_pk_window' if p['kind']=='pk_snapshot' else 'current_live_window'
    if p['period']['basis']!=expected: fail('LIVE_PERIOD_INVALID')
    for key in ('start_utc','end_utc'): p['period'][key]=normalize_time(p['period'][key])
    start,end=p['period']['start_utc'],p['period']['end_utc']
    if not start or not end or not start<end or not start<=p['captured_at_utc']<=end: fail('LIVE_PERIOD_INVALID')
    if type(p['gaps']) is not list or len(p['gaps'])>100: fail()
    for gap in p['gaps']: text(gap)
    if len(set(p['gaps']))!=len(p['gaps']): fail()
    reasons=['source_unverified','identity_mapping_required','current_window_not_30_day_rank','no_session_catalog_or_watch_evidence']
    if p['kind']=='pk_snapshot':
        context(p['context_before']);context(p['context_after'])
        if p['context_before']!=p['context_after']: reasons.append('context_changed')
        if any(c['battle_id_origin']!='direct' for c in (p['context_before'],p['context_after'])): reasons.append('battle_identity_not_direct')
        sides={'left','right'}
    else:
        if p['context_before'] is not None or p['context_after'] is not None: fail('LIVE_CONTEXT_INVALID')
        sides={'room'}
    if type(p['ranks']) is not list or len(p['ranks'])!=len(sides): fail('LIVE_RANK_INVALID')
    found=set();total=0
    for rank in p['ranks']:
        exact(rank,{'side','rank_kind','coverage','is_complete','pagination_terminal','rows'})
        if type(rank['side']) is not str or rank['side'] not in sides or rank['side'] in found: fail('LIVE_SIDE_INVALID')
        found.add(rank['side'])
        expected='pk_contribution' if p['kind']=='pk_snapshot' else 'contribution'
        if rank['rank_kind']!=expected or rank['coverage'] not in ('complete','partial','unknown'): fail('LIVE_RANK_INVALID')
        if type(rank['is_complete']) is not bool or type(rank['pagination_terminal']) is not bool: fail()
        if rank['coverage']=='complete' and not (rank['is_complete'] and rank['pagination_terminal']): fail('LIVE_COMPLETENESS_INVALID')
        if rank['coverage']!='complete' and rank['is_complete']: fail('LIVE_COMPLETENESS_INVALID')
        if type(rank['rows']) is not list or len(rank['rows'])>5000: raise AppError(413,'LIVE_LIMIT','Rank row budget exceeded.')
        total+=len(rank['rows'])
        if rank['coverage']=='unknown' and rank['rows']: fail('LIVE_COMPLETENESS_INVALID')
        if rank['coverage']!='complete': reasons.append(rank['side']+'_coverage_'+rank['coverage'])
        if not rank['pagination_terminal']: reasons.append(rank['side']+'_pagination_incomplete')
        seen=set();positions=set()
        for row in rank['rows']:
            exact(row,{'source_person_id','position','metric'})
            identifier(row['source_person_id'])
            if type(row['position']) is not int or not 1<=row['position']<=1000000: fail()
            if row['source_person_id'] in seen or row['position'] in positions: fail('LIVE_ROW_DUPLICATE')
            seen.add(row['source_person_id']);positions.add(row['position'])
            metric(row['metric'])
            if row['metric']['state']=='known' and not re.fullmatch(r'(0|[1-9][0-9]*)(\.[0-9]+)?',row['metric']['value']): fail('LIVE_SCORE_INVALID')
    if total>10000: raise AppError(413,'LIVE_LIMIT','Total row budget exceeded.')
    if p['gaps']: reasons.append('declared_gaps')
    state='new'
    if previous is not None:
        old=normalize_preview(previous)['payload']
        if old['revision_id']==p['revision_id']:
            if old!=p: raise AppError(409,'LIVE_REVISION_REUSED','Revision ID cannot change content.')
            state='duplicate'
        else:
            scope=lambda v: (v['kind'],v['source_id'],v['source_version'],v['source_account_namespace'],v['account_key'],v['room_id'],v['period'],v['context_before'],v['context_after'])
            if scope(old)!=scope(p): state='different_scope'
            elif p['source_revision']<=old['source_revision']: state='superseded'
            else: state='revision'
    return {'payload':p,'metadata':{'preview_only':True,'eligible_for_auto_ingestion':False,
            'comparison':state,'digest':hashlib.sha256(canonical_json(p)).hexdigest(),
            'blockers':sorted(set(reasons)),'gaps':p['gaps'],
            'rank_contract_mapping':'requires_verified_person_keys_and_registered_source',
            'session_contract_mapping':'unsupported_without_direct_catalog_boundaries_and_metrics'}}
