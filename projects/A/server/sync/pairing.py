"""Explicit local pairing with independently approved account/type/channel scopes."""
import json
from ..common import AppError,canonical_json
from ..security import DPAPIProtector
from .schema import require_schema

class PairingStore:
    def __init__(self,db,protector=None):
        require_schema(db);self.db=db;self.protector=protector or DPAPIProtector()

    def pair(self,device_key,public_key,*,accounts,channels,event_types,operator_keys=()):
        if not isinstance(device_key,str) or not device_key or len(device_key)>200 or not isinstance(public_key,bytes) or len(public_key)!=32:
            raise AppError(422,'INVALID_PAIRING','Stable device and Ed25519 public key required.')
        scope={}
        for name,values in [('accounts',accounts),('channels',channels),('event_types',event_types)]:
            if not isinstance(values,(list,tuple)) or not values or len(values)>200 or any(not isinstance(v,str) or not v or len(v)>200 or v=='*' for v in values):
                raise AppError(422,'INVALID_PAIRING','Explicit finite pairing scope required.')
            scope[name]=sorted(set(values))
        if not isinstance(operator_keys,(list,tuple)) or len(operator_keys)>100 or any(not isinstance(v,str) or not v or len(v)>200 or v=='*' for v in operator_keys):raise AppError(422,'INVALID_PAIRING','Explicit finite approved operators required.')
        scope['operator_keys']=sorted(set(operator_keys))
        with self.db.transaction() as conn:
            old=conn.execute('SELECT public_key FROM sync_peer WHERE device_key=?',(device_key,)).fetchone()
            if old and bytes(old[0])!=public_key: raise AppError(409,'PAIRING_KEY_CHANGED','Key change requires explicit revoke and new device identity.')
            conn.execute('INSERT INTO sync_peer VALUES(?,?,?,0) ON CONFLICT(device_key) DO UPDATE SET scope_json=excluded.scope_json,revoked=0',(device_key,public_key,canonical_json(scope).decode()))

    def revoke(self,device_key):
        with self.db.transaction() as conn:conn.execute('UPDATE sync_peer SET revoked=1 WHERE device_key=?',(device_key,))

    def authorize(self,device_key,*,account=None,channel=None,event_type=None,operator_key=None):
        with self.db.lock:row=self.db.conn.execute('SELECT * FROM sync_peer WHERE device_key=?',(device_key,)).fetchone()
        if not row or row['revoked']:raise AppError(403,'DEVICE_NOT_PAIRED','Device is unpaired or revoked.')
        scope=json.loads(row['scope_json'])
        if any(value is not None and value not in scope.get(name,[]) for name,value in [('accounts',account),('channels',channel),('event_types',event_type),('operator_keys',operator_key)]):
            raise AppError(403,'SYNC_SCOPE_DENIED','Device scope does not permit this account, channel or event type.')
        return bytes(row['public_key'])

    def install_key(self,key_id,key,*,active=True,channels=None):
        if not isinstance(key_id,str) or not key_id or len(key_id)>80 or not isinstance(key,bytes) or len(key)!=32 or type(active) is not bool:
            raise AppError(422,'INVALID_KEY','Finite key identifier and AES-256 key required.')
        protected=self.protector.protect(key)
        # Employee activity and model traffic require explicit independent keys.
        channels=channels if channels is not None else ['messages','actions','profile','collection','config','control']
        if not isinstance(channels,(list,tuple)) or not channels or len(channels)>100 or any(not isinstance(c,str) or not c or len(c)>200 or c=='*' for c in channels):raise AppError(422,'INVALID_KEY_SCOPE','Explicit finite key channels required.')
        with self.db.transaction() as conn:
            old=conn.execute('SELECT key_bytes FROM sync_key WHERE key_id=?',(key_id,)).fetchone()
            if old and self.protector.unprotect(bytes(old[0]))!=key:raise AppError(409,'KEY_ID_REUSED','Existing key identifier cannot be reused.')
            conn.execute('INSERT INTO sync_key VALUES(?,?,?) ON CONFLICT(key_id) DO UPDATE SET active=excluded.active',(key_id,protected,int(active)))
            for channel in set(channels):
                if active:conn.execute('UPDATE sync_key_channel SET active=0 WHERE channel=?',(channel,))
                conn.execute('INSERT INTO sync_key_channel VALUES(?,?,?) ON CONFLICT(key_id,channel) DO UPDATE SET active=excluded.active',(key_id,channel,int(active)))
            conn.execute('UPDATE sync_key SET active=EXISTS(SELECT 1 FROM sync_key_channel c WHERE c.key_id=sync_key.key_id AND c.active=1)')

    def key(self,key_id,*,publish=False,channel=None):
        with self.db.lock:row=self.db.conn.execute('SELECT * FROM sync_key WHERE key_id=?',(key_id,)).fetchone()
        if not row or (publish and not row['active']):raise AppError(403,'KEY_UNAVAILABLE','Key is unknown or inactive for publishing.')
        if channel is not None:
            with self.db.lock:scope=self.db.conn.execute('SELECT active FROM sync_key_channel WHERE key_id=? AND channel=?',(key_id,channel)).fetchone()
            if not scope or publish and not scope[0]:raise AppError(403,'KEY_CHANNEL_DENIED','Key does not permit this channel or is inactive for publishing.')
        return self.protector.unprotect(bytes(row['key_bytes']))
