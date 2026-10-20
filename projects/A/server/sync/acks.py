"""Durable signed control ACK queue; no domain event, counter or recursive ACK."""
import os,secrets
from ..common import AppError,utc_now
from ..db import digest
from .events import parse_json
from .packager import Packager
from .importer import require_no_deletion_hold

class Acks:
    def __init__(self,db,codec,*,staging_dir,send_dir):
        # Reuse strict deployment path checks without publishing a domain-event package.
        paths=Packager(db,codec,staging_dir=staging_dir,send_dir=send_dir)
        self.db=db;self.codec=codec;self.staging=paths.staging;self.send=paths.send

    def queue(self,importer,package_id,recipient,channel,*,key_id):
        if importer.db is not self.db:
            raise AppError(409,'ACK_DATABASE_MISMATCH','ACK records must use the same database as their durable queue.')
        with self.db.transaction() as conn:
            records=importer.ack_records(package_id)
            original=conn.execute('SELECT wire FROM sync_package WHERE package_id=?',(package_id,)).fetchone()
            header,_=self.codec.open(bytes(original[0]))
            if header['sender']!=recipient:raise AppError(403,'ACK_WRONG_ROUTE','ACK recipient must be the original source.')
            self._preflight(conn,records,recipient)
            hashed=digest({'recipient':recipient,'channel':channel,'records':records})
            old=conn.execute('SELECT control_id FROM sync_control_outbox WHERE record_digest=?',(hashed,)).fetchone()
            if old:return old[0]
            wire=self.codec.seal(recipient,channel,records,key_id=key_id,kind='CONTROL_ACK')
            control_id=parse_json(wire)['header']['package_id']
            conn.execute('INSERT INTO sync_control_outbox VALUES(?,?,?,?,?,?,?)',(control_id,hashed,recipient,channel,wire,'prepared',utc_now()))
            return control_id

    def _preflight(self,conn,records,recipient):
        """Bind the full authenticated ACK to its original received DATA bodies."""
        events=[]
        for record in records:
            if not isinstance(record,dict) or set(record)!={'package_id','events'} or not isinstance(record['events'],list):
                raise AppError(422,'ACK_INVALID','Complete original package references required.')
            original=conn.execute('SELECT wire FROM sync_package WHERE package_id=?',(record['package_id'],)).fetchone()
            if original is None:raise AppError(409,'ACK_ORIGINAL_MISSING','Original received DATA must remain available for ACK verification.')
            header,members=self.codec.open(bytes(original[0]))
            if header['kind']!='DATA' or header['sender']!=recipient or header['package_id']!=record['package_id']:
                raise AppError(403,'ACK_WRONG_ROUTE','ACK must bind the original DATA source and package.')
            expected={e['event_id']:e['event_digest'] for e in members}
            seen=set()
            for item in record['events']:
                if (not isinstance(item,dict) or set(item)!={'event_id','event_digest','state'}
                    or not isinstance(item['event_id'],str) or item['event_id'] in seen
                    or expected.get(item['event_id'])!=item['event_digest']
                    or item['state'] not in ('staged','applied','terminal_rejected')):
                    raise AppError(422,'ACK_INVALID','ACK must describe exact original immutable event identities.')
                seen.add(item['event_id'])
            if seen!=set(expected):raise AppError(422,'ACK_INVALID','ACK must cover the complete original DATA package.')
            events.extend(members)
        require_no_deletion_hold(self.db,events)

    def publish(self,control_id):
        with self.db.transaction() as conn:
            row=conn.execute('SELECT wire,record_digest,recipient,channel FROM sync_control_outbox WHERE control_id=?',(control_id,)).fetchone()
            if not row:raise AppError(404,'CONTROL_UNKNOWN','Prepared signed ACK is unknown.')
            wire=bytes(row['wire'])
            header,records=self.codec.open_own_control(wire)
            if header['package_id']!=control_id or header['recipient']!=row['recipient'] or header['channel']!=row['channel']:
                raise AppError(403,'ACK_WRONG_ROUTE','Durable ACK metadata must match its own authenticated wire.')
            if digest({'recipient':row['recipient'],'channel':row['channel'],'records':records})!=row['record_digest']:
                raise AppError(409,'CONTROL_COLLISION','Durable ACK records do not match their original digest.')
            self._preflight(conn,records,row['recipient'])
            final=self.send/(control_id+'.dfa')
            if final.exists():
                if final.read_bytes()!=wire:raise AppError(409,'CONTROL_COLLISION','Published ACK differs from durable bytes.')
            else:
                scratch=self.staging/(secrets.token_hex(16)+'.tmp')
                try:
                    with scratch.open('xb') as stream:stream.write(wire);stream.flush();os.fsync(stream.fileno())
                    os.replace(scratch,final)
                finally:
                    if scratch.exists():scratch.unlink()
            conn.execute("UPDATE sync_control_outbox SET state='published' WHERE control_id=?",(control_id,))
            return final

    def pending(self):
        with self.db.lock:return [dict(r) for r in self.db.conn.execute("SELECT control_id,recipient,channel,state,created_at FROM sync_control_outbox WHERE state='prepared' ORDER BY created_at")]
