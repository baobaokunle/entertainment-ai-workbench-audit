"""Authenticated full package recovery journal independent of the transport and database."""
import hashlib,json,os
from pathlib import Path
from ..common import AppError
from ..security import SecureEnvelope
from .importer import require_no_deletion_hold

class RecoveryJournal:
    def __init__(self,path,*,key,transport_dir=None):
        self.path=Path(path).resolve();self.key=key
        if transport_dir:
            transport=Path(transport_dir).resolve()
            if self.path==transport or transport in self.path.parents:raise AppError(422,'JOURNAL_LOCATION_INVALID','Recovery journal must be outside transport.')
        self.path.mkdir(parents=True,exist_ok=True)

    def preserve(self,wire):
        name=hashlib.sha256(wire).hexdigest();final=self.path/(name+'.journal')
        if final.exists():
            if self.read(final)!=wire:raise AppError(409,'JOURNAL_COLLISION','Journal entry does not match its digest.')
            return final
        encrypted=SecureEnvelope.seal(wire,self.key,aad=name.encode())
        temporary=self.path/(name+'.tmp')
        with temporary.open('xb') as stream:stream.write(encrypted);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,final);return final

    def read(self,path):
        path=Path(path).resolve()
        if path.parent!=self.path or path.suffix!='.journal':raise AppError(422,'JOURNAL_PATH_INVALID','Journal paths must be owned entries.')
        raw=SecureEnvelope.open(path.read_bytes(),self.key,aad=path.stem.encode())
        if hashlib.sha256(raw).hexdigest()!=path.stem:raise AppError(422,'JOURNAL_HASH_INVALID','Journal body is invalid.')
        return raw

    def replay(self,importer):
        """Reapply full authenticated bodies after restore; watermarks cannot replace this."""
        # Authenticate and gate every old entry before any new ledger/raw copy.
        # The transaction also prevents a concurrent deletion commit during replay.
        with importer.db.transaction() as conn:
            wires,_=self._preflight_all(importer.db,codec=importer.codec,importer=importer)
            importer._preflight_staged(conn)
            receipts=[importer.receive(wire,preserve=False) for wire in wires]
            # Duplicate receipts do not prove reconstructed domain bodies.
            importer.retry_staged()
            for receipt in receipts:
                row=importer.db.conn.execute('SELECT state FROM sync_package WHERE package_id=?',(receipt['package_id'],)).fetchone()
                receipt['state']=row[0]
                receipt['recovery_pending']=row[0]=='staged'
        return receipts

    def preserve_local(self,events,*,db=None):
        """Low-level preservation without db retains historical compatibility.

        Context-aware callers must provide db; those hold its transaction through
        the complete encrypted write, rather than inferring deletion from bytes.
        """
        if db is not None:
            with db.transaction():
                require_no_deletion_hold(db,events)
                return self._preserve_local(events)
        return self._preserve_local(events)

    def _preserve_local(self,events):
        from ..common import canonical_json
        from .events import validate_event
        for event in events:validate_event(event)
        raw=canonical_json({'events':events});name=hashlib.sha256(raw).hexdigest()
        final=self.path/(name+'.local')
        if final.exists():
            if SecureEnvelope.open(final.read_bytes(),self.key,aad=name.encode())!=raw:raise AppError(409,'JOURNAL_COLLISION','Existing local journal body differs.')
            return final
        temporary=self.path/(name+'.local.tmp')
        with temporary.open('xb') as stream:
            stream.write(SecureEnvelope.seal(raw,self.key,aad=name.encode()));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,final);return final

    def preserve_pending(self,db):
        """Before explicit restore, preserve all old-epoch outstanding complete local bodies."""
        from .events import from_row
        with db.transaction():
            require_no_deletion_hold(db, ())
            device=dict(db.conn.execute('SELECT key,value FROM app_meta'))['device_key']
            events=[from_row(r) for r in db.conn.execute("SELECT e.* FROM domain_event e JOIN outbox o USING(event_id) WHERE e.device_key=? AND o.state!='applied' ORDER BY e.epoch,e.channel,e.seq",(device,))]
            require_no_deletion_hold(db,events)
            return self.preserve_local(events,db=db) if events else None

    def _local_events(self):
        from .events import parse_json,validate_event
        events=[]
        for path in sorted(self.path.glob('*.local')):
            raw=SecureEnvelope.open(path.read_bytes(),self.key,aad=path.stem.encode())
            if hashlib.sha256(raw).hexdigest()!=path.stem:raise AppError(422,'JOURNAL_HASH_INVALID','Local recovery digest mismatch.')
            record=parse_json(raw)
            if not isinstance(record,dict) or set(record)!={'events'} or not isinstance(record['events'],list):
                raise AppError(422,'JOURNAL_RECORD_INVALID','Local recovery must contain complete authenticated events.')
            events.extend(validate_event(event) for event in record['events'])
        return events

    def _preflight_all(self,db,*,codec=None,importer=None):
        """No partial recovery when either journal class contains a held member."""
        require_no_deletion_hold(db, ())
        local=self._local_events()
        require_no_deletion_hold(db, local)
        paths=sorted(self.path.glob('*.journal'))
        if paths and codec is None:
            installed=db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletion_meta'").fetchone()
            if installed:
                raise AppError(503,'LOCAL_DELETION_V1_HOLD','Authenticate every incoming journal before local replay; its codec is required.')
            # Legacy no-deletion local recovery did not process incoming files.
            return [],local
        wires=[]
        for path in paths:
            wire=self.read(path)
            header,events=codec.open(wire)
            if importer is not None:
                importer.preflight(db.conn,header,events)
            elif header['kind']=='DATA':
                require_no_deletion_hold(db, events)
            else:
                # ACK payloads carry publication identifiers, not event bodies.
                from .events import from_row
                for record in events:
                    if isinstance(record,dict) and isinstance(record.get('package_id'),str):
                        require_no_deletion_hold(db,[from_row(row) for row in db.conn.execute(
                            'SELECT e.* FROM sync_publication_event p JOIN domain_event e USING(event_id) WHERE p.package_id=?',
                            (record['package_id'],))])
            wires.append(wire)
        return wires,local

    def recover_local(self,db,handlers,*,codec=None):
        """Explicit restored-body replay. Original IDs remain; no new epoch until completion."""
        from ..common import canonical_json,utc_now
        from .events import validate_event
        recovered=0;unresolved={}
        with db.transaction() as conn:
            device=dict(conn.execute('SELECT key,value FROM app_meta'))['device_key']
            _,events=self._preflight_all(db,codec=codec)
            # Retained staged receipts are part of the same recovery operation.
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sync_received'").fetchone():
                require_no_deletion_hold(db,(json.loads(row[0]) for row in conn.execute(
                    "SELECT event_json FROM sync_received WHERE state='staged'")))
            pending={};sequences={}
            for event in events:
                validate_event(event)
                if event['device_key']!=device:raise AppError(403,'JOURNAL_DEVICE_INVALID','Local journal must match restored device.')
                eid=event['event_id'];sequence=(event['epoch'],event['channel'],event['seq'])
                if eid in pending and pending[eid]['event_digest']!=event['event_digest'] or sequence in sequences and sequences[sequence]!=eid:
                    raise AppError(409,'EVENT_ID_REUSED','Journal identifier or sequence has conflicting bodies.')
                sequences[sequence]=eid
                old=conn.execute('SELECT event_digest FROM domain_event WHERE event_id=?',(event['event_id'],)).fetchone()
                if old:
                    if old[0]!=event['event_digest']:raise AppError(409,'EVENT_ID_REUSED','Restored local body differs from journal.')
                    continue
                pending[eid]=event
            progress=True
            while pending and progress:
                progress=False
                for eid,event in sorted(list(pending.items()),key=lambda item:(item[1]['epoch'],item[1]['channel'],item[1]['seq'])):
                    if any(not conn.execute("SELECT 1 FROM domain_event WHERE event_id=? UNION ALL SELECT 1 FROM sync_received WHERE event_id=? AND state='applied'",(parent,parent)).fetchone() for parent in event['parents']):
                        unresolved[eid]='PARENT_PENDING';continue
                    seq=conn.execute('SELECT seq FROM event_sequence WHERE device_key=? AND epoch=? AND channel=?',(device,event['epoch'],event['channel'])).fetchone()
                    if event['seq']!=(seq[0] if seq else 0)+1:
                        unresolved[eid]='SEQUENCE_GAP';continue
                    if event['event_type'] not in handlers:
                        unresolved[eid]='RECOVERY_HANDLER_MISSING';continue
                    try:
                        with db.transaction():
                            handlers[event['event_type']](conn,event)
                            semantic={k:v for k,v in event.items() if k!='event_digest'}
                            conn.execute('INSERT INTO domain_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(eid,device,event['epoch'],event['channel'],event['seq'],event['account_key'],event['target_key'],event['event_type'],event['payload_hash'],event['event_digest'],canonical_json(event['parents']).decode(),canonical_json(semantic).decode(),event['created_at_utc']))
                            conn.execute('INSERT INTO outbox(event_id,created_at_utc) VALUES(?,?)',(eid,utc_now()))
                            conn.execute('INSERT INTO event_sequence VALUES(?,?,?,?) ON CONFLICT(device_key,epoch,channel) DO UPDATE SET seq=max(seq,excluded.seq)',(device,event['epoch'],event['channel'],event['seq']))
                    except AppError as exc:
                        if exc.code=='DEPENDENCY_PENDING':
                            unresolved[eid]='DEPENDENCY_PENDING';continue
                        raise
                    recovered+=1;progress=True;pending.pop(eid);unresolved.pop(eid,None)
        return {'recovered_bodies':recovered,'pending':unresolved,'new_epoch_allowed':False,'reason':'Confirm incoming replay and reconcile peer gaps before explicit epoch rotation.'}
