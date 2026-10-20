"""Bounded read-only annotations and causal human controls on the model channel."""
import json
import re
from .common import AppError,canonical_json,utc_now
from .db import digest
from .local_deletion import guarded,reject_hidden

DDL = (
'''CREATE TABLE IF NOT EXISTS ai_job(job_key TEXT PRIMARY KEY,account_key TEXT NOT NULL,person_key TEXT NOT NULL,
 purpose TEXT NOT NULL,state TEXT NOT NULL,context_digest TEXT NOT NULL,context_json TEXT NOT NULL,
 model_digest TEXT NOT NULL,prompt_version TEXT NOT NULL,input_tokens INTEGER NOT NULL,
 result_json TEXT,error_code TEXT,request_event_id TEXT NOT NULL,created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL)''',
'''CREATE TABLE IF NOT EXISTS ai_control(event_id TEXT PRIMARY KEY,job_key TEXT NOT NULL,
 operation TEXT NOT NULL,value_json TEXT NOT NULL,parents_json TEXT NOT NULL)''',
'''CREATE TABLE IF NOT EXISTS ai_preference(event_id TEXT PRIMARY KEY,account_key TEXT NOT NULL,person_key TEXT NOT NULL,
 purpose TEXT NOT NULL,enabled INTEGER NOT NULL,parents_json TEXT NOT NULL)''',
'''CREATE INDEX IF NOT EXISTS ai_job_scope ON ai_job(account_key,person_key,purpose,created_at_utc)''',
'''CREATE TABLE IF NOT EXISTS ai_execution(job_key TEXT PRIMARY KEY,attempts INTEGER NOT NULL,generation INTEGER NOT NULL)''')

def install_schema(db):
    with db.transaction() as conn:
        for sql in DDL: conn.execute(sql)

def require_schema(db):
    with db.lock:
        names={row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'ai_job','ai_control','ai_preference','ai_execution'}.issubset(names): raise AppError(409,'AI_MIGRATION_REQUIRED','Explicit AI schema installation is required.')

def parse_output(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError()
            result[key]=value
        return result
    try:
        return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError())) if isinstance(raw,str) else raw
    except (ValueError,TypeError,RecursionError): raise AppError(422,'AI_JSON_INVALID','Model output JSON is invalid.') from None

def validate_result(raw,context,purpose):
    result=parse_output(raw)
    if not isinstance(result,dict) or set(result)!={'emotion','topics','replies','intent'} or result['intent'] not in ('unknown','open','closing'):
        raise AppError(422,'AI_STRUCTURE_INVALID','Read-only annotation structure is invalid.')
    messages=context.get('messages',[]);sources={(m['message_key'],m['body_revision']):m for m in messages}
    def refs(value):
        if not isinstance(value,list) or not 1<=len(value)<=4: raise AppError(422,'AI_REFERENCE_INVALID','Bounded exact source references are required.')
        seen=set()
        for reference in value:
            if not isinstance(reference,dict) or set(reference)!={'message_key','body_revision','quote'} or not isinstance(reference['message_key'],str) or type(reference['body_revision'])is not int:
                raise AppError(422,'AI_REFERENCE_INVALID','Exact message and body revision reference required.')
            key=(reference['message_key'],reference['body_revision']);source=sources.get(key)
            if source is None or key in seen or not isinstance(reference['quote'],str) or not 1<=len(reference['quote'])<=120 or reference['quote'] not in source['body']:
                raise AppError(422,'AI_REFERENCE_INVALID','Reference must quote supplied same-person current-version source.')
            seen.add(key)
    emotion=result['emotion']
    if emotion is not None:
        if not isinstance(emotion,dict) or set(emotion)!={'label','refs'} or emotion['label'] not in ('可能疲惫','可能开心','可能难过','可能烦躁','平静','混合'):
            raise AppError(422,'AI_EMOTION_INVALID','At most one bounded tentative emotion is allowed.')
        refs(emotion['refs'])
    for field in ('topics','replies'):
        items=result[field]
        if not isinstance(items,list) or len(items)>2: raise AppError(422,'AI_STRUCTURE_INVALID','At most two topics or short references are allowed.')
        if field=='replies' and purpose!='reply' and items: raise AppError(422,'AI_REPLY_NOT_REQUESTED','Short reply references require an explicit request.')
        for item in items:
            if not isinstance(item,dict) or set(item)!={'text','refs'} or not isinstance(item['text'],str) or not 1<=len(item['text'])<=120 or '\x00' in item['text']:
                raise AppError(422,'AI_TEXT_INVALID','Bounded plain read-only text required.')
            refs(item['refs'])
    incoming=[m for m in messages if m.get('direction')=='in']
    closing=bool(context.get('order_verified') and incoming and re.search(r'我先睡|我睡了|先休息|晚安|先不聊|不聊了',incoming[-1]['body']))
    if closing and result['replies']:
        key=incoming[-1]['message_key']
        if result['intent']!='closing' or any(len(item['text'])>32 or re.search(r'[?？]',item['text']) or not any(ref['message_key']==key for ref in item['refs']) for item in result['replies']):
            raise AppError(422,'AI_LATEST_INTENT_INVALID','Latest closing intent requires a short cited ending, without a question.')
    if purpose=='reply' and not context.get('order_verified') and result['replies']:
        raise AppError(422,'AI_ORDER_UNKNOWN','Unknown chronology cannot support a latest-intent reply.')
    if result['replies']:
        latest=(incoming[-1]['message_key'],incoming[-1]['body_revision']) if incoming else None
        if latest is None or any(not any((ref['message_key'],ref['body_revision'])==latest for ref in item['refs']) for item in result['replies']):
            raise AppError(422,'AI_LATEST_REFERENCE_REQUIRED','Each reply must cite the exact latest incoming source; older or outgoing text cannot replace it.')
    return result

def tips(rows):
    by_id={row['event_id']:row for row in rows};superseded={parent for row in rows for parent in json.loads(row['parents_json'])}
    return [by_id[key] for key in sorted(set(by_id)-superseded)]

class AnnotationStore:
    def __init__(self,db): require_schema(db); self.db=db
    @guarded
    def raw_job(self,job_key,conn=None):
        if conn is not None: row=conn.execute('SELECT * FROM ai_job WHERE job_key=?',(job_key,)).fetchone()
        else:
            with self.db.lock: row=self.db.conn.execute('SELECT * FROM ai_job WHERE job_key=?',(job_key,)).fetchone()
        if not row: raise AppError(404,'AI_JOB_UNKNOWN','Annotation job is missing.')
        reject_hidden(self.db,row['account_key'],job_key=job_key)
        return dict(row)
    @guarded
    def read(self,job_key,conn=None):
        if conn is None:
            with self.db.lock: return self.read(job_key,self.db.conn)
        job=self.raw_job(job_key,conn);controls=[dict(row) for row in conn.execute('SELECT * FROM ai_control WHERE job_key=?',(job_key,))];heads=tips(controls)
        execution=conn.execute('SELECT attempts,generation FROM ai_execution WHERE job_key=?',(job_key,)).fetchone()
        result=json.loads(job['result_json']) if job['result_json'] else None;state=job['state']
        if any(row['operation']=='cancel' for row in controls): state='canceled';result=None
        elif any(row['operation']=='hide' for row in controls): state='hidden';result=None
        elif heads:
            values={row['value_json'] for row in heads}
            if len(values)>1: state='correction_conflict';result=None
            else: state='corrected';result=json.loads(heads[0]['value_json'])
        # Cached derived input must remain valid even through this low-level read.
        from .derived_import import check_message
        try:
            for source in json.loads(job['context_json']).get('messages',[]):
                row=conn.execute('SELECT * FROM message WHERE message_key=?',(source['message_key'],)).fetchone()
                if row is None:raise AppError(409,'AI_SOURCE_STALE','Source message is missing.')
                check_message(self.db,row)
        except AppError:
            state='stale';result=None;heads=[]
        return {'job_key':job_key,'account_key':job['account_key'],'person_key':job['person_key'],'purpose':job['purpose'],
                'state':state,'result':result,'error_code':job['error_code'],'revision':[r['event_id'] for r in heads],
                'context_digest':job['context_digest'],'model_digest':job['model_digest'],'prompt_version':job['prompt_version'],
                'execution':{'attempts':execution['attempts'] if execution else 0,'generation':execution['generation'] if execution else 0,'max_attempts':3},
                'created_at_utc':job['created_at_utc'],'updated_at_utc':job['updated_at_utc'],
                'context_quality':{key:json.loads(job['context_json']).get(key) for key in ('dropped_messages','skipped_unreliable','order_verified')},
                'correction_candidates':[json.loads(r['value_json']) for r in heads] if state=='correction_conflict' else []}
    @guarded
    def validate_context_current(self,context,conn=None):
        if conn is None:
            with self.db.lock: return self.validate_context_current(context,self.db.conn)
        if not isinstance(context,dict) or set(context)!={'account_key','person_key','messages','dropped_messages','skipped_unreliable','order_verified','source_generation'} or not isinstance(context['source_generation'],str) or not isinstance(context.get('messages'),list) or len(context['messages'])>24 or type(context['order_verified'])is not bool or any(type(context[k])is not int or context[k]<0 for k in ('dropped_messages','skipped_unreliable')):
            raise AppError(422,'AI_CONTEXT_INVALID','Bounded source context required.')
        seen=set()
        for source in context['messages']:
            if not isinstance(source,dict) or set(source)!={'message_key','body_revision','body_sha256','body','direction','occurred_lower_utc','occurred_upper_utc','time_precision','time_verified'} or not isinstance(source['message_key'],str) or type(source['body_revision'])is not int or source['body_revision']<1 or not isinstance(source['body_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',source['body_sha256']) or not isinstance(source['body'],str) or type(source['time_verified'])is not bool or source['message_key'] in seen:raise AppError(422,'AI_CONTEXT_INVALID','Exact bounded source fields required.')
            seen.add(source['message_key'])
            reject_hidden(self.db,context.get('account_key'),message_key=source['message_key'])
            row=conn.execute('SELECT m.*,r.body,r.body_sha256 FROM message m JOIN message_revision r ON r.message_key=m.message_key AND r.revision=m.active_body_revision WHERE m.message_key=?',(source['message_key'],)).fetchone()
            if row is None: raise AppError(409,'DEPENDENCY_PENDING','Source messages must be applied before annotations.')
            from .derived_import import check_message
            check_message(self.db,row)
            quality=json.loads(row['quality_json'])
            if row['account_key']!=context.get('account_key') or row['person_key']!=context.get('person_key') or row['active_body_revision']!=source.get('body_revision') or row['body_sha256']!=source.get('body_sha256') or row['body']!=source.get('body') or quality.get('identity')!='verified' or quality.get('body')!='verified' or quality.get('direction')!='verified' or row['kind']!='text' or row['identity_state']!='exact' or any(row[k]!=source[k] for k in ('direction','occurred_lower_utc','occurred_upper_utc','time_precision')) or source['time_verified']!=(quality.get('time')=='verified'):
                raise AppError(409,'AI_SOURCE_STALE','Source identity, body or revision changed.')
        messages=context['messages'];times=[m['occurred_lower_utc'] for m in messages]
        ordered=bool(messages) and all(m['time_verified'] and m['time_precision']=='second' and m['occurred_lower_utc'] and m['occurred_lower_utc']==m['occurred_upper_utc'] for m in messages) and len(set(times))==len(times) and times==sorted(times)
        if context['order_verified']!=bool(ordered):raise AppError(422,'AI_ORDER_INVALID','Chronology must bind verified exact source times.')
        return True
    @guarded
    def preference(self,account,person,purpose,conn=None):
        if conn is None:
            with self.db.lock: return self.preference(account,person,purpose,self.db.conn)
        rows=[dict(r) for r in conn.execute('SELECT * FROM ai_preference WHERE account_key=? AND person_key=? AND purpose=?',(account,person,purpose))];heads=tips(rows)
        return {'enabled':all(row['enabled']==1 for row in heads),'revision':[r['event_id'] for r in heads]}
    @guarded
    def apply_control(self,conn,event):
        p=event['payload']
        if event['channel']!='model' or not isinstance(p,dict) or set(p)!={'job_key','operation','value'} or not isinstance(p['job_key'],str) or p['operation'] not in ('cancel','hide','correct'): raise AppError(422,'AI_CONTROL_INVALID','Finite model control required.')
        try: job=self.raw_job(p['job_key'],conn)
        except AppError as e:
            if e.status==404: raise AppError(409,'DEPENDENCY_PENDING','Annotation request must precede controls.') from None
            raise
        if (event['account_key'],event['target_key'])!=(job['account_key'],job['person_key']): raise AppError(403,'AI_SCOPE_INVALID','Annotation belongs to another account or person.')
        if not event.get('actor'): raise AppError(403,'AI_ACTOR_REQUIRED','Human annotation control requires authenticated actor provenance.')
        if p['operation']=='correct': validate_result(p['value'],json.loads(job['context_json']),job['purpose'])
        elif p['value'] is not None: raise AppError(422,'AI_CONTROL_INVALID','Hide and cancel carry no replacement content.')
        allowed={r[0] for r in conn.execute('SELECT event_id FROM ai_control WHERE job_key=?',(p['job_key'],))}|{job['request_event_id']}
        if any(parent not in allowed for parent in event['parents']): raise AppError(409,'AI_CONTROL_PARENT_INVALID','Controls must bind this annotation causal versions.')
        conn.execute('INSERT OR IGNORE INTO ai_control VALUES(?,?,?,?,?)',(event['event_id'],p['job_key'],p['operation'],canonical_json(p['value']).decode(),canonical_json(event['parents']).decode()))
        if p['operation'] in ('hide','cancel'): conn.execute('UPDATE ai_job SET state=?,updated_at_utc=? WHERE job_key=?',('hidden' if p['operation']=='hide' else 'canceled',utc_now(),p['job_key']))
        else:conn.execute("UPDATE ai_job SET state='corrected',updated_at_utc=? WHERE job_key=?",(utc_now(),p['job_key']))
    @guarded
    def apply_preference(self,conn,event):
        p=event['payload']
        if event['channel']!='model' or not isinstance(p,dict) or set(p)!={'purpose','enabled'} or p['purpose'] not in ('emotion','topics','reply') or type(p['enabled'])is not bool or not event.get('actor'): raise AppError(422,'AI_PREFERENCE_INVALID','Finite authenticated annotation preference required.')
        if not conn.execute("SELECT 1 FROM contact WHERE account_key=? AND person_key=? AND object_kind='person' AND verified=1",(event['account_key'],event['target_key'])).fetchone():raise AppError(409,'DEPENDENCY_PENDING','Verified personal object must precede preferences.')
        allowed={r[0] for r in conn.execute('SELECT event_id FROM ai_preference WHERE account_key=? AND person_key=? AND purpose=?',(event['account_key'],event['target_key'],p['purpose']))}
        if any(parent not in allowed for parent in event['parents']): raise AppError(409,'AI_PREFERENCE_PARENT_INVALID','Preference parents must bind this account, person and purpose.')
        conn.execute('INSERT OR IGNORE INTO ai_preference VALUES(?,?,?,?,?,?)',(event['event_id'],event['account_key'],event['target_key'],p['purpose'],int(p['enabled']),canonical_json(event['parents']).decode()))
        if not p['enabled']: conn.execute("UPDATE ai_job SET state='disabled',updated_at_utc=? WHERE account_key=? AND person_key=? AND purpose=? AND state IN ('queued','running')",(utc_now(),event['account_key'],event['target_key'],p['purpose']))
