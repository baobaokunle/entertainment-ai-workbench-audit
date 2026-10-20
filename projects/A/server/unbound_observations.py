"""Bounded unbound UI observations, deliberately separate from identity edges.

Display names and metrics are variable hints, not identities or platform facts.
Construction and in-memory use have no I/O; explicit files are local plaintext.
"""
from contextlib import ExitStack
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
from threading import RLock
import uuid

from .common import AppError,canonical_json

FORMAT='unbound-observation-ledger-1'
FILE_FORMAT='dfa-unbound-observations-json-1'
MAX_ROWS=100000
MAX_BATCH=1000
MAX_FILE_BYTES=8*1024**2
MAX_LINE_BYTES=16384
METRIC_KINDS=frozenset(('contribution','watch_duration','interaction_count','gift_count','rank','fan_club_level','membership_duration'))
FIELDS=frozenset(('observation_id','account_key','provider_namespace','tab_id','document_generation','panel_generation',
    'source_id','source_version','source_kind','observed_at','display_name_hint','metrics','period_state','binding_state'))
FLAGS={'binding_state':'unbound','source_permission':'not_assessed','account_binding':'unverified',
    'time_authority':'observation_claim_only','period_state':'unknown','contact_created':False,'facts_written':0,'business_writes':0}

def _fail(code='UNBOUND_OBSERVATION_INVALID',status=400):
    raise AppError(status,code,'Unbound observation metadata was rejected.')
def _label(value,maximum=128):
    if type(value)is not str or not 1<=len(value)<=maximum or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]*',value)is None:_fail()
    return value
def _observation_id(value):
    if type(value)is not str or len(value)!=36:_fail()
    try:parsed=uuid.UUID(value)
    except ValueError:_fail()
    if parsed.version!=4 or parsed.variant!=uuid.RFC_4122 or str(parsed)!=value:_fail()
    return value
def _sha(value):
    if type(value)is not str or re.fullmatch(r'[0-9a-f]{64}',value)is None:_fail()
    return value
def _hash(value):return hashlib.sha256(canonical_json(value)).hexdigest()
def _utf8(value):
    try:return len(value.encode('utf-8'))
    except UnicodeError:_fail()
def _validate(raw):
    if type(raw)is not dict or set(raw)!=FIELDS:_fail()
    dto=dict(raw);_observation_id(dto['observation_id'])
    for key in ('account_key','provider_namespace','source_id','source_version'):_label(dto[key])
    if type(dto['tab_id'])is not int or not 1<=dto['tab_id']<=2**31-1:_fail()
    for field in ('document_generation','panel_generation'):
        if type(dto[field])is not int or not 0<=dto[field]<=2**53-1:_fail()
    if type(dto['source_kind'])is not str or dto['source_kind']not in ('anchor_visible_row','anchor_visible_panel'):_fail()
    if type(dto['binding_state'])is not str or dto['binding_state']!='unbound' or type(dto['period_state'])is not str or dto['period_state']!='unknown':_fail()
    stamp=dto['observed_at']
    if type(stamp)is not str or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3})?Z',stamp)is None:_fail()
    try:datetime.fromisoformat(stamp.replace('Z','+00:00'))
    except ValueError:_fail()
    hint=dto['display_name_hint']
    if hint is not None and(type(hint)is not str or not 1<=len(hint)<=128 or _utf8(hint)>512 or any(ord(c)<32 or ord(c)==127 for c in hint)):_fail()
    metrics=dto['metrics']
    if type(metrics)is not list or not 0<=len(metrics)<=len(METRIC_KINDS):_fail()
    seen=set();clean=[]
    for metric in metrics:
        if type(metric)is not dict or set(metric)!={'metric_kind','value_raw'}:_fail()
        kind=metric['metric_kind'];value=metric['value_raw']
        if type(kind)is not str or kind not in METRIC_KINDS or kind in seen:_fail()
        if type(value)is not str or not 1<=len(value)<=128 or _utf8(value)>512:_fail()
        # Finite visible numeric/unit strings and explicit unavailable labels.
        # Preserve spacing/punctuation/units; never normalize into a fact.
        if value not in ('未知','暂无','未显示','未加载','未开启','--','—')and re.fullmatch(r'[0-9０-９.,，+\-−<>≤≥%％KkMmWw万亿千百秒分时小时分钟天周月年级金币音浪元钻石点位名人次个 \u3000]+',value)is None:_fail()
        seen.add(kind);clean.append(dict(metric))
    dto['metrics']=clean
    return dto

class UnboundObservationLedger:
    def __init__(self,*,max_rows=10000,max_batch=1000,max_state_bytes=MAX_FILE_BYTES):
        for value,minimum,maximum in ((max_rows,1,MAX_ROWS),(max_batch,1,MAX_BATCH),(max_state_bytes,16384,MAX_FILE_BYTES)):
            if type(value)is not int or not minimum<=value<=maximum:_fail()
        self._limits={'max_rows':max_rows,'max_batch':max_batch,'max_state_bytes':max_state_bytes}
        self._records={};self._digests={};self._revision=0;self._bytes=1024;self._lock=RLock()
    def append_batch(self,observations):
        if type(observations)is not list or not 1<=len(observations)<=self._limits['max_batch']:_fail('UNBOUND_OBSERVATION_LIMIT',413)
        with self._lock:
            staged={};hashes={};charged=0;duplicates=0
            for raw in observations:
                dto=_validate(raw);key=dto['observation_id'];digest=_hash(dto)
                if key in staged or key in self._records:
                    old=hashes[key]if key in staged else self._digests[key]
                    if old!=digest:_fail('UNBOUND_OBSERVATION_CONFLICT',409)
                    duplicates+=1;continue
                staged[key]=dto;hashes[key]=digest;charged+=len(canonical_json({'observation':dto,'evidence_digest':digest}))+1
            if len(self._records)+len(staged)>self._limits['max_rows']or self._bytes+charged>self._limits['max_state_bytes']:_fail('UNBOUND_OBSERVATION_LIMIT',413)
            self._records.update(staged);self._digests.update(hashes);self._bytes+=charged
            if staged:self._revision+=1
            return {'added':len(staged),'duplicates':duplicates,'summary':self.summary()}
    def get(self,observation_id,*,account_key,provider_namespace):
        _label(account_key);_label(provider_namespace);_observation_id(observation_id)
        with self._lock:
            dto=self._records.get(observation_id)
            if dto is None or dto['account_key']!=account_key or dto['provider_namespace']!=provider_namespace:return None
            return {**dto,'metrics':[dict(metric)for metric in dto['metrics']]}
    def summary(self):
        with self._lock:return {'format':FORMAT,'rows':len(self._records),'revision':self._revision,'metadata_budget_bytes':self._bytes,**FLAGS}
    def export_state(self):
        with self._lock:
            records=[{'observation':{**self._records[key],'metrics':[dict(m)for m in self._records[key]['metrics']]},'evidence_digest':self._digests[key]}for key in sorted(self._records)]
            payload={'format':FORMAT,'version':1,'limits':dict(self._limits),'revision':self._revision,'records':records}
            if len(canonical_json(payload))+100>self._limits['max_state_bytes']:_fail('UNBOUND_OBSERVATION_LIMIT',413)
            return {**payload,'state_digest':_hash(payload)}
    @classmethod
    def from_state(cls,state):
        try:return cls._restore(state)
        except AppError as exc:
            if exc.code=='UNBOUND_OBSERVATION_LIMIT':raise
            _fail('UNBOUND_STATE_INVALID')
        except (TypeError,ValueError,OverflowError):_fail('UNBOUND_STATE_INVALID')
    @classmethod
    def _restore(cls,state):
        if type(state)is not dict or set(state)!={'format','version','limits','revision','records','state_digest'}:_fail('UNBOUND_STATE_INVALID')
        if type(state['format'])is not str or state['format']!=FORMAT or type(state['version'])is not int or state['version']!=1:_fail('UNBOUND_STATE_INVALID')
        limits=state['limits']
        if type(limits)is not dict or set(limits)!={'max_rows','max_batch','max_state_bytes'}:_fail('UNBOUND_STATE_INVALID')
        ledger=cls(**limits);records=state['records']
        if type(records)is not list or len(records)>limits['max_rows']:_fail('UNBOUND_OBSERVATION_LIMIT',413)
        if type(state['revision'])is not int or not int(bool(records))<=state['revision']<=len(records):_fail('UNBOUND_STATE_INVALID')
        seen=set();validated=[];charged=1024
        for entry in records:
            if type(entry)is not dict or set(entry)!={'observation','evidence_digest'}:_fail('UNBOUND_STATE_INVALID')
            dto=_validate(entry['observation']);key=dto['observation_id'];_sha(entry['evidence_digest'])
            if key in seen or _hash(dto)!=entry['evidence_digest']:_fail('UNBOUND_STATE_INVALID')
            seen.add(key);validated.append(dto);charged+=len(canonical_json(entry))+1
            if charged>limits['max_state_bytes']:_fail('UNBOUND_OBSERVATION_LIMIT',413)
        payload={key:state[key]for key in state if key!='state_digest'};_sha(state['state_digest'])
        if _hash(payload)!=state['state_digest']or len(canonical_json(state))>limits['max_state_bytes']:_fail('UNBOUND_STATE_INVALID')
        for offset in range(0,len(validated),limits['max_batch']):ledger.append_batch(validated[offset:offset+limits['max_batch']])
        ledger._revision=state['revision']
        if ledger.export_state()!=state:_fail('UNBOUND_STATE_INVALID')
        return ledger

def _file_path(value,*,new=False):
    from .backup_scratch import reject_links
    path=Path(value)
    if not path.is_absolute()or '..'in path.parts:_fail('UNBOUND_FILE_PATH_INVALID')
    reject_links(path)
    if new:
        if not path.parent.is_dir()or not path.name.endswith('.unbound.json'):_fail('UNBOUND_FILE_PATH_INVALID')
        if path.exists():_fail('UNBOUND_FILE_EXISTS',409)
    elif not path.is_file()or path.stat().st_nlink!=1:_fail('UNBOUND_FILE_PATH_INVALID')
    return path
def _read(path,expected_sha256):
    from .identity_candidate_files import pinned_bytes
    _sha(expected_sha256);path=_file_path(path)
    try:return pinned_bytes(path,expected_sha256,maximum=MAX_FILE_BYTES)
    except AppError:_fail('UNBOUND_FILE_READ_REJECTED',422)
def load_file(path,expected_sha256):
    from .identity_candidate_files import strict_json
    raw=_read(path,expected_sha256)
    try:state=strict_json(raw)
    except AppError:_fail('UNBOUND_FILE_JSON_INVALID',422)
    if type(state)is not dict or set(state)!={'format','ledger'}or state['format']!=FILE_FORMAT:_fail('UNBOUND_FILE_FORMAT_INVALID',422)
    return UnboundObservationLedger.from_state(state['ledger'])
def append_jsonl_copy(ledger,path,expected_sha256):
    from .identity_candidate_files import strict_json
    raw=_read(path,expected_sha256);state=ledger.export_state();private=UnboundObservationLedger.from_state(state)
    batch=[]
    for line in raw.splitlines():
        if not line.strip():continue
        if len(line)>MAX_LINE_BYTES:_fail('UNBOUND_FILE_LINE_LIMIT',413)
        try:batch.append(strict_json(line))
        except AppError:_fail('UNBOUND_FILE_JSON_INVALID',422)
        if len(batch)==state['limits']['max_batch']:private.append_batch(batch);batch=[]
    if batch:private.append_batch(batch)
    return private
def save_new_file(ledger,path):
    from .backup_scratch import reject_links
    from .export_candidates import _Lease
    if os.name!='nt':_fail('UNBOUND_FILE_WINDOWS_REQUIRED',503)
    path=_file_path(path,new=True);blob=canonical_json({'format':FILE_FORMAT,'ledger':ledger.export_state()})
    if len(blob)>MAX_FILE_BYTES:_fail('UNBOUND_FILE_LIMIT',413)
    stage=path.parent/('.unbound-json-'+uuid.uuid4().hex+'.tmp');owned=None;published=False
    try:
        with ExitStack()as stack:
            stack.enter_context(_Lease(path.parent,directory=True));_file_path(path,new=True)
            with stage.open('xb')as stream:
                info=os.fstat(stream.fileno())
                if info.st_nlink!=1:_fail('UNBOUND_FILE_PATH_INVALID')
                owned=(info.st_dev,info.st_ino);stream.write(blob);stream.flush();os.fsync(stream.fileno())
            reject_links(stage);info=stage.stat()
            if (info.st_dev,info.st_ino)!=owned or info.st_nlink!=1:_fail('UNBOUND_FILE_CHANGED',409)
            expected=hashlib.sha256(blob).hexdigest()
            if _read(stage,expected)!=blob:_fail('UNBOUND_FILE_CHANGED',409)
            _file_path(path,new=True);os.rename(stage,path);published=True
            if _read(path,expected)!=blob:_fail('UNBOUND_FILE_CHANGED',409)
    except Exception as exc:
        if published:
            error=AppError(503,'UNBOUND_FILE_PUBLISHED_UNVERIFIED','Observation file publication needs review; no binding was performed.')
            error.publication_state='published_unverified';error.durable_state_unknown=True;raise error from None
        if isinstance(exc,AppError):raise
        _fail('UNBOUND_FILE_UNAVAILABLE',503)
    finally:
        if owned is not None and stage.exists():
            reject_links(stage);info=stage.stat()
            if (info.st_dev,info.st_ino)==owned and info.st_nlink==1:stage.unlink()
    return {'format':FILE_FORMAT,'file_bytes':len(blob),'file_sha256':hashlib.sha256(blob).hexdigest(),
        'encrypted':False,'publication_state':'published_verified','latest_head_verified':False,'summary':ledger.summary(),**FLAGS}
