"""Durable byte-identical retries and atomic publication outside the database directory."""
import os,secrets
from pathlib import Path
from ..common import AppError,utc_now
from .events import from_row
from .schema import require_schema
from .importer import require_no_deletion_hold

class Packager:
    def __init__(self,db,codec,*,staging_dir,send_dir,journal=None):
        require_schema(db);self.db=db;self.codec=codec;self.journal=journal
        self.staging=Path(staging_dir).resolve();self.send=Path(send_dir).resolve()
        if self.staging==self.send or self.send in self.staging.parents or self.staging.anchor!=self.send.anchor:
            raise AppError(422,'STAGING_LOCATION_INVALID','Staging must be outside transport on the same volume.')
        if self.send==db.path.parent or self.send in db.path.parents:raise AppError(422,'TRANSPORT_LOCATION_INVALID','Database and transport must be separate.')
        self.staging.mkdir(parents=True,exist_ok=True);self.send.mkdir(parents=True,exist_ok=True)

    def _withdrawal_rows(self,conn,peer,limit=None):
        # A legacy mixed prepared package may already own this event's outbox
        # state. Repackage that same withdrawal alone; never copy its raw members.
        query="""SELECT e.* FROM domain_event e JOIN outbox o USING(event_id)
            WHERE e.device_key=? AND e.channel='messages' AND e.event_type='source.revoked'
            AND (o.state='pending' OR (
                EXISTS(SELECT 1 FROM sync_publication_event m JOIN sync_publication p USING(package_id)
                    WHERE m.event_id=e.event_id AND p.peer_key=? AND p.state='prepared')
                AND NOT EXISTS(SELECT 1 FROM sync_publication_event m JOIN sync_publication p USING(package_id)
                    WHERE m.event_id=e.event_id AND p.peer_key=? AND p.state='published'
                    AND NOT EXISTS(SELECT 1 FROM sync_publication_event m2 JOIN domain_event e2 ON e2.event_id=m2.event_id
                        WHERE m2.package_id=p.package_id AND e2.event_type!='source.revoked'))))
            ORDER BY e.epoch,e.seq"""
        parameters=[self.codec.device_key,peer,peer]
        if limit is not None:query+=' LIMIT ?';parameters.append(limit)
        return conn.execute(query,parameters).fetchall()

    def withdrawal_candidates(self,peer):
        from .handlers import validate_withdrawal_event
        with self.db.lock:
            require_no_deletion_hold(self.db, ())
            events=[from_row(row) for row in self._withdrawal_rows(self.db.conn,peer)]
            require_no_deletion_hold(self.db, events)
            for event in events:validate_withdrawal_event(event)
            return events

    def prepare(self,peer,channel,*,key_id,limit=100,event_types=None):
        if type(limit) is not int or not 1<=limit<=10000:raise AppError(422,'INVALID_LIMIT','Finite package count required.')
        if event_types is not None and (not isinstance(event_types,(list,tuple)) or tuple(event_types)!=('source.revoked',) or channel!='messages'):
            raise AppError(422,'PRIORITY_EVENT_INVALID','Only existing source withdrawals may use priority selection.')
        with self.db.transaction() as conn:
            require_no_deletion_hold(self.db, ())
            existing=conn.execute("SELECT p.package_id FROM sync_publication p WHERE p.peer_key=? AND p.channel=? AND p.state='prepared'"+(" AND EXISTS(SELECT 1 FROM sync_publication_event m WHERE m.package_id=p.package_id) AND NOT EXISTS(SELECT 1 FROM sync_publication_event m JOIN domain_event e USING(event_id) WHERE m.package_id=p.package_id AND e.event_type!='source.revoked')" if event_types is not None else '')+" ORDER BY p.created_at LIMIT 1",(peer,channel)).fetchone()
            if existing:
                require_no_deletion_hold(self.db, [from_row(row) for row in conn.execute(
                    'SELECT e.* FROM sync_publication_event p JOIN domain_event e USING(event_id) WHERE p.package_id=?', (existing[0],))])
                if event_types is not None:
                    from .handlers import validate_withdrawal_event
                    for member in conn.execute('SELECT e.* FROM sync_publication_event p JOIN domain_event e USING(event_id) WHERE p.package_id=?',(existing[0],)):
                        validate_withdrawal_event(from_row(member))
                return existing[0]
            rows=self._withdrawal_rows(conn,peer,limit) if event_types is not None else conn.execute("SELECT e.* FROM domain_event e JOIN outbox o ON o.event_id=e.event_id WHERE o.state='pending' AND e.device_key=? AND e.channel=? ORDER BY e.epoch,e.seq LIMIT ?",(self.codec.device_key,channel,limit)).fetchall()
            if not rows:return None
            events=[from_row(r) for r in rows]
            require_no_deletion_hold(self.db, events)
            # Priority withdrawals may reuse an old prepared mixed publication.
            # A held mixed v1 operation cannot be rewritten as a survivor package.
            if event_types is not None:
                for event in events:
                    owners=conn.execute("SELECT p.package_id FROM sync_publication p JOIN sync_publication_event m USING(package_id) WHERE m.event_id=? AND p.peer_key=? AND p.state='prepared'",
                                        (event['event_id'],peer)).fetchall()
                    for owner in owners:
                        require_no_deletion_hold(self.db,[from_row(member) for member in conn.execute(
                            'SELECT e.* FROM sync_publication_event m JOIN domain_event e USING(event_id) WHERE m.package_id=?', (owner[0],))])
            if event_types is not None:
                from .handlers import validate_withdrawal_event
                for event in events:validate_withdrawal_event(event)
            # Split a large batch before mutation; a single oversized event fails clearly.
            while True:
                try:wire=self.codec.seal(peer,channel,events,key_id=key_id);break
                except AppError as exc:
                    if exc.code!='PACKAGE_SIZE_LIMIT' or len(events)==1:raise
                    events=events[:max(1,len(events)//2)]
            from .events import parse_json
            pid=parse_json(wire)['header']['package_id']
            require_no_deletion_hold(self.db, events)
            if self.journal:self.journal.preserve_local(events,db=self.db)
            conn.execute('INSERT INTO sync_publication VALUES(?,?,?,?,?,?)',(pid,peer,channel,wire,'prepared',utc_now()))
            for event in events:
                conn.execute('INSERT INTO sync_publication_event VALUES(?,?)',(pid,event['event_id']))
                conn.execute("UPDATE outbox SET state='published',attempts=attempts+1 WHERE event_id=?",(event['event_id'],))
            return pid

    def publish(self,package_id):
        # BEGIN IMMEDIATE also serializes deletion commits from another Database
        # connection; hold this transaction through the atomic filesystem write.
        with self.db.transaction() as conn:
            require_no_deletion_hold(self.db, ())
            row=conn.execute('SELECT wire,peer_key,channel FROM sync_publication WHERE package_id=?',(package_id,)).fetchone()
            if not row:raise AppError(404,'PUBLICATION_UNKNOWN','Prepared publication is unknown.')
            members=[from_row(member) for member in conn.execute(
                'SELECT e.* FROM sync_publication_event p JOIN domain_event e USING(event_id) WHERE p.package_id=?',(package_id,))]
            require_no_deletion_hold(self.db, members)
            self.codec.pairing.authorize(row['peer_key'],channel=row['channel'])
            for member in members:
                self.codec.pairing.authorize(row['peer_key'],account=member['account_key'],event_type=member['event_type'])
            wire=bytes(row[0]);final=self.send/(package_id+'.dfa')
            require_no_deletion_hold(self.db, members)
            if final.exists():
                if final.read_bytes()!=wire:raise AppError(409,'PUBLICATION_COLLISION','Final package differs from durable bytes.')
                conn.execute("UPDATE sync_publication SET state='published' WHERE package_id=? AND state='prepared'",(package_id,))
                return final
            scratch=self.staging/(secrets.token_hex(16)+'.tmp')
            try:
                with scratch.open('xb') as stream:stream.write(wire);stream.flush();os.fsync(stream.fileno())
                os.replace(scratch,final)
            finally:
                if scratch.exists():scratch.unlink()
            conn.execute("UPDATE sync_publication SET state='published' WHERE package_id=? AND state='prepared'",(package_id,))
            return final

    def acknowledge_wire(self,importer,package_id,recipient,channel,*,key_id):
        if importer.db is not self.db:
            raise AppError(409,'ACK_DATABASE_MISMATCH','ACK codec and receipt must share their guarded database.')
        with self.db.transaction():
            require_no_deletion_hold(self.db, ())
            records=importer.ack_records(package_id)
            return self.codec.seal(recipient,channel,records,key_id=key_id,kind='CONTROL_ACK')

    def retry(self,package_id):return self.publish(package_id)
