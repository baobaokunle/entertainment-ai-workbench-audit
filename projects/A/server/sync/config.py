"""Versioned finite manager requests; designated baobao service accepts/rejects automatically."""
import json,uuid
from ..common import AppError,canonical_json
from ..db import digest
from ..collection import actor_ref
from .schema import require_schema

class ConfigRequests:
    FIELDS={'request_id','task_key','expected_version','enabled','hour','minute'}
    def __init__(self,db,collection):require_schema(db);self.db=db;self.collection=collection

    def _validate(self,payload):
        if not isinstance(payload,dict) or set(payload)!=self.FIELDS or not isinstance(payload['request_id'],str) or not payload['request_id'] or len(payload['request_id'])>200 or payload['task_key']!=self.collection.task_key or type(payload['expected_version']) is not int or payload['expected_version']<1 or type(payload['enabled']) is not bool or type(payload['hour']) is not int or not 0<=payload['hour']<=23 or type(payload['minute']) is not int or not 0<=payload['minute']<=59:
            raise AppError(422,'CONFIG_REQUEST_INVALID','Only versioned time and enabled requests are permitted.')

    def request(self,account,payload,principal):
        principal.require('collection:request',account)
        if principal.role!='manager':raise AppError(403,'MANAGER_ONLY','Only the authenticated manager requests plan changes.')
        self._validate(payload)
        with self.db.transaction() as conn:
            old=conn.execute('SELECT digest,state FROM config_request WHERE request_id=?',(payload['request_id'],)).fetchone()
            if old:
                if old[0]!=digest({'account':account,'payload':payload}):raise AppError(409,'CONFIG_ID_REUSED','Request ID binds account and complete request.')
                return {'status':'duplicate','state':old['state'],'request_id':payload['request_id']}
            known=conn.execute('SELECT max(result_version) FROM config_request WHERE account_key=?',(account,)).fetchone()[0]
            local=conn.execute('SELECT version FROM collection_config WHERE task_key=?',(self.collection.task_key,)).fetchone()
            known=max(known or 1,local[0] if local else 1)
            if payload['expected_version']!=known:raise AppError(409,'STALE_CONFIG','Known collection configuration version has changed.')
            event_id=self.db.append_event(conn,account_key=account,target_key=self.collection.task_key,event_type='collection.config.request',payload=payload,channel='config',actor=actor_ref(principal))
            self._store(conn,account,payload,'pending')
            return {'status':'pending','request_id':payload['request_id'],'event_id':event_id}

    def _store(self,conn,account,payload,state):
        hashed=digest({'account':account,'payload':payload});old=conn.execute('SELECT digest FROM config_request WHERE request_id=?',(payload['request_id'],)).fetchone()
        if old:
            if old[0]!=hashed:raise AppError(409,'CONFIG_ID_REUSED','Request identifier binds complete semantics.')
            return
        conn.execute('INSERT INTO config_request VALUES(?,?,?,?,?,?,NULL,NULL)',(payload['request_id'],account,payload['expected_version'],canonical_json(payload).decode(),hashed,state))

    def apply_request(self,conn,event):
        self._validate(event['payload'])
        if event['target_key']!=self.collection.task_key:raise AppError(422,'CONFIG_TARGET_INVALID','Plan target mismatch.')
        self._store(conn,event['account_key'],event['payload'],'received')

    def process_pending(self,principal):
        """Worker hook after imported transaction commits. Responses are new local events."""
        self.collection._authorize(principal);principal.require('collection:configure')
        results=[]
        with self.db.transaction() as conn:
            for row in conn.execute("SELECT * FROM config_request WHERE state='received' ORDER BY request_id").fetchall():
                payload=json.loads(row['request_json']);config=self.collection.config()
                state='accepted' if payload['expected_version']==config['version'] else 'rejected';reason=None if state=='accepted' else 'STALE_CONFIG'
                if state=='accepted':
                    conn.execute('UPDATE collection_config SET version=version+1,enabled=?,hour=?,minute=? WHERE task_key=?',(int(payload['enabled']),payload['hour'],payload['minute'],payload['task_key']))
                result={'request_id':row['request_id'],'request_digest':row['digest'],'state':state,'reason':reason,'version':self.collection.config()['version']}
                conn.execute('UPDATE config_request SET state=?,reason=?,result_version=? WHERE request_id=?',(state,reason,result['version'],row['request_id']))
                self.db.append_event(conn,account_key=row['account_key'],target_key=self.collection.task_key,event_type='collection.config.result',payload=result,channel='config',actor=actor_ref(principal))
                results.append(result)
        return results

    def apply_result(self,conn,event):
        p=event['payload']
        if set(p)!={'request_id','request_digest','state','reason','version'} or p['state'] not in ('accepted','rejected') or type(p['version']) is not int or p['version']<1 or event['device_key']!=self.collection.owner_device or event['target_key']!=self.collection.task_key:raise AppError(422,'CONFIG_RESULT_INVALID','Only designated baobao configuration results are authoritative.')
        row=conn.execute('SELECT account_key,digest FROM config_request WHERE request_id=?',(p['request_id'],)).fetchone()
        if not row or row['account_key']!=event['account_key'] or row['digest']!=p['request_digest']:raise AppError(409,'CONFIG_RESULT_UNBOUND','Configuration response does not match the request.')
        conn.execute('UPDATE config_request SET state=?,reason=?,result_version=? WHERE request_id=?',(p['state'],p['reason'],p['version'],p['request_id']))

    def status(self):
        with self.db.lock:return [dict(r) for r in self.db.conn.execute('SELECT request_id,state,reason,result_version FROM config_request ORDER BY request_id')]

    def register_handlers(self,importer):
        importer.register_handler('collection.config.request',self.apply_request);importer.register_handler('collection.config.result',self.apply_result)
