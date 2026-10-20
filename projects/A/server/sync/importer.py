"""Transactional receive ledger; per-channel sequence gaps and causal dependencies stay staged."""
import hashlib,json
from ..common import AppError,canonical_json,utc_now
from .schema import require_schema


def require_no_deletion_hold(db, events):
    """Preflight a complete v1 action under its caller's DB lock/transaction.

    This neither changes authenticated events nor advances a rejected receipt.
    An installed, unbound deletion schema fails closed through require_ready.
    """
    from ..local_deletion import require_ready, event_requires_hold
    require_ready(db)
    for event in events:
        if event_requires_hold(db, event):
            raise AppError(503, 'LOCAL_DELETION_V1_HOLD',
                           'This complete v1 operation references a locally suppressed object; signed copies remain pending.')


class Importer:
    def __init__(self,db,codec,*,journal=None):
        require_schema(db);self.db=db;self.codec=codec;self.journal=journal;self.handlers={}

    def register_handler(self,event_type,handler):
        if event_type in self.handlers:raise AppError(409,'HANDLER_EXISTS','An event handler is already registered.')
        self.handlers[event_type]=handler

    def receive(self,wire,*,preserve=True):
        try:return self._receive(wire,preserve=preserve)
        except AppError as exc:
            if isinstance(wire,bytes):
                with self.db.transaction() as conn:conn.execute('INSERT OR IGNORE INTO sync_rejection(wire_digest,reason,received_at) VALUES(?,?,?)',(hashlib.sha256(wire).hexdigest(),exc.code,utc_now()))
            raise

    def _receive(self,wire,*,preserve=True):
        header,events=self.codec.open(wire)
        package_digest=hashlib.sha256(wire).hexdigest();pid=header['package_id']
        with self.db.transaction() as conn:
            self.preflight(conn, header, events)
            # Drain can also revisit old staged packages. Hold before copying any
            # new wire if any member of that complete operation is suppressed.
            self._preflight_staged(conn)
            old=conn.execute('SELECT digest,state FROM sync_package WHERE package_id=?',(pid,)).fetchone()
            if old:
                if old['digest']!=package_digest:raise AppError(409,'PACKAGE_ID_REUSED','Package ID has different authenticated content.')
                if preserve and self.journal:self.journal.preserve(wire)
                return {'status':'duplicate','package_id':pid,'state':old['state'],'ack_required':header['kind']=='DATA'}
            if header['kind']=='CONTROL_ACK':
                if preserve and self.journal:self.journal.preserve(wire)
                return self._ack(conn,header,events,wire,package_digest)
            # Preflight complete package ID/sequence reuse before any write.
            ids={};sequences={}
            for event in events:
                eid=event['event_id'];seq=(event['device_key'],event['epoch'],event['channel'],event['seq'])
                old=conn.execute('SELECT event_digest FROM sync_received WHERE event_id=? OR (device_key=? AND epoch=? AND channel=? AND seq=?)',(eid,*seq)).fetchone()
                if old and old[0]!=event['event_digest'] or eid in ids and ids[eid]!=event['event_digest'] or seq in sequences and sequences[seq]!=eid:
                    raise AppError(409,'EVENT_ID_REUSED','Same event ID or sequence has different complete semantics.')
                ids[eid]=event['event_digest'];sequences[seq]=eid
            require_no_deletion_hold(self.db, events)
            if preserve and self.journal:self.journal.preserve(wire)
            conn.execute('INSERT INTO sync_package VALUES(?,?,?,NULL,?,?)',(pid,package_digest,'received',wire,utc_now()))
            for event in events:
                conn.execute('INSERT OR IGNORE INTO sync_received VALUES(?,?,?,?,?,?,?,?,NULL)',(event['event_id'],event['event_digest'],event['device_key'],event['epoch'],event['channel'],event['seq'],canonical_json(event).decode(),'staged'))
                conn.execute('INSERT OR IGNORE INTO sync_package_event VALUES(?,?)',(pid,event['event_id']))
            self._drain(conn)
            states=[conn.execute('SELECT state FROM sync_received WHERE event_id=?',(e['event_id'],)).fetchone()[0] for e in events]
            state='applied' if all(s=='applied' for s in states) else 'terminal_rejected' if any(s=='terminal_rejected' for s in states) else 'staged'
            conn.execute('UPDATE sync_package SET state=? WHERE package_id=?',(state,pid))
            return {'status':state,'package_id':pid,'event_states':states,'ack_required':True}

    def preflight(self, conn, header, events):
        """Authenticated package preflight; CONTROL_ACK binds original members."""
        if header['kind']=='DATA':
            require_no_deletion_hold(self.db, events)
        else:
            from .events import from_row
            require_no_deletion_hold(self.db, ())
            for record in events:
                if isinstance(record,dict) and isinstance(record.get('package_id'),str):
                    members=[from_row(row) for row in conn.execute(
                        'SELECT e.* FROM sync_publication_event p JOIN domain_event e USING(event_id) WHERE p.package_id=?',
                        (record['package_id'],))]
                    require_no_deletion_hold(self.db, members)

    def _preflight_staged(self, conn):
        require_no_deletion_hold(self.db, (json.loads(row[0]) for row in conn.execute(
            "SELECT event_json FROM sync_received WHERE state='staged'")))

    def _drain(self,conn):
        self._preflight_staged(conn)
        self._preapply_withdrawals(conn)
        progress=True
        while progress:
            progress=False
            for row in conn.execute("SELECT * FROM sync_received WHERE state='staged' ORDER BY device_key,epoch,channel,seq").fetchall():
                event=json.loads(row['event_json']);key=(row['device_key'],row['epoch'],row['channel'])
                conn.execute('INSERT OR IGNORE INTO sync_watermark(device_key,epoch,channel) VALUES(?,?,?)',key)
                processed=conn.execute('SELECT processed FROM sync_watermark WHERE device_key=? AND epoch=? AND channel=?',key).fetchone()[0]
                if row['seq']!=processed+1:
                    conn.execute("UPDATE sync_received SET reason='SEQUENCE_GAP' WHERE event_id=?",(row['event_id'],));continue
                if any(not conn.execute("SELECT 1 FROM sync_received WHERE event_id=? AND state='applied' UNION ALL SELECT 1 FROM domain_event WHERE event_id=?",(p,p)).fetchone() for p in event['parents']):
                    conn.execute("UPDATE sync_received SET reason='PARENT_PENDING' WHERE event_id=?",(row['event_id'],));continue
                handler=self.handlers.get(event['event_type'])
                # Handler errors rollback all domain writes for this event; receipt remains explicit.
                conn.execute('SAVEPOINT apply_event')
                try:
                    if not handler:raise AppError(422,'EVENT_TYPE_UNSUPPORTED','No independently registered domain handler.')
                    handler(conn,event)
                    conn.execute('RELEASE apply_event');state='applied';reason=None
                except AppError as exc:
                    conn.execute('ROLLBACK TO apply_event');conn.execute('RELEASE apply_event')
                    if exc.code=='DEPENDENCY_PENDING':
                        conn.execute("UPDATE sync_received SET reason='DEPENDENCY_PENDING' WHERE event_id=?",(row['event_id'],))
                        continue
                    state='terminal_rejected';reason=exc.code
                conn.execute('UPDATE sync_received SET state=?,reason=? WHERE event_id=?',(state,reason,row['event_id']))
                conn.execute('UPDATE sync_watermark SET processed=? WHERE device_key=? AND epoch=? AND channel=?',(row['seq'],*key));progress=True
        for keyrow in conn.execute('SELECT device_key,epoch,channel FROM sync_watermark').fetchall():
            key=tuple(keyrow)
            for column,accepted in [('received',{'staged','applied','terminal_rejected'}),('applied',{'applied'})]:
                continuous=0
                for row in conn.execute('SELECT seq,state FROM sync_received WHERE device_key=? AND epoch=? AND channel=? ORDER BY seq',key):
                    if row['seq']!=continuous+1 or row['state'] not in accepted:break
                    continuous=row['seq']
                conn.execute('UPDATE sync_watermark SET '+column+'=? WHERE device_key=? AND epoch=? AND channel=?',(continuous,*key))
        for package in conn.execute('SELECT DISTINCT package_id FROM sync_package_event').fetchall():
            states=[r[0] for r in conn.execute('SELECT r.state FROM sync_package_event p JOIN sync_received r USING(event_id) WHERE p.package_id=?',(package[0],))]
            state='applied' if all(s=='applied' for s in states) else 'terminal_rejected' if any(s=='terminal_rejected' for s in states) else 'staged'
            conn.execute('UPDATE sync_package SET state=? WHERE package_id=?',(state,package[0]))

    def _preapply_withdrawals(self,conn):
        """Lower origin authority before gaps close, without advancing any ledger.

        Only received, fully authenticated DATA events enter this ledger. The
        narrowly selected handler is identical to normal application, including
        runtime account approval. Its durable marker binds the complete digest;
        it does not assert receipt/application of any missing predecessor.
        """
        from .events import validate_event
        from .handlers import validate_withdrawal_event
        handler=self.handlers.get('source.revoked')
        if handler is None:return
        rows=conn.execute("SELECT event_json,event_digest FROM sync_received WHERE state='staged' AND json_extract(event_json,'$.event_type')='source.revoked'").fetchall()
        for row in rows:
            event=validate_event(json.loads(row['event_json']))
            key='withdrawal_preapplied:'+event['event_id']
            prior=conn.execute('SELECT value FROM sync_meta WHERE key=?',(key,)).fetchone()
            if event['event_digest']!=row['event_digest'] or prior and prior[0]!=event['event_digest']:
                raise AppError(409,'WITHDRAWAL_DIGEST_CONFLICT','Withdrawal marker must bind the complete authenticated event.')
            if prior:continue
            conn.execute('SAVEPOINT preapply_withdrawal')
            try:
                validate_withdrawal_event(event)
                handler(conn,event)
                conn.execute('INSERT INTO sync_meta VALUES(?,?)',(key,event['event_digest']))
                conn.execute('RELEASE preapply_withdrawal')
            except AppError:
                # Missing dependencies or invalid target remain subject to the
                # ordinary ordered drain, including its terminal-rejection rules.
                conn.execute('ROLLBACK TO preapply_withdrawal');conn.execute('RELEASE preapply_withdrawal')

    def retry_staged(self):
        with self.db.transaction() as conn:self._drain(conn)

    def ack_records(self,package_id):
        with self.db.lock:
            row=self.db.conn.execute('SELECT * FROM sync_package WHERE package_id=?',(package_id,)).fetchone()
            if not row:raise AppError(404,'PACKAGE_UNKNOWN','No received package.')
            header,events=self.codec.open(bytes(row['wire']))
            if header['kind']!='DATA':raise AppError(422,'ACK_LOOP_REJECTED','Control acknowledgements never generate acknowledgements.')
            require_no_deletion_hold(self.db, events)
            return [{'package_id':package_id,'events':[{'event_id':e['event_id'],'event_digest':e['event_digest'],'state':self.db.conn.execute('SELECT state FROM sync_received WHERE event_id=?',(e['event_id'],)).fetchone()[0]} for e in events]}]

    def _ack(self,conn,header,records,wire,wire_digest):
        for record in records:
            if not isinstance(record,dict) or set(record)!={'package_id','events'} or not isinstance(record['events'],list) or len(record['events'])>10000:raise AppError(422,'ACK_INVALID','Invalid control record.')
            publication=conn.execute('SELECT peer_key FROM sync_publication WHERE package_id=?',(record['package_id'],)).fetchone()
            if not publication or publication[0]!=header['sender']:raise AppError(403,'ACK_WRONG_PEER','ACK must match the intended publication recipient.')
            expected=dict(conn.execute('SELECT e.event_id,e.event_digest FROM sync_publication_event p JOIN domain_event e ON e.event_id=p.event_id WHERE p.package_id=?',(record['package_id'],)))
            seen=set()
            for item in record['events']:
                if not isinstance(item,dict) or set(item)!={'event_id','event_digest','state'} or item['event_id'] in seen or expected.get(item['event_id'])!=item['event_digest'] or item['state'] not in ('staged','applied','terminal_rejected'):
                    raise AppError(422,'ACK_INVALID','ACK does not bind exact published event digests.')
                seen.add(item['event_id'])
            if seen!=set(expected):raise AppError(422,'ACK_INVALID','ACK must describe the complete publication.')
            for item in record['events']:
                if item['state']=='applied':conn.execute("UPDATE outbox SET state='applied' WHERE event_id=?",(item['event_id'],))
            conn.execute('INSERT INTO sync_ack VALUES(?,?,?) ON CONFLICT(package_id) DO UPDATE SET ack_json=excluded.ack_json,created_at=excluded.created_at',(record['package_id'],canonical_json(record).decode(),utc_now()))
        conn.execute('INSERT INTO sync_package VALUES(?,?,?,NULL,?,?)',(header['package_id'],wire_digest,'applied',wire,utc_now()))
        return {'status':'applied','package_id':header['package_id'],'ack_required':False}

    def status(self):
        with self.db.lock:
            watermarks=[dict(r) for r in self.db.conn.execute('SELECT * FROM sync_watermark ORDER BY device_key,epoch,channel')]
            for watermark in watermarks:
                key=(watermark['device_key'],watermark['epoch'],watermark['channel']);last=0;ranges=[]
                for row in self.db.conn.execute('SELECT seq FROM sync_received WHERE device_key=? AND epoch=? AND channel=? ORDER BY seq',key):
                    if row[0]>last+1:ranges.append([last+1,row[0]-1])
                    last=row[0]
                watermark['missing_ranges']=ranges
            events=[dict(r) for r in self.db.conn.execute("SELECT r.event_id,r.state,r.reason,m.value AS withdrawal_preapplied_digest FROM sync_received r LEFT JOIN sync_meta m ON m.key='withdrawal_preapplied:'||r.event_id")]
            for event in events:event['withdrawal_preapplied']=event['withdrawal_preapplied_digest'] is not None
            return {'watermarks':watermarks,'events':events,'rejections':[dict(r) for r in self.db.conn.execute('SELECT * FROM sync_rejection')],'coverage':'transport_ledger_only'}
