"""Reliable independent follow directions; discovery never substitutes occurrence."""
import json
from .common import AppError,canonical_json,utc_now
from .db import digest
from .identity import IdentityService

RELATION_DDL='''CREATE TABLE IF NOT EXISTS relation_observation (
 account_key TEXT NOT NULL, person_key TEXT NOT NULL, direction TEXT NOT NULL,
 source_id TEXT NOT NULL, source_version TEXT NOT NULL, observation_id TEXT NOT NULL,
 version INTEGER NOT NULL, state TEXT NOT NULL, actual_at_utc TEXT, discovered_at_utc TEXT NOT NULL,
 semantic_digest TEXT NOT NULL, event_id TEXT NOT NULL,
 PRIMARY KEY(account_key,person_key,direction,source_id,observation_id));'''

def install_schema(db):
    with db.transaction() as conn: conn.execute(RELATION_DDL)

class RelationService:
    def __init__(self,db): self.db=db;self.identity=IdentityService(db)

    def _validate(self,p):
        expected={'direction','source_id','source_version','observation_id','version','state','actual_at_utc','discovered_at_utc'}
        if set(p)!=expected or p['direction'] not in ('fan_follows_me','i_follow_fan') or p['state'] not in ('known_following','known_not_following','unknown') or type(p['version'])is not int or p['version']<1 or not all(isinstance(p[k],str) and p[k] for k in ('source_id','source_version','observation_id','discovered_at_utc')): raise AppError(422,'INVALID_RELATION','Independent relation observation and monotonic source version required.')
        from datetime import datetime
        for k in ('actual_at_utc','discovered_at_utc'):
            if p[k] is not None:
                try: stamp=datetime.fromisoformat(p[k].replace('Z','+00:00'))
                except (ValueError,AttributeError): raise AppError(422,'INVALID_RELATION_TIME','Explicit UTC occurrence or discovery time required.')
                if stamp.tzinfo is None or stamp.utcoffset().total_seconds()!=0: raise AppError(422,'INVALID_RELATION_TIME','UTC time required.')

    def record(self,account,person,payload,*,actor=None):
        self._validate(payload)
        with self.db.transaction() as conn:
            previous=conn.execute('SELECT semantic_digest FROM relation_observation WHERE account_key=? AND person_key=? AND direction=? AND source_id=? AND observation_id=?',(account,person,payload['direction'],payload['source_id'],payload['observation_id'])).fetchone()
            if previous:
                if previous[0]!=digest(payload): raise AppError(409,'RELATION_ID_REUSE','Observation identity was reused.')
                return {'status':'duplicate',**self.get(account,person)}
            self._authorize(conn,account,person,payload)
            event_id=self.db.append_event(conn,account_key=account,target_key=person,event_type='relation.observation',payload=payload,channel='profile',actor=actor)
            self.apply_remote(conn,json.loads(conn.execute('SELECT event_json FROM domain_event WHERE event_id=?',(event_id,)).fetchone()[0]))
        return {'status':'accepted',**self.get(account,person)}

    def _authorize(self,conn,account,person,p):
        if not self.identity.trusted(p['source_id'],p['source_version'],'follow_relation',account): raise AppError(403,'RELATION_SOURCE_UNVERIFIED','Reliable registered relation source required.')
        if not conn.execute("SELECT 1 FROM contact WHERE account_key=? AND person_key=? AND object_kind='person' AND verified=1",(account,person)).fetchone(): raise AppError(409,'RELATION_IDENTITY_UNKNOWN','Verified personal identity required.')

    def apply_remote(self,conn,event):
        p=event['payload'];self._validate(p);account,person=event['account_key'],event['target_key'];self._authorize(conn,account,person,p)
        existing=conn.execute('SELECT semantic_digest FROM relation_observation WHERE account_key=? AND person_key=? AND direction=? AND source_id=? AND observation_id=?',(account,person,p['direction'],p['source_id'],p['observation_id'])).fetchone()
        if existing:
            if existing[0]!=digest(p): raise AppError(409,'RELATION_ID_REUSE','Observation identity was reused.')
            return
        conn.execute('INSERT INTO relation_observation VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(account,person,p['direction'],p['source_id'],p['source_version'],p['observation_id'],p['version'],p['state'],p['actual_at_utc'],p['discovered_at_utc'],digest(p),event['event_id']))

    def get(self,account,person):
        with self.db.lock: rows=[dict(r) for r in self.db.conn.execute('SELECT * FROM relation_observation WHERE account_key=? AND person_key=? ORDER BY direction,source_id,version,observation_id',(account,person))]
        states={d:'unknown' for d in ('fan_follows_me','i_follow_fan')};changes=[]
        for direction in states:
            directional=[r for r in rows if r['direction']==direction and self.identity.trusted(r['source_id'],r['source_version'],'follow_relation',account)];latest={}
            for row in directional:
                source=(row['source_id'],row['source_version']);old=latest.get(source)
                if old and old['version']==row['version'] and old['state']!=row['state']: latest[source]={**row,'state':'unknown'}
                elif old is None or row['version']>old['version']:
                    if old and old['state']=='known_following' and row['state']=='known_not_following': changes.append({'direction':direction,'actual_at_utc':row['actual_at_utc'],'discovered_at_utc':row['discovered_at_utc'],'event_id':row['event_id']})
                    latest[source]=row
            values={r['state'] for r in latest.values()}
            if len(values)==1: states[direction]=values.pop()
        mutual=None if any(v=='unknown' for v in states.values()) else all(v=='known_following' for v in states.values())
        return {'directions':states,'mutual':mutual,'changes':changes}

    def register_handlers(self,importer): importer.register_handler('relation.observation',self.apply_remote)
