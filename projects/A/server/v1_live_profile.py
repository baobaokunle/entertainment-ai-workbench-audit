"""Read-only saved-contact live facts; no chat bodies, schema, or collection."""
from .profile import ProfileService
from .session_facts import SessionFacts
from .message_facts import normalize_time

METRICS=(('contribution','礼物贡献'),('watch_seconds','观看'),('comment_count','评论'),
         ('pk_contribution','PK贡献'),('attendance','出席'),('like_count','点赞'))

def unavailable_metric(label, reason='metric_not_saved'):
    return {'label':label,'state':'unknown','value':None,'unit':None,'raw_display':None,
            'precision':'unknown','coverage':'unknown','reason':reason}

class V1LiveProfile:
    def __init__(self, db):
        self.db=db;self.profiles=ProfileService(db);self.sessions=SessionFacts(db)

    def project(self, account, person, limit=10):
        if type(limit) is not int or not 1<=limit<=20:raise ValueError('finite live profile limit required')
        with self.db.lock:
            # Person qualification, active revisions and source checks belong
            # to the same read view, including on read-only connections.
            owned=not self.db.conn.in_transaction
            if owned:self.db.conn.execute('BEGIN')
            try:
                return self._project(account,person,limit)
            finally:
                if owned:self.db.conn.execute('ROLLBACK')

    def _project(self, account, person, limit):
        contact=self.profiles._contact(account,person)
        result={'format':'v1-live-profile-1','account_key':account,'person_key':person,
                'scope':'saved_contact_observed_sessions','coverage':'observed_only','complete':False,
                'sessions':[],'more_observed_sessions':False,'likes':unavailable_metric('点赞'),
                'companionship':unavailable_metric('陪伴'),'tier_from_session_metrics':False}
        if contact['object_kind']!='person' or not contact['verified']:
            result['state']='identity_unverified';return result
        observed=[]
        for payload in self.sessions.list_active(account):
            if payload.get('formal') is not True:continue
            viewers=[v for v in payload['viewers'] if v['person_key']==person]
            if len(viewers)!=1:continue
            metrics={}
            for key,label in METRICS:
                metric=viewers[0]['metrics'].get(key)
                if not metric or metric['state']!='known':metrics[key]=unavailable_metric(label,'source_metric_unknown')
                else:metrics[key]={'label':label,'state':'known','value':metric['value'],'unit':metric['unit'],
                    'raw_display':metric['raw_display'],'precision':metric['precision'],
                    'coverage':payload['metric_coverage'][key],'reason':None}
            observed.append({'session_key':payload['session_key'],'basis':'single_session',
                'started_lower_utc':payload['started_lower_utc'],'started_upper_utc':payload['started_upper_utc'],
                'ended_lower_utc':payload['ended_lower_utc'],'ended_upper_utc':payload['ended_upper_utc'],
                'captured_at_utc':payload['captured_at_utc'],'revision_id':payload['revision_id'],
                'source_id':payload['source_id'],'source_version':payload['source_version'],'metrics':metrics})
        observed.sort(key=lambda row:(normalize_time(row['captured_at_utc']),row['session_key']),reverse=True)
        result.update(state='observed' if observed else 'unknown',sessions=observed[:limit],more_observed_sessions=len(observed)>limit)
        return result
