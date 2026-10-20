"""Signed authenticated encrypted single-file packages with finite limits."""
import base64,hashlib,secrets,re
from ..common import AppError,canonical_json
from ..security import SecureEnvelope
from .events import parse_json,validate_event

MAX_BYTES=8*1024*1024
MAX_RECORDS=10000
HEADER={'protocol_version','package_id','key_id','sender','recipient','channel','kind','ciphertext_hash'}

def generate_signing_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.generate()

class PackageCodec:
    def __init__(self,pairing,device_key,signing_key):
        self.pairing=pairing;self.device_key=device_key;self.signing_key=signing_key

    def seal(self,recipient,channel,records,*,key_id,kind='DATA',package_id=None):
        if (kind=='CONTROL_ACK') != (channel=='control'):
            raise AppError(403,'PACKAGE_CHANNEL_INVALID','Only control acknowledgements may use the control channel.')
        self.pairing.authorize(recipient,channel=channel)
        if kind not in ('DATA','CONTROL_ACK') or not isinstance(records,list) or len(records)>MAX_RECORDS or not records:
            raise AppError(413,'PACKAGE_RECORD_LIMIT','Package records are invalid or exceed the limit.')
        if kind=='DATA':
            for event in records:
                validate_event(event)
                if event['device_key']!=self.device_key or event['channel']!=channel:raise AppError(403,'EVENT_ROUTING_INVALID','Only own events in this channel may be published.')
                self.pairing.authorize(recipient,account=event['account_key'],channel=channel,event_type=event['event_type'])
        header={'protocol_version':1,'package_id':package_id or secrets.token_hex(16),'key_id':key_id,'sender':self.device_key,'recipient':recipient,'channel':channel,'kind':kind}
        if not isinstance(header['package_id'],str) or not re.fullmatch('[0-9a-f]{32}',header['package_id']):raise AppError(422,'PACKAGE_ID_INVALID','Opaque random package identifier required.')
        plain=canonical_json({'record_count':len(records),'records':records})
        if len(plain)>MAX_BYTES-2048:raise AppError(413,'PACKAGE_SIZE_LIMIT','Package must be split before publishing.')
        ciphertext=SecureEnvelope.seal(plain,self.pairing.key(key_id,publish=True,channel=channel),aad=canonical_json(header))
        header['ciphertext_hash']=hashlib.sha256(ciphertext).hexdigest()
        signature=self.signing_key.sign(canonical_json(header)+ciphertext)
        wire=canonical_json({'header':header,'ciphertext':base64.b64encode(ciphertext).decode(),'signature':base64.b64encode(signature).decode()})
        if len(wire)>MAX_BYTES:raise AppError(413,'PACKAGE_SIZE_LIMIT','Package must be split before publishing.')
        return wire

    def open(self,wire):
        return self._open(wire,local_control=False)

    def open_own_control(self,wire):
        """Authenticate a durable ACK we signed, without impersonating its peer."""
        return self._open(wire,local_control=True)

    def _open(self,wire,*,local_control):
        if not isinstance(wire,bytes) or len(wire)>MAX_BYTES:raise AppError(413,'PACKAGE_SIZE_LIMIT','Package exceeds the finite byte limit.')
        outer=parse_json(wire)
        if not isinstance(outer,dict) or set(outer)!={'header','ciphertext','signature'}:raise AppError(422,'INVALID_PACKAGE','Invalid outer package.')
        header=outer['header']
        if not isinstance(header,dict) or set(header)!=HEADER or type(header['protocol_version']) is not int or header['protocol_version']!=1 or header['kind'] not in ('DATA','CONTROL_ACK'):
            raise AppError(422,'INVALID_PACKAGE','Unsupported package header.')
        for name in HEADER-{'protocol_version'}:
            if not isinstance(header[name],str) or not header[name] or len(header[name])>200:raise AppError(422,'INVALID_PACKAGE','Invalid bounded package header.')
        if not re.fullmatch('[0-9a-f]{32}',header['package_id']):raise AppError(422,'PACKAGE_ID_INVALID','Opaque package identifier required.')
        if (header['kind']=='CONTROL_ACK') != (header['channel']=='control'):
            raise AppError(403,'PACKAGE_CHANNEL_INVALID','Only control acknowledgements may use the control channel.')
        if local_control:
            if header['sender']!=self.device_key or header['kind']!='CONTROL_ACK':
                raise AppError(403,'PACKAGE_WRONG_ROUTE','Only our own signed control publication may be inspected locally.')
            self.pairing.authorize(header['recipient'],channel=header['channel'])
            from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
            public=self.signing_key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        else:
            if header['recipient']!=self.device_key:raise AppError(403,'PACKAGE_WRONG_ROUTE','Package recipient does not match this device.')
            public=self.pairing.authorize(header['sender'],channel=header['channel'])
        try:
            ciphertext=base64.b64decode(outer['ciphertext'],validate=True);signature=base64.b64decode(outer['signature'],validate=True)
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(public).verify(signature,canonical_json(header)+ciphertext)
        except Exception as exc:raise AppError(422,'SIGNATURE_INVALID','Package device signature is invalid.') from exc
        if hashlib.sha256(ciphertext).hexdigest()!=header['ciphertext_hash']:raise AppError(422,'PACKAGE_HASH_INVALID','Ciphertext digest mismatch.')
        aad={key:value for key,value in header.items() if key!='ciphertext_hash'}
        plain=SecureEnvelope.open(ciphertext,self.pairing.key(header['key_id'],publish=local_control,channel=header['channel']),aad=canonical_json(aad))
        inner=parse_json(plain)
        if not isinstance(inner,dict) or set(inner)!={'record_count','records'} or type(inner['record_count']) is not int or not isinstance(inner['records'],list) or inner['record_count']!=len(inner['records']) or not 1<=inner['record_count']<=MAX_RECORDS:
            raise AppError(422,'PACKAGE_COUNT_INVALID','Manifest count or finite record limit invalid.')
        if header['kind']=='DATA':
            for event in inner['records']:
                validate_event(event)
                if event['device_key']!=header['sender'] or event['channel']!=header['channel']:raise AppError(403,'EVENT_ROUTING_INVALID','Event source or channel mismatch.')
                self.pairing.authorize(header['sender'],account=event['account_key'],channel=event['channel'],event_type=event['event_type'])
                if event['actor'] is not None:self.pairing.authorize(header['sender'],operator_key=event['actor']['operator_key'])
        return header,inner['records']
