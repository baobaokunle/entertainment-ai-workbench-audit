"""Baobao-owned finite collection coordinator. No browser or arbitrary commands."""
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import json,uuid
from .common import AppError,utc_now,canonical_json
from .sync.schema import require_schema

BJ=ZoneInfo('Asia/Shanghai')

def actor_ref(principal):
    return {'operator_key':principal.operator_key,'device_key':principal.device_key,'session_id':principal.session_id}

class CollectionService:
    def __init__(self,db,*,owner_device,task_key='nightly-source-collection'):
        require_schema(db);self.db=db;self.owner_device=owner_device;self.task_key=task_key

    def initialize(self):
        """Explicit local deployment setup only; constructor and tick never install a plan."""
        with self.db.transaction() as conn:
            conn.execute('INSERT OR IGNORE INTO collection_config VALUES(?,1,1,4,0,?)',(self.task_key,self.owner_device))
            row=conn.execute('SELECT owner_device FROM collection_config WHERE task_key=?',(self.task_key,)).fetchone()
            if row[0]!=self.owner_device:raise AppError(409,'COLLECTION_OWNER_CHANGED','Existing collection owner cannot be silently replaced.')

    def config(self):
        with self.db.lock:row=self.db.conn.execute('SELECT * FROM collection_config WHERE task_key=?',(self.task_key,)).fetchone()
        if not row:raise AppError(409,'COLLECTION_NOT_INITIALIZED','Explicit collection plan initialization required.')
        return dict(row)

    def _authorize(self,principal):
        principal.require('collection:run')
        if principal.role!='baobao' or principal.device_key!=self.owner_device:raise AppError(403,'COLLECTION_OWNER_ONLY','Only the designated baobao device may collect.')
        if dict(self.db.conn.execute('SELECT key,value FROM app_meta'))['device_key']!=self.owner_device:raise AppError(403,'COLLECTION_WRONG_DEVICE','This installation is not the designated collector.')

    def start(self,principal,*,now=None,trigger='manual',user_resume=False):
        self._authorize(principal)
        if trigger not in ('scheduled','manual','retry') or type(user_resume) is not bool:raise AppError(422,'INVALID_COLLECTION_TRIGGER','Only finite collection triggers are allowed.')
        now=now or datetime.now(timezone.utc)
        if not isinstance(now,datetime) or now.tzinfo is None:raise AppError(422,'COLLECTION_TIME_INVALID','An aware clock is required.')
        local=now.astimezone(BJ);day=local.date().isoformat()
        with self.db.transaction() as conn:
            config=self.config()
            if not config['enabled']:return {'status':'disabled'}
            if trigger=='scheduled' and (local.hour,local.minute)!=(config['hour'],config['minute']):
                if (local.hour,local.minute)>(config['hour'],config['minute']):
                    conn.execute('INSERT OR IGNORE INTO collection_gap VALUES(?,?,?,0)',(self.task_key,day,'missed_schedule'))
                return {'status':'not_due','catchup':'next_night'}
            active=conn.execute("SELECT run_key FROM collection_run WHERE task_key=? AND state='running'",(self.task_key,)).fetchone()
            if active:return {'status':'locked','run_key':active[0]}
            old=conn.execute('SELECT * FROM collection_run WHERE task_key=? AND day=?',(self.task_key,day)).fetchone()
            if old:
                if old['state']=='complete':return {'status':'duplicate','run_key':old['run_key']}
                if trigger!='retry':return {'status':old['state'],'run_key':old['run_key']}
                if old['state'] in ('blocked_login','blocked_validation') and not user_resume:return {'status':old['state'],'run_key':old['run_key'],'retry':'requires_user_resume'}
                run_key=old['run_key'];conn.execute("UPDATE collection_run SET state='running',started_at=?,finished_at=NULL,reason=NULL WHERE run_key=?",(now.astimezone(timezone.utc).isoformat(),run_key))
            else:
                run_key='run_'+uuid.uuid4().hex
                conn.execute('INSERT INTO collection_run VALUES(?,?,?,?,?,?,?,NULL,NULL)',(run_key,self.task_key,day,trigger,'running',self.owner_device,now.astimezone(timezone.utc).isoformat()))
            gaps=[dict(r) for r in conn.execute('SELECT day,reason FROM collection_gap WHERE task_key=? AND resolved=0 ORDER BY day',(self.task_key,))]
            return {'status':'running','run_key':run_key,'task_key':self.task_key,'day':day,'merge_gaps':gaps,'collector':'source_adapter_unverified'}

    def tick(self,principal,*,now=None):return self.start(principal,now=now,trigger='scheduled')

    def finish(self,principal,run_key,*,state,reason=None,resolved_days=()):
        self._authorize(principal)
        if state not in ('complete','partial','failed','blocked_login','blocked_validation') or reason is not None and (not isinstance(reason,str) or len(reason)>200):raise AppError(422,'INVALID_RUN_STATE','Finite run result required.')
        with self.db.transaction() as conn:
            row=conn.execute('SELECT * FROM collection_run WHERE run_key=?',(run_key,)).fetchone()
            if not row or row['task_key']!=self.task_key or row['state']!='running':raise AppError(409,'RUN_NOT_ACTIVE','Run must hold this task lock.')
            conn.execute('UPDATE collection_run SET state=?,finished_at=?,reason=? WHERE run_key=?',(state,utc_now(),reason,run_key))
            if state!='complete':conn.execute('INSERT OR REPLACE INTO collection_gap VALUES(?,?,?,0)',(self.task_key,row['day'],state))
            # Only explicitly verified coverage closes gaps, never merely a successful run.
            for day in resolved_days:
                if not isinstance(day,str) or len(day)!=10:raise AppError(422,'INVALID_GAP_DATE','Explicit verified dates required.')
                conn.execute('UPDATE collection_gap SET resolved=1 WHERE task_key=? AND day=?',(self.task_key,day))
        return {'status':state,'run_key':run_key}

    def recover_interrupted(self,principal,*,process_confirmed_dead=False):
        self._authorize(principal)
        if process_confirmed_dead is not True:raise AppError(409,'RUN_LIVENESS_UNKNOWN','Confirm prior worker exit before releasing persistent locks.')
        with self.db.transaction() as conn:
            rows=conn.execute("SELECT run_key,day FROM collection_run WHERE task_key=? AND state='running'",(self.task_key,)).fetchall()
            for row in rows:
                conn.execute("UPDATE collection_run SET state='failed',reason='worker_interrupted',finished_at=? WHERE run_key=?",(utc_now(),row['run_key']))
                conn.execute('INSERT OR REPLACE INTO collection_gap VALUES(?,?,?,0)',(self.task_key,row['day'],'worker_interrupted'))
            return {'released':len(rows),'automatic_catchup':False}

    def status(self):
        with self.db.lock:
            return {'config':self.config(),'runs':[dict(r) for r in self.db.conn.execute('SELECT * FROM collection_run WHERE task_key=? ORDER BY day DESC LIMIT 100',(self.task_key,))],'gaps':[dict(r) for r in self.db.conn.execute('SELECT * FROM collection_gap WHERE task_key=? AND resolved=0',(self.task_key,))],'source_adapter':'unverified','ai_status':'independent'}
