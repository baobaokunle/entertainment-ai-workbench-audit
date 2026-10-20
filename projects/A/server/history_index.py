"""Durable Unicode n-gram candidate index with literal verification and scoped cursors."""
import base64,json
from datetime import date,datetime,time,timedelta,timezone
from .rules.daily import BJ
from .common import AppError,canonical_json
from .db import digest
from .local_deletion import guarded, sql_visible

def install_schema(db):
    with db.transaction() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS history_watermark(message_key TEXT PRIMARY KEY REFERENCES message(message_key),revision INTEGER NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS history_gram(gram TEXT NOT NULL,message_key TEXT NOT NULL REFERENCES message(message_key),PRIMARY KEY(gram,message_key)) WITHOUT ROWID')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_history_order ON message(account_key,person_key,occurred_lower_utc DESC,message_key DESC)')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_cursor_person ON message(account_key,person_key,coalesce(occurred_lower_utc,'') DESC,message_key DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_cursor_account ON message(account_key,coalesce(occurred_lower_utc,'') DESC,message_key DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_cursor_conversation ON message(account_key,conversation_key,coalesce(occurred_lower_utc,'') DESC,message_key DESC)")
        conn.execute('CREATE TABLE IF NOT EXISTS history_stats(id INTEGER PRIMARY KEY CHECK(id=1),messages INTEGER NOT NULL,indexed_current INTEGER NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO history_stats SELECT 1,(SELECT count(*) FROM message),(SELECT count(*) FROM message m JOIN history_watermark w ON w.message_key=m.message_key AND w.revision=m.active_body_revision)')
        triggers=(
            "CREATE TRIGGER IF NOT EXISTS history_message_insert AFTER INSERT ON message BEGIN UPDATE history_stats SET messages=messages+1 WHERE id=1; END",
            "CREATE TRIGGER IF NOT EXISTS history_message_delete BEFORE DELETE ON message BEGIN UPDATE history_stats SET messages=messages-1,indexed_current=indexed_current-(SELECT count(*) FROM history_watermark WHERE message_key=OLD.message_key AND revision=OLD.active_body_revision) WHERE id=1; END",
            "CREATE TRIGGER IF NOT EXISTS history_message_revision AFTER UPDATE OF active_body_revision ON message BEGIN UPDATE history_stats SET indexed_current=indexed_current+(SELECT count(*) FROM history_watermark WHERE message_key=NEW.message_key AND revision=NEW.active_body_revision)-(SELECT count(*) FROM history_watermark WHERE message_key=OLD.message_key AND revision=OLD.active_body_revision) WHERE id=1; END",
            "CREATE TRIGGER IF NOT EXISTS history_watermark_insert AFTER INSERT ON history_watermark BEGIN UPDATE history_stats SET indexed_current=indexed_current+(SELECT count(*) FROM message WHERE message_key=NEW.message_key AND active_body_revision=NEW.revision) WHERE id=1; END",
            "CREATE TRIGGER IF NOT EXISTS history_watermark_delete AFTER DELETE ON history_watermark BEGIN UPDATE history_stats SET indexed_current=indexed_current-(SELECT count(*) FROM message WHERE message_key=OLD.message_key AND active_body_revision=OLD.revision) WHERE id=1; END",
            "CREATE TRIGGER IF NOT EXISTS history_watermark_update AFTER UPDATE OF revision ON history_watermark BEGIN UPDATE history_stats SET indexed_current=indexed_current+(SELECT count(*) FROM message WHERE message_key=NEW.message_key AND active_body_revision=NEW.revision)-(SELECT count(*) FROM message WHERE message_key=OLD.message_key AND active_body_revision=OLD.revision) WHERE id=1; END")
        for statement in triggers: conn.execute(statement)
        prior=conn.execute("SELECT value FROM app_meta WHERE key='history_schema_version'").fetchone()
        if prior and prior[0] not in ('1','2'): raise AppError(409,'HISTORY_SCHEMA_INCOMPATIBLE','Explicit compatible history migration required.')
        conn.execute("INSERT INTO app_meta VALUES('history_schema_version','2') ON CONFLICT(key) DO UPDATE SET value='2'")

class HistoryIndex:
    def __init__(self,db):
        self.db=db
        with db.lock: version=db.conn.execute("SELECT value FROM app_meta WHERE key='history_schema_version'").fetchone()
        if not version or version[0]!='2': raise AppError(409,'HISTORY_NOT_INITIALIZED','Explicit history index initialization or migration required.')

    @guarded
    def update(self,message_key):
        with self.db.transaction() as conn:
            row=conn.execute('SELECT m.active_body_revision,r.body FROM message m JOIN message_revision r ON m.message_key=r.message_key AND m.active_body_revision=r.revision WHERE m.message_key=? AND '+sql_visible(self.db),(message_key,)).fetchone()
            if row is None:
                conn.execute('DELETE FROM history_gram WHERE message_key=?',(message_key,))
                conn.execute('DELETE FROM history_watermark WHERE message_key=?',(message_key,))
                return False
            conn.execute('DELETE FROM history_gram WHERE message_key=?',(message_key,))
            body=row['body'];grams={body[i:i+n] for n in (1,2,3) for i in range(max(0,len(body)-n+1))}
            conn.executemany('INSERT INTO history_gram VALUES(?,?)',((gram,message_key) for gram in sorted(grams)))
            conn.execute('INSERT INTO history_watermark VALUES(?,?) ON CONFLICT(message_key) DO UPDATE SET revision=excluded.revision',(message_key,row['active_body_revision']))
            return True

    @guarded
    def rebuild(self,limit=1000):
        if type(limit)is not int or not 1<=limit<=100000: raise AppError(422,'INVALID_LIMIT','Finite index batch required.')
        with self.db.lock: keys=[r[0] for r in self.db.conn.execute('SELECT m.message_key FROM message m LEFT JOIN history_watermark w ON w.message_key=m.message_key WHERE '+sql_visible(self.db)+' AND (w.message_key IS NULL OR w.revision<>m.active_body_revision) LIMIT ?',(limit,))]
        for key in keys: self.update(key)
        return {'indexed':len(keys),'watermark':self.status()}

    @guarded
    def status(self):
        with self.db.lock:
            if self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletion_target'").fetchone():
                total=self.db.conn.execute('SELECT count(*) FROM message m WHERE '+sql_visible(self.db)).fetchone()[0]
                fresh=self.db.conn.execute('SELECT count(*) FROM message m JOIN history_watermark w ON w.message_key=m.message_key AND w.revision=m.active_body_revision WHERE '+sql_visible(self.db)).fetchone()[0]
            else:total,fresh=self.db.conn.execute('SELECT messages,indexed_current FROM history_stats WHERE id=1').fetchone()
        result={'messages':total,'indexed_current':fresh,'pending':total-fresh}
        if self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletion_target'").fetchone():
            result['scope']='visible_messages_only'
            result['retained_inactive_messages']=self.db.conn.execute('SELECT count(*) FROM message').fetchone()[0]-total
        return result

    @guarded
    def forget(self,message_key):
        """Explicit privacy-purge hook; source text and revisions are owned by A."""
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM history_gram WHERE message_key=?',(message_key,))
            conn.execute('DELETE FROM history_watermark WHERE message_key=?',(message_key,))

    @guarded
    def search(self,account,person=None,*,conversation_key=None,keyword=None,day=None,cursor=None,limit=50):
        with self.db.lock:
            # The local lock cannot exclude commits from another connection.
            # Plan, rows and derived evidence must use the same read snapshot.
            owned=not self.db.conn.in_transaction
            if owned:self.db.conn.execute('BEGIN')
            try:
                return self._search(account,person,conversation_key=conversation_key,keyword=keyword,day=day,cursor=cursor,limit=limit)
            finally:
                if owned:self.db.conn.execute('ROLLBACK')

    def _search(self,account,person=None,*,conversation_key=None,keyword=None,day=None,cursor=None,limit=50):
        if type(limit)is not int or not 1<=limit<=200 or keyword is not None and (not isinstance(keyword,str) or len(keyword)>200): raise AppError(422,'INVALID_HISTORY_QUERY','Bounded literal history query required.')
        if day is not None:
            try:
                day=date.fromisoformat(day) if isinstance(day,str) else day
                if type(day)is not date:raise ValueError()
                begin=datetime.combine(day,time.min,BJ).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
                finish=datetime.combine(day+timedelta(days=1),time.min,BJ).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
            except (ValueError,OverflowError):
                raise AppError(422,'INVALID_HISTORY_DATE','ISO Beijing date required.') from None
        scope=digest([account,person,conversation_key,keyword,day.isoformat() if day else None])
        watermark=self.status()
        conditions=['m.account_key=?',sql_visible(self.db)];params=[account]
        if person is not None: conditions.append('m.person_key=?');params.append(person)
        if conversation_key is not None: conditions.append('m.conversation_key=?');params.append(conversation_key)
        if day is not None:
            conditions.append('(m.occurred_lower_utc>=? AND m.occurred_lower_utc<? AND m.occurred_upper_utc<=? AND (m.occurred_upper_utc<? OR m.occurred_lower_utc<m.occurred_upper_utc))');params.extend([begin,finish,finish,finish])
        inverted=bool(keyword) and watermark['pending']==0
        if keyword:
            gram=keyword[:min(3,len(keyword))]
            conditions.append('instr(r.body,?)>0');params.append(keyword)
            conditions.append('(g.gram=? AND m.message_key=g.message_key)' if inverted else '(EXISTS(SELECT 1 FROM history_gram g WHERE g.gram=? AND g.message_key=m.message_key) OR NOT EXISTS(SELECT 1 FROM history_watermark w WHERE w.message_key=m.message_key AND w.revision=m.active_body_revision))');params.append(gram)
        if cursor:
            try:
                data=json.loads(base64.urlsafe_b64decode(cursor+'='*((-len(cursor))%4)))
                if set(data)!={'scope','time','key'} or data['scope']!=scope or not isinstance(data['time'],str) or not isinstance(data['key'],str): raise ValueError()
            except (ValueError,TypeError,UnicodeError): raise AppError(422,'INVALID_CURSOR','Cursor must match the history query.')
            conditions.append("(coalesce(m.occurred_lower_utc,'')<? OR (coalesce(m.occurred_lower_utc,'')=? AND m.message_key<?))");params.extend([data['time'],data['time'],data['key']])
        source='history_gram g CROSS JOIN message m' if inverted else 'message m'
        sql='SELECT m.*,r.body FROM '+source+' JOIN message_revision r ON r.message_key=m.message_key AND r.revision=m.active_body_revision WHERE '+' AND '.join(conditions)+" ORDER BY coalesce(m.occurred_lower_utc,'') DESC,m.message_key DESC LIMIT ?"
        with self.db.lock:
            rows=[dict(r) for r in self.db.conn.execute(sql,params+[limit+1])]
            from .derived_import import check_message
            for row in rows[:limit]:check_message(self.db,row)
        more=len(rows)>limit;items=rows[:limit];next_cursor=None
        for item in items:
            item['quality']=json.loads(item.pop('quality_json'))
            item['manual_state']='verified_manual' if item['actor_state']=='attributed' else item['quality'].get('manual','manual_unknown')
        if more:
            last=items[-1];next_cursor=base64.urlsafe_b64encode(canonical_json({'scope':scope,'time':last['occurred_lower_utc'] or '','key':last['message_key']})).decode().rstrip('=')
        return {'items':items,'unknown_time':[m['message_key'] for m in items if m['occurred_lower_utc'] is None],'next_cursor':next_cursor,'index':watermark,'coverage':{'state':'observed_history','complete':False}}
