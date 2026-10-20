"""Isolated, fixture-only origin retirement statements; no transport effects.

The verifier receives its registry, request, original event commitment and log
checkpoint from a trusted caller. A statement cannot supply those trust anchors.
It does not attest peer erasure, physical purge, or checkpoint freshness.
"""
import base64
from dataclasses import dataclass
import hashlib
import hmac
import threading

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .auth import Principal
from .common import AppError, canonical_json
from .db import digest, stable_key
from .deletion_intents import validate_intent
from .local_deletion import LogicalDeletionCoordinator, require_ready
from .sync.events import from_row, parse_json, validate_event


FORMAT='dfa-source-retirement-proof-2'
DOMAIN=b'DFA-SOURCE-RETIREMENT-PROOF-V2\x00'
MAX_PROOF_BYTES=8192
CHANNELS=frozenset({'messages','actions','profile','collection','config','model','activities'})
EVENT_FIELDS=frozenset({'origin_device_key','account_key','epoch','channel','seq',
                        'event_id','event_digest','payload_hash','parent_digest'})
PROOF_FIELDS=EVENT_FIELDS | frozenset({'format','protocol_version','proof_kind','authority_id',
    'authority_version','signing_key_sha256','intent_id','request_digest','log_head'})


def _fixture(value):
    if value is not True:
        raise AppError(403,'RETIREMENT_FIXTURE_ONLY','Origin proof work remains explicitly fictional; product integration is disabled.')


def _text(value,limit=200):
    if not isinstance(value,str) or not value or '\x00' in value:
        raise AppError(422,'RETIREMENT_FORMAT_INVALID','Finite identifiers are required.')
    if len(value)>limit:
        raise AppError(413,'RETIREMENT_LIMIT','Identifier exceeds its finite budget.')


def _hash(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
        raise AppError(422,'RETIREMENT_FORMAT_INVALID','Complete lowercase SHA256 commitments are required.')


def _principal(principal,scope,account,local_device):
    if not isinstance(principal,Principal) or principal.role!='manager' or principal.device_key!=local_device:
        raise AppError(403,'RETIREMENT_AUTHORITY_REQUIRED','An independently authorized local Principal is required.')
    principal.require(scope,account)


@dataclass(frozen=True)
class _Authority:
    authority_id: str
    authority_version: str
    origin_device_key: str
    account_key: str
    public_key: bytes


class RetirementAuthorityRegistry:
    """Explicit in-memory fixture registry, independent of transport pairing.

    Registration trusts a server-issued Principal carrying a separate scope.
    No role inside a proof, paired peer, or default application role registers it.
    Persistence, key rotation/revocation and real authority enrollment are pending.
    """
    MAX_AUTHORITIES=256

    def __init__(self,*,fixture_mode=False):
        _fixture(fixture_mode)
        self._entries={}
        self.lock=threading.RLock()

    def register(self,authority_id,authority_version,origin_device_key,account_key,public_key,
                 principal,*,local_device_key):
        for value in (authority_id,origin_device_key,account_key,local_device_key):_text(value)
        _text(authority_version,80)
        _principal(principal,'deletion:authority:register',account_key,local_device_key)
        if not isinstance(public_key,bytes) or len(public_key)!=32:
            raise AppError(422,'RETIREMENT_KEY_INVALID','Explicit raw Ed25519 public key required.')
        Ed25519PublicKey.from_public_bytes(public_key)
        entry=_Authority(authority_id,authority_version,origin_device_key,account_key,public_key)
        key=(authority_id,authority_version,origin_device_key,account_key)
        with self.lock:
            old=self._entries.get(key)
            if old and old!=entry:
                raise AppError(409,'RETIREMENT_AUTHORITY_CONFLICT','Registered identity cannot silently replace its signing key.')
            if not old and len(self._entries)>=self.MAX_AUTHORITIES:
                raise AppError(413,'RETIREMENT_LIMIT','Authority registry exceeds its finite budget.')
            self._entries[key]=entry
        return {'authority_id':authority_id,'authority_version':authority_version,
                'origin_device_key':origin_device_key,'account_key':account_key,
                'signing_key_sha256':hashlib.sha256(public_key).hexdigest()}

    def _get(self,authority_id,authority_version,origin_device_key,account_key):
        with self.lock:entry=self._entries.get((authority_id,authority_version,origin_device_key,account_key))
        if entry is None:
            raise AppError(403,'RETIREMENT_AUTHORITY_UNREGISTERED','No exact independently trusted origin/account/version signing authority.')
        return entry


def _validate_event_commitment(commitment,*,bind_identity=True):
    if not isinstance(commitment,dict) or set(commitment)!=EVENT_FIELDS:
        raise AppError(422,'RETIREMENT_FORMAT_INVALID','A complete body-free original event commitment is required.')
    for name in ('origin_device_key','account_key','epoch','channel','event_id'):_text(commitment[name])
    for name in ('event_digest','payload_hash','parent_digest'):_hash(commitment[name])
    if commitment['channel'] not in CHANNELS or type(commitment['seq']) is not int or not 1<=commitment['seq']<=2**63-1:
        raise AppError(422,'RETIREMENT_FORMAT_INVALID','Original finite domain stream and sequence required.')
    if bind_identity and commitment['event_id']!=stable_key('event',commitment['origin_device_key'],commitment['epoch'],commitment['channel'],commitment['seq']):
        raise AppError(409,'RETIREMENT_EVENT_IDENTITY','Original event ID must bind source device, epoch, channel and sequence.')
    return commitment


def event_commitment(event):
    """Project full validated semantics. Caller must authenticate their provenance.

    validate_event alone is not a source signature; issue also checks local origin
    storage, and verification trusts this commitment only when supplied out of band.
    """
    validate_event(event)
    result={name:event[name] for name in ('account_key','epoch','channel','seq','event_id','event_digest','payload_hash')}
    result.update(origin_device_key=event['device_key'],parent_digest=digest(event['parents']))
    return _validate_event_commitment(result)


def _validate_proof(proof):
    if not isinstance(proof,dict) or set(proof)!=PROOF_FIELDS:
        raise AppError(422,'RETIREMENT_FORMAT_INVALID','Only the fixed body-free retirement proof fields are accepted.')
    if proof['format']!=FORMAT or type(proof['protocol_version']) is not int or proof['protocol_version']!=2 or proof['proof_kind']!='source_origin_retirement':
        raise AppError(422,'RETIREMENT_FORMAT_INVALID','This distinct protocol2 statement is not a schema2 event or transport ACK.')
    for name in ('authority_id','intent_id'):_text(proof[name])
    _text(proof['authority_version'],80)
    for name in ('signing_key_sha256','request_digest','log_head'):_hash(proof[name])
    _validate_event_commitment({name:proof[name] for name in EVENT_FIELDS},bind_identity=False)
    if len(canonical_json(proof))>MAX_PROOF_BYTES:
        raise AppError(413,'RETIREMENT_LIMIT','Complete proof exceeds its finite byte budget.')
    return proof


def _bind_request(proof,request,expected_request_digest,expected_head):
    _hash(expected_request_digest);_hash(expected_head)
    validate_intent(request)
    request=parse_json(canonical_json(request))
    if (not hmac.compare_digest(digest(request),expected_request_digest)
        or not hmac.compare_digest(proof['request_digest'],expected_request_digest)
        or not hmac.compare_digest(proof['log_head'],expected_head)
        or proof['intent_id']!=request['intent_id']
        or proof['account_key']!=request['account_key']
        or proof['origin_device_key']!=request['device_key']):
        raise AppError(409,'RETIREMENT_REQUEST_MISMATCH','Independent request identity, digest and checkpoint must match exactly.')
    if {'event_id':proof['event_id'],'event_digest':proof['event_digest']} not in request['event_refs']:
        raise AppError(403,'RETIREMENT_EVENT_NOT_REQUESTED','The exact original event is outside this independent request.')


class RetirementProofService:
    def __init__(self,db,signing_key,registry,*,fixture_mode=False):
        _fixture(fixture_mode)
        if not isinstance(registry,RetirementAuthorityRegistry) or not isinstance(signing_key,Ed25519PrivateKey):
            raise AppError(403,'RETIREMENT_AUTHORITY_REQUIRED','Explicit registry and original Ed25519 source signing key required.')
        self.db,self.signing_key,self.registry=db,signing_key,registry

    def issue(self,event,principal,*,authority_id,authority_version,intent_id,expected_request_digest,expected_head):
        commitment=event_commitment(event)
        for value in (authority_id,intent_id):_text(value)
        _text(authority_version,80);_hash(expected_request_digest);_hash(expected_head)
        with self.db.lock,self.registry.lock:
            require_ready(self.db)
            gate=getattr(self.db,'_deletion_gate',None)
            if not isinstance(gate,LogicalDeletionCoordinator) or gate.db is not self.db:
                raise AppError(503,'LOCAL_DELETION_HOLD','A ready authenticated local coordinator is required to issue a statement.')
            meta=dict(self.db.conn.execute('SELECT key,value FROM app_meta'))
            origin=meta.get('device_key')
            _principal(principal,'deletion:retirement:issue',event['account_key'],origin)
            if event['device_key']!=origin:
                raise AppError(403,'RETIREMENT_FOREIGN_ORIGIN','A device cannot resign or retire another origin event.')
            authority=self.registry._get(authority_id,authority_version,origin,event['account_key'])
            public=self.signing_key.public_key().public_bytes_raw()
            if not hmac.compare_digest(public,authority.public_key):
                raise AppError(403,'RETIREMENT_SIGNING_KEY_MISMATCH','Source signing key must match independent registered authority.')
            row=self.db.conn.execute('SELECT * FROM domain_event WHERE event_id=?',(event['event_id'],)).fetchone()
            if row is None or canonical_json(from_row(row))!=canonical_json(event):
                raise AppError(409,'RETIREMENT_ORIGINAL_NOT_STORED','Complete original local event semantics must match immutable storage.')
            if not hmac.compare_digest(gate.expected_head,expected_head):
                raise AppError(409,'RETIREMENT_REQUEST_MISMATCH','Caller must supply the independently retained current checkpoint.')
            with gate.log.lock:
                records=gate.log.read(expected_head=expected_head)
                requests=[record['intent'] for record in records if record['intent']['intent_id']==intent_id]
                if len(requests)!=1:
                    raise AppError(403,'RETIREMENT_REQUEST_MISSING','One authenticated independent intent is required.')
                request=requests[0]
                proof=dict(commitment,format=FORMAT,protocol_version=2,proof_kind='source_origin_retirement',
                    authority_id=authority_id,authority_version=authority_version,
                    signing_key_sha256=hashlib.sha256(public).hexdigest(),intent_id=intent_id,
                    request_digest=expected_request_digest,log_head=expected_head)
                _validate_proof(proof);_bind_request(proof,request,expected_request_digest,expected_head)
                persisted=self.db.conn.execute('SELECT intent_json FROM local_deletion_request WHERE intent_id=?',(intent_id,)).fetchone()
                ref=self.db.conn.execute('SELECT event_digest,account_key FROM local_deletion_event WHERE event_id=?',(event['event_id'],)).fetchone()
                if (not persisted or persisted[0]!=canonical_json(request).decode() or not ref
                    or tuple(ref)!=(event['event_digest'],event['account_key'])):
                    raise AppError(403,'RETIREMENT_EVENT_NOT_SUPPRESSED','Ready local projection must contain this exact authenticated event reference.')
                signature=self.signing_key.sign(DOMAIN+canonical_json(proof))
                # Detect independent log changes before returning; this still
                # cannot prove checkpoint freshness after the operation ends.
                require_ready(self.db)
                gate.log.read(expected_head=expected_head)
                return {'proof':proof,'signature':base64.b64encode(signature).decode('ascii')}


def verify_retirement_proof(envelope,registry,*,expected_request,expected_request_digest,
                            expected_head,expected_event,fixture_mode=False):
    _fixture(fixture_mode)
    if isinstance(envelope,bytes):
        if len(envelope)>MAX_PROOF_BYTES:
            raise AppError(413,'RETIREMENT_LIMIT','Proof envelope exceeds its finite byte budget.')
        envelope=parse_json(envelope)
    if not isinstance(envelope,dict) or set(envelope)!={'proof','signature'}:
        raise AppError(422,'RETIREMENT_FORMAT_INVALID','Fixed proof and Ed25519 signature envelope required.')
    raw_proof=envelope['proof']
    if not isinstance(raw_proof,dict):
        raise AppError(422,'RETIREMENT_FORMAT_INVALID','Fixed proof metadata is required.')
    proof=_validate_proof(dict(raw_proof))
    if not isinstance(registry,RetirementAuthorityRegistry):
        raise AppError(403,'RETIREMENT_AUTHORITY_REQUIRED','A trusted registry cannot be supplied by signed role claims.')
    with registry.lock:
        authority=registry._get(proof['authority_id'],proof['authority_version'],proof['origin_device_key'],proof['account_key'])
        try:
            value=envelope['signature']
            if not isinstance(value,str) or len(value)!=88:raise ValueError()
            signature=base64.b64decode(value,validate=True)
            if len(signature)!=64 or base64.b64encode(signature).decode('ascii')!=value:raise ValueError()
            Ed25519PublicKey.from_public_bytes(authority.public_key).verify(signature,DOMAIN+canonical_json(proof))
        except (ValueError,InvalidSignature,TypeError) as exc:
            raise AppError(422,'RETIREMENT_SIGNATURE_INVALID','Complete source-origin signature verification failed.') from exc
        if proof['signing_key_sha256']!=hashlib.sha256(authority.public_key).hexdigest():
            raise AppError(403,'RETIREMENT_SIGNING_KEY_MISMATCH','Proof must bind the exact registered source key.')
        _bind_request(proof,expected_request,expected_request_digest,expected_head)
        _validate_event_commitment({name:proof[name] for name in EVENT_FIELDS})
        if not isinstance(expected_event,dict):
            raise AppError(422,'RETIREMENT_FORMAT_INVALID','Trusted original event metadata required.')
        expected_event=_validate_event_commitment(dict(expected_event))
        if any(proof[name]!=expected_event[name] for name in EVENT_FIELDS):
            raise AppError(409,'RETIREMENT_EVENT_BINDING','Original event semantics and stream commitment differ.')
        return dict(proof,verified=True,statement='authorized_origin_retirement_only',
                    peer_deleted=False,physical_purged=False,latest_head_proven=False,
                    copies_state='copies_pending',product_entry_enabled=False)
