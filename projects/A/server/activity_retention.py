"""Explicit candidate activity-only archive/expiry. Other immutable copies remain pending."""
import calendar,json
from datetime import datetime,timedelta,timezone
from .common import AppError,canonical_json
from .activity import ActivityService,indexed_utc,seconds
from .profile import pinned_timezone

POLICY_FIELDS={'enabled','raw_days','summary_months','diagnostics_days'}
DEFAULT_POLICY={'enabled':False,'raw_days':30,'summary_months':12,'diagnostics_days':14}

def validate_policy(policy):
    if not isinstance(policy,dict) or set(policy)!=POLICY_FIELDS or type(policy['enabled']) is not bool or type(policy['raw_days']) is not int or policy['raw_days'] not in (30,90) or type(policy['summary_months']) is not int or policy['summary_months']!=12 or type(policy['diagnostics_days']) is not int or policy['diagnostics_days']!=14:
        raise AppError(422,'ACTIVITY_RETENTION_POLICY_INVALID','仅允许架构候选30/90日细节、12个月汇总、14日诊断，默认关闭')
    return dict(policy)

def install_schema(db):
    with db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='activity_event'").fetchone():raise AppError(409,'ACTIVITY_NOT_INITIALIZED','先显式初始化活动模式')
        conn.execute('CREATE TABLE IF NOT EXISTS activity_retention_policy(id INTEGER PRIMARY KEY CHECK(id=1),policy_json TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS activity_daily_archive(device_key TEXT NOT NULL,operator_key TEXT NOT NULL,day TEXT NOT NULL,algorithm_version TEXT NOT NULL,tau INTEGER NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(device_key,operator_key,day,algorithm_version,tau))')
        conn.execute('CREATE TABLE IF NOT EXISTS activity_expiry(device_key TEXT NOT NULL,operator_key TEXT NOT NULL,cutoff_utc TEXT NOT NULL,PRIMARY KEY(device_key,operator_key))')
        conn.execute("INSERT OR IGNORE INTO activity_retention_policy VALUES(1,?)",(canonical_json(DEFAULT_POLICY).decode(),))
        conn.execute("INSERT OR IGNORE INTO app_meta VALUES('activity_retention_schema','1')")
        for table in ('activity_daily_archive','activity_retention_policy','activity_expiry'):
            for operation in ('INSERT','UPDATE','DELETE'):
                conn.execute('CREATE TRIGGER IF NOT EXISTS activity_version_'+table+'_'+operation+' AFTER '+operation+' ON '+table+" BEGIN UPDATE app_meta SET value=CAST(value AS INTEGER)+1 WHERE key='data_version'; END")

def _month_before(day,months):
    year=day.year+(day.month-1-months)//12;month=(day.month-1-months)%12+1
    return day.replace(year=year,month=month,day=min(day.day,calendar.monthrange(year,month)[1]))

class ActivityRetention:
    def __init__(self,db):
        self.db=db
        with db.lock:
            if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='activity_retention_policy'").fetchone():raise AppError(409,'ACTIVITY_RETENTION_NOT_INITIALIZED','活动留存需显式初始化')
    def policy(self):
        with self.db.lock:return json.loads(self.db.conn.execute('SELECT policy_json FROM activity_retention_policy WHERE id=1').fetchone()[0])
    def configure(self,policy,principal):
        self._authorize(principal);policy=validate_policy(policy)
        with self.db.transaction() as conn:conn.execute('UPDATE activity_retention_policy SET policy_json=? WHERE id=1',(canonical_json(policy).decode(),))
        return {'policy':policy,'task_installed':False,'cleanup_started':False}
    def _authorize(self,principal):
        principal.require('activities:retain')
        if principal.role!='manager':raise AppError(403,'forbidden','活动留存维护仅限独立授权管理')
    def maintain(self,principal,*,now,limit=100000):
        self._authorize(principal)
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset()!=timedelta(0) or type(limit) is not int or not 1<=limit<=100000:raise AppError(422,'ACTIVITY_RETENTION_WINDOW_INVALID','需明确UTC维护时刻及有限批量')
        policy=self.policy()
        if not policy['enabled']:return {'state':'disabled','deleted_rows':0,'archived_days':0,'tasks_installed':False}
        zone=pinned_timezone('Asia/Shanghai');today=now.astimezone(zone).date();raw_day=today-timedelta(days=policy['raw_days']);summary_day=_month_before(today,12)
        cutoff=datetime.combine(raw_day,datetime.min.time(),zone).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
        archived=0;deleted=0
        with self.db.transaction() as conn:
            # Never partially process a device/day and then fabricate its summary.
            rows=conn.execute('SELECT * FROM activity_event WHERE wall_utc<? ORDER BY device_key,operator_key,wall_utc,event_id LIMIT ?',(cutoff,limit+1)).fetchall()
            if len(rows)>limit:raise AppError(413,'ACTIVITY_RETENTION_BATCH_LIMIT','细节批量超过维护上限；事务未清理')
            groups={}
            for row in rows:
                day=datetime.fromisoformat(row['wall_utc'].replace('Z','+00:00')).astimezone(zone).date()
                groups.setdefault((row['device_key'],row['operator_key'],day),[]).append(row)
            for (device,operator,day),items in groups.items():
                if day<summary_day:continue
                raw=json.loads(items[0]['payload_json'])
                service=ActivityService(self.db,device_key=device,windows_session_id=raw['windows_session_id'],user_sid=raw['user_sid'])
                start=datetime.combine(day,datetime.min.time(),zone).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
                end=datetime.combine(day+timedelta(days=1),datetime.min.time(),zone).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
                for tau in (30,60,90):
                    value={'day':day.isoformat(),'device_key':device,'operator_key':operator,'sample_rows':len(items),'coverage':'observed_only','estimate_state':'uncalibrated','algorithm_version':'forward-union-v1','calibration_version':None,'tau':tau,'physical_erasure':'copies_pending'}
                    # Service manager views can span operators; archive each independently.
                    from .auth import Principal
                    scoped=Principal(operator,'baobao',device,'retention-local',frozenset({'activities:self'}),frozenset())
                    value['estimate']=service.timeline(scoped,start_utc=start,end_utc=end,tau=tau)['estimate']
                    conn.execute('INSERT INTO activity_daily_archive VALUES(?,?,?,?,?,?) ON CONFLICT(device_key,operator_key,day,algorithm_version,tau) DO UPDATE SET payload_json=excluded.payload_json',(device,operator,day.isoformat(),'forward-union-v1',tau,canonical_json(value).decode()))
                archived+=1
            for device,operator in {(r['device_key'],r['operator_key']) for r in rows}:
                conn.execute('INSERT INTO activity_expiry VALUES(?,?,?) ON CONFLICT(device_key,operator_key) DO UPDATE SET cutoff_utc=MAX(activity_expiry.cutoff_utc,excluded.cutoff_utc)',(device,operator,cutoff))
            deleted=conn.execute('DELETE FROM activity_event WHERE wall_utc<?',(cutoff,)).rowcount
            expired_summaries=conn.execute('DELETE FROM activity_daily_archive WHERE day<?',(summary_day.isoformat(),)).rowcount
        return {'state':'maintained','deleted_rows':deleted,'archived_days':archived,'expired_summary_rows':expired_summaries,'raw_cutoff_utc':cutoff,'summary_cutoff_day':summary_day.isoformat(),'diagnostics':'no_diagnostic_store_not_cleaned','physical_erasure':'copies_pending','immutable_events_packages_journals_backups':'not_modified','tasks_installed':False}
    def archives(self,principal,*,device_key,start_day,end_day,tau=60):
        if principal.role=='manager':principal.require('activities:read');scope='';params=[device_key]
        else:
            principal.require('activities:self')
            if principal.device_key!=device_key:raise AppError(403,'forbidden','仅能看本人设备归档')
            scope=' AND operator_key=?';params=[device_key,principal.operator_key]
        from datetime import date
        try:
            lower=date.fromisoformat(start_day);upper=date.fromisoformat(end_day)
            if lower>upper or tau not in (30,60,90):raise ValueError()
        except (ValueError,TypeError):raise AppError(422,'ACTIVITY_ARCHIVE_WINDOW_INVALID','归档日期或候选参数无效') from None
        with self.db.lock:
            rows=self.db.conn.execute('SELECT payload_json FROM activity_daily_archive WHERE device_key=?'+scope+' AND day>=? AND day<=? AND tau=? ORDER BY day LIMIT 1001',(*params,start_day,end_day,tau)).fetchall()
        if len(rows)>1000:raise AppError(413,'ACTIVITY_ARCHIVE_WINDOW_LIMIT','归档窗口超过上限')
        return [json.loads(r[0]) for r in rows]
