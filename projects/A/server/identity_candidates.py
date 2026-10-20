"""Bounded, account/namespace-scoped identity observations. No trusted aliases.

Inputs describe finite same-object fields; they do not authenticate a source,
owner, peer, group membership or anchor user. Original text remains elsewhere.
This module performs no I/O, database enrollment, synchronization or merging.
"""
from collections import Counter,defaultdict
import hashlib
import re
from threading import RLock

from .common import AppError,canonical_json

FORMAT='identity-candidate-ledger-1'
MAX_RECORDS=1_000_000
MAX_EDGES=100_000
MAX_BATCH=2000
MAX_STATE_BYTES=512*1024**2
MAX_WITHDRAWALS=10000
MAX_LOOKUP_EDGES=2000
FIELDS=frozenset(('account_key','provider_namespace','object_kind','subject_role','context_kind',
    'source_id','source_version','snapshot_digest','record_ref','subject_uid','uid','sec_uid',
    'field_scope','uid_field','sec_uid_field'))
ROLES=frozenset(('sender','private_peer','group_member','anchor_subject'))
SENDER_SCOPES={
    'raw_data':('sender_uid','sender_sec_uid'),
    'raw_data.sender':('uid','sec_uid'),
    'raw_data.sender_info':('uid','sec_uid'),
    'raw_data.data.sender':('uid','sec_uid'),
    'raw_data.message.sender':('uid','sec_uid'),
}
ROLE_SCOPES={'sender':SENDER_SCOPES,'private_peer':{'conversation.peer':('uid','sec_uid')},
    'group_member':{'conversation.member':('uid','sec_uid')},
    'anchor_subject':{'anchor.user_row':('uid','sec_uid')}}
AUTHORITY={'verified':False,'source_permission':'not_assessed','account_binding':'unverified',
    'context_binding':'unverified','business_writes':0,'identity_merges':0}

def _fail(code='IDENTITY_CANDIDATE_INVALID',status=400):
    raise AppError(status,code,'Identity candidate metadata was rejected.')
def _label(value,limit=128):
    if type(value) is not str or not 1<=len(value)<=limit or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]*',value) is None:_fail()
    return value
def _sha(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{64}',value) is None:_fail()
    return value
def _uid(value):
    if type(value) is not str or re.fullmatch(r'[0-9]{1,64}',value) is None:_fail()
    return value
def _sec(value):
    # Opaque typed token syntax is not a proof that a value belongs to anyone.
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,256}',value) is None:_fail()
    return value
def _hash(value):return hashlib.sha256(canonical_json(value)).hexdigest()
def _scope(account_key,provider_namespace,object_kind):
    _label(account_key);_label(provider_namespace)
    if type(object_kind) is not str or object_kind!='person':_fail()
    return account_key,provider_namespace,object_kind
def _origin(account_key,provider_namespace,source_id,source_version):
    for value in (account_key,provider_namespace,source_id,source_version):_label(value)
    return account_key,provider_namespace,source_id,source_version
def _validate(dto):
    if type(dto) is not dict or set(dto)!=FIELDS:_fail()
    result=dict(dto)
    _scope(result['account_key'],result['provider_namespace'],result['object_kind'])
    _origin(result['account_key'],result['provider_namespace'],result['source_id'],result['source_version'])
    _sha(result['snapshot_digest']);_label(result['record_ref'],256)
    role=result['subject_role'];context=result['context_kind'];scope=result['field_scope']
    if type(role) is not str or role not in ROLES or type(context) is not str or type(scope) is not str:_fail()
    contexts={'sender':{'unknown','private','group'},'private_peer':{'private'},
        'group_member':{'group'},'anchor_subject':{'unknown','anchor'}}
    if context not in contexts[role] or scope not in ROLE_SCOPES[role]:_fail()
    uid,sec=result['uid'],result['sec_uid']
    if uid is None and sec is None:_fail()
    expected_uid,expected_sec=ROLE_SCOPES[role][scope]
    if uid is not None:
        _uid(uid)
        if type(result['uid_field']) is not str or result['uid_field']!=expected_uid:_fail()
        # Independently extracted message/subject UID must match scope UID.
        if type(result['subject_uid']) is not str or result['subject_uid']!=uid:_fail()
        _uid(result['subject_uid'])
    elif result['uid_field'] is not None or result['subject_uid'] is not None:_fail()
    if sec is not None:
        _sec(sec)
        if type(result['sec_uid_field']) is not str or result['sec_uid_field']!=expected_sec:_fail()
    elif result['sec_uid_field'] is not None:_fail()
    return result
def _record_key(dto):
    # A source/version cannot reuse one ref with another snapshot, role or body.
    return (dto['account_key'],dto['provider_namespace'],dto['source_id'],dto['source_version'],dto['record_ref'])
def _record_origin(dto):return (dto['account_key'],dto['provider_namespace'],dto['source_id'],dto['source_version'])
def _partition(dto):return dto['account_key'],dto['provider_namespace'],dto['object_kind']
def _edge_key(dto):return (*_partition(dto),dto['uid'],dto['sec_uid'])
def _single_key(dto):
    kind='uid'if dto['uid']is not None else 'sec_uid'
    return (*_partition(dto),dto['subject_role'],kind,dto[kind])

class IdentityCandidateLedger:
    def __init__(self,*,max_records=300000,max_edges=20000,max_batch=2000,max_state_bytes=256*1024**2):
        for value,minimum,maximum in ((max_records,1,MAX_RECORDS),(max_edges,1,MAX_EDGES),
            (max_batch,1,MAX_BATCH),(max_state_bytes,16384,MAX_STATE_BYTES)):
            if type(value) is not int or not minimum<=value<=maximum:_fail()
        self._limits={'max_records':max_records,'max_edges':max_edges,'max_batch':max_batch,'max_state_bytes':max_state_bytes}
        self._lock=RLock();self._records={};self._digests={};self._edges={};self._singles={}
        self._origin_records=defaultdict(set);self._withdrawals=set();self._typed_edges=defaultdict(set)
        self._encoded_bytes=1024;self._revision=0;self._withdrawn_records=0

    def append_batch(self,records):
        if type(records) is not list or not 1<=len(records)<=self._limits['max_batch']:_fail('IDENTITY_CANDIDATE_LIMIT',413)
        # All DTOs and budgets are checked before any mutation.
        staged={};staged_hash={};charged=0;duplicates=0;new_edges=set()
        with self._lock:
            for raw in records:
                dto=_validate(raw);key=_record_key(dto);hashvalue=_hash(dto)
                if key in staged or key in self._records:
                    prior=staged_hash[key]if key in staged else self._digests[key]
                    if prior!=hashvalue:_fail('IDENTITY_RECORD_CONFLICT',409)
                    duplicates+=1;continue
                staged[key]=dto;staged_hash[key]=hashvalue
                charged+=len(canonical_json({'observation':dto,'evidence_digest':hashvalue}))+1
                if dto['uid']is not None and dto['sec_uid']is not None and _edge_key(dto)not in self._edges:new_edges.add(_edge_key(dto))
            if len(self._records)+len(staged)>self._limits['max_records']or len(self._edges)+len(new_edges)>self._limits['max_edges']or self._encoded_bytes+charged>self._limits['max_state_bytes']:_fail('IDENTITY_CANDIDATE_LIMIT',413)
            for key,dto in staged.items():
                self._records[key]=dto;self._digests[key]=staged_hash[key];origin=_record_origin(dto)
                self._origin_records[origin].add(key);active=origin not in self._withdrawals
                if not active:self._withdrawn_records+=1
                if dto['uid']is not None and dto['sec_uid']is not None:
                    edgekey=_edge_key(dto)
                    edge=self._edges.setdefault(edgekey,{'records':set(),'active_count':0,'role_active':Counter(),'role_total':Counter(),'state':'withdrawn'})
                    edge['records'].add(key);edge['role_total'][dto['subject_role']]+=1
                    if active:edge['active_count']+=1;edge['role_active'][dto['subject_role']]+=1
                    p=_partition(dto)
                    self._typed_edges[(*p,'uid',dto['uid'])].add(edgekey)
                    self._typed_edges[(*p,'sec_uid',dto['sec_uid'])].add(edgekey)
                else:
                    single=self._singles.setdefault(_single_key(dto),{'total':0,'active':0})
                    single['total']+=1;single['active']+=int(active)
            self._encoded_bytes+=charged
            if staged:self._revision+=1;self._recompute()
            return {'added':len(staged),'duplicates':duplicates,'summary':self.summary()}

    def _recompute(self):
        # Identity contradictions span all observation roles in a partition.
        adjacency=defaultdict(lambda:defaultdict(set))
        for key,edge in self._edges.items():
            edge['state']='pending'if edge['active_count']else 'withdrawn'
            if edge['active_count']:
                p=key[:3];u=('uid',key[3]);s=('sec_uid',key[4])
                adjacency[p][u].add(s);adjacency[p][s].add(u)
        for p,graph in adjacency.items():
            visited=set()
            for start in graph:
                if start in visited:continue
                todo=[start];component=set();conflict=False
                while todo:
                    node=todo.pop()
                    if node in component:continue
                    component.add(node);visited.add(node)
                    if len(graph[node])>1:conflict=True
                    todo.extend(graph[node]-component)
                if conflict:
                    for kind,uid in component:
                        if kind=='uid':
                            for _,sec in graph[(kind,uid)]:self._edges[(*p,uid,sec)]['state']='conflict'

    def withdraw_source(self,*,account_key,provider_namespace,source_id,source_version):
        origin=_origin(account_key,provider_namespace,source_id,source_version)
        with self._lock:
            if origin in self._withdrawals:return {'duplicate':True,'affected_records':0,'summary':self.summary()}
            charge=len(canonical_json(dict(zip(('account_key','provider_namespace','source_id','source_version'),origin))))+1
            if len(self._withdrawals)>=MAX_WITHDRAWALS or self._encoded_bytes+charge>self._limits['max_state_bytes']:_fail('IDENTITY_CANDIDATE_LIMIT',413)
            self._withdrawals.add(origin);keys=self._origin_records.get(origin,())
            for key in keys:
                dto=self._records[key]
                if dto['uid']is not None and dto['sec_uid']is not None:
                    edge=self._edges[_edge_key(dto)];edge['active_count']-=1;edge['role_active'][dto['subject_role']]-=1
                else:self._singles[_single_key(dto)]['active']-=1
            self._withdrawn_records+=len(keys);self._encoded_bytes+=charge;self._revision+=1;self._recompute()
            return {'duplicate':False,'affected_records':len(keys),'summary':self.summary()}

    def summary(self):
        with self._lock:
            states=Counter({'pending':0,'conflict':0,'withdrawn':0});records=Counter({'pending':0,'conflict':0,'withdrawn':self._withdrawn_records})
            for edge in self._edges.values():
                states[edge['state']]+=1
                if edge['active_count']:records[edge['state']]+=edge['active_count']
            records['pending']+=sum(single['active']for single in self._singles.values())
            return {'format':FORMAT,'revision':self._revision,'records':len(self._records),'edges':len(self._edges),
                'single_key_records':sum(single['total']for single in self._singles.values()),'edge_states':dict(states),
                'record_states':dict(records),'withdrawn_sources':len(self._withdrawals),'metadata_budget_bytes':self._encoded_bytes,**AUTHORITY}

    def lookup(self,*,account_key,provider_namespace,object_kind,subject_role,identity_kind,identity_value):
        p=_scope(account_key,provider_namespace,object_kind)
        if type(subject_role) is not str or subject_role not in ROLES or type(identity_kind)is not str or identity_kind not in ('uid','sec_uid'):_fail()
        (_uid if identity_kind=='uid'else _sec)(identity_value)
        with self._lock:
            keys=self._typed_edges.get((*p,identity_kind,identity_value),())
            if len(keys)>MAX_LOOKUP_EDGES:_fail('IDENTITY_CANDIDATE_LIMIT',413)
            candidates=[];global_conflict=False
            for key in sorted(keys):
                edge=self._edges[key]
                global_conflict|=edge['state']=='conflict'
                if not edge['role_total'][subject_role]:continue
                active=edge['role_active'][subject_role]
                state=edge['state']if active else 'withdrawn'
                candidates.append({'uid':key[3],'sec_uid':key[4],'state':state,'evidence_count':edge['role_total'][subject_role],
                    'active_evidence_count':active,'edge_digest':_hash(list(key))})
            single=self._singles.get((*p,subject_role,identity_kind,identity_value),{'total':0,'active':0})
            if global_conflict:status='conflict'
            elif any(item['state']=='pending'for item in candidates):status='pending'
            elif candidates:status='withdrawn'
            elif single['active']:status='pending'
            elif single['total']:status='withdrawn'
            else:status='not_found'
            return {'status':status,'global_identity_conflict':global_conflict,'subject_role':subject_role,
                'candidates':candidates,'single_key_evidence_count':single['total'],
                'active_single_key_evidence_count':single['active'],**AUTHORITY}

    def export_state(self):
        with self._lock:
            records=[{'observation':dict(self._records[key]),'evidence_digest':self._digests[key]}for key in sorted(self._records)]
            withdrawals=[dict(zip(('account_key','provider_namespace','source_id','source_version'),origin))for origin in sorted(self._withdrawals)]
            payload={'format':FORMAT,'version':1,'revision':self._revision,'limits':dict(self._limits),'records':records,'withdrawals':withdrawals}
            encoded=canonical_json(payload)
            if len(encoded)+100>self._limits['max_state_bytes']:_fail('IDENTITY_CANDIDATE_LIMIT',413)
            return {**payload,'state_digest':hashlib.sha256(encoded).hexdigest()}

    @classmethod
    def from_state(cls,state):
        try:return cls._restore(state)
        except AppError as exc:
            if exc.code=='IDENTITY_CANDIDATE_LIMIT':raise
            _fail('IDENTITY_STATE_INVALID')
        except (TypeError,ValueError,OverflowError):_fail('IDENTITY_STATE_INVALID')

    @classmethod
    def _restore(cls,state):
        if type(state)is not dict or set(state)!={'format','version','revision','limits','records','withdrawals','state_digest'}:_fail('IDENTITY_STATE_INVALID')
        if state['format']!=FORMAT or type(state['version'])is not int or state['version']!=1 or type(state['revision'])is not int or not 0<=state['revision']<=2**63-1:_fail('IDENTITY_STATE_INVALID')
        limits=state['limits']
        if type(limits)is not dict or set(limits)!={'max_records','max_edges','max_batch','max_state_bytes'}:_fail('IDENTITY_STATE_INVALID')
        ledger=cls(**limits)
        records,withdrawals=state['records'],state['withdrawals']
        if type(records)is not list or type(withdrawals)is not list or len(records)>limits['max_records']or len(withdrawals)>MAX_WITHDRAWALS:_fail('IDENTITY_CANDIDATE_LIMIT',413)
        if not len(withdrawals)+int(bool(records))<=state['revision']<=len(withdrawals)+len(records):_fail('IDENTITY_STATE_INVALID')
        # Check each bounded entry before whole-state encoding; never trust edge states.
        record_keys=set();validated=[];charged=1024
        for entry in records:
            if type(entry)is not dict or set(entry)!={'observation','evidence_digest'}:_fail('IDENTITY_STATE_INVALID')
            dto=_validate(entry['observation']);key=_record_key(dto);_sha(entry['evidence_digest'])
            if key in record_keys or _hash(dto)!=entry['evidence_digest']:_fail('IDENTITY_STATE_INVALID')
            record_keys.add(key);validated.append(dto);charged+=len(canonical_json(entry))+1
            if charged>limits['max_state_bytes']:_fail('IDENTITY_CANDIDATE_LIMIT',413)
        withdrawal_keys=set()
        for entry in withdrawals:
            if type(entry)is not dict or set(entry)!={'account_key','provider_namespace','source_id','source_version'}:_fail('IDENTITY_STATE_INVALID')
            origin=_origin(**entry)
            if origin in withdrawal_keys:_fail('IDENTITY_STATE_INVALID')
            withdrawal_keys.add(origin);charged+=len(canonical_json(entry))+1
            if charged>limits['max_state_bytes']:_fail('IDENTITY_CANDIDATE_LIMIT',413)
        payload={key:state[key]for key in state if key!='state_digest'};_sha(state['state_digest'])
        if _hash(payload)!=state['state_digest']or len(canonical_json(state))>limits['max_state_bytes']:_fail('IDENTITY_STATE_INVALID')
        # Tombstones are loaded before any old observations can become active.
        for entry in withdrawals:ledger.withdraw_source(**entry)
        for start in range(0,len(validated),limits['max_batch']):ledger.append_batch(validated[start:start+limits['max_batch']])
        ledger._revision=state['revision']
        # Enforce canonical ordering and exact finite state representation.
        if ledger.export_state()!=state:_fail('IDENTITY_STATE_INVALID')
        return ledger
