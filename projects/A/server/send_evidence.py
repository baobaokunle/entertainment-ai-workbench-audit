"""An action is only a candidate. Completion is never written by this module."""
import json
from .common import AppError, canonical_json, utc_now
from .db import stable_key, digest, reject_floats
from .identity import IdentityService, require_text
from .message_facts import normalize_time
from .local_deletion import guarded,sql_visible,reject_hidden,is_hidden_action


class SendEvidence:
    def __init__(self,db):
        self.db=db
        self.identities=IdentityService(db)

    @guarded
    def record_action(self,action,principal,*,emit_event=True):
        if principal is None or not isinstance(action,dict):
            raise AppError(401,'ACTION_AUTH_REQUIRED','Authenticated action provenance is required.')
        if {'actor','actor_key','operator_key','device_key','session_id','role','completed','completion'}.intersection(action):
            raise AppError(400,'CLIENT_AUTHORITY_REJECTED','Action cannot nominate actor or completion authority.')
        reject_floats(action)
        for field in ('action_id','source_id','account_key','conversation_key','action_kind'):
            require_text(action.get(field),field,200)
        principal.require('actions:write',action['account_key'])
        source_id=action['source_id']; version=action.get('source_version','1')
        source=self.identities.get_source(source_id,version)
        if not source:
            raise AppError(409,'SOURCE_NOT_REGISTERED','Action source must be registered.')
        if self.identities.origin_withdrawn(principal.device_key,action['account_key'],source_id,version):
            raise AppError(422,'SOURCE_WITHDRAWN','This origin withdrew the action source version.')
        correlation=action.get('platform_correlation_key')
        if correlation is not None: require_text(correlation,'platform_correlation_key')
        lower=normalize_time(action.get('occurred_lower_utc'));upper=normalize_time(action.get('occurred_upper_utc'))
        if (lower is None)!=(upper is None) or (lower and lower>upper):
            raise AppError(400,'INVALID_TIME_INTERVAL','Action occurrence interval is invalid.')
        evidence=source['evidence']
        # Elevated collector scope is server assigned; no request boolean can grant it.
        trusted=bool('action_provenance:trusted' in principal.scopes and action['action_kind']=='native_send'
                     and source['source_kind'] in ('fictional','trusted_local_action')
                     and evidence.get('device_key')==principal.device_key and evidence.get('operator_key')==principal.operator_key
                     and all(self.identities.trusted(source_id,version,cap,action['account_key']) for cap in ('local_action','native_manual_action','platform_correlation'))
                     and action.get('automation_state')=='verified_manual' and correlation and lower and upper)
        semantic=digest({'action':action,'operator_key':principal.operator_key,'device_key':principal.device_key,'session_id':principal.session_id})
        key=stable_key('action',source_id,version,action['action_id']);now=utc_now()
        with self.db.transaction() as conn:
            if self.identities.origin_withdrawn(principal.device_key,action['account_key'],source_id,version):
                raise AppError(422,'SOURCE_WITHDRAWN','This origin withdrew the action source version.')
            trusted=trusted and self.identities.get_source(source_id,version)['verified']
            old=conn.execute('SELECT semantic_digest,trusted FROM manual_action WHERE action_key=?',(key,)).fetchone()
            if old and old[0]!=semantic:
                raise AppError(409,'ACTION_ID_REUSED','Action identifier has different semantics or actor provenance.')
            if is_hidden_action(self.db,action['account_key'],key):
                raise AppError(410,'LOCAL_DELETION_SUPPRESSED','This exact action is locally suppressed.')
            if old:
                if old[0]!=semantic:
                    raise AppError(409,'ACTION_ID_REUSED','Action identifier has different semantics or actor provenance.')
                return {'status':'duplicate','action_key':key,'provenance':'verified' if old[1] else 'candidate','committed':True}
            target=conn.execute('SELECT 1 FROM conversation WHERE account_key=? AND conversation_key=?',(action['account_key'],action['conversation_key'])).fetchone()
            if not target:
                raise AppError(409,'CONVERSATION_UNKNOWN','Action target must be a registered conversation of this account.')
            conn.execute('INSERT INTO manual_action VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,action['action_id'],action['account_key'],action['conversation_key'],source_id,version,principal.operator_key,principal.device_key,principal.session_id,action['action_kind'],correlation,int(trusted),semantic,canonical_json(action).decode(),now))
            if emit_event:
                self.db.append_event(conn,account_key=action['account_key'],target_key=key,event_type='action.candidate',channel='actions',payload={'action_key':key,'action':action,'provenance':'verified' if trusted else 'candidate'},actor={'operator_key':principal.operator_key,'device_key':principal.device_key,'session_id':principal.session_id})
            if trusted:
                links=conn.execute("SELECT m.message_key FROM message m WHERE m.account_key=? AND m.conversation_key=? AND m.platform_correlation_key=? AND m.actor_state='attributed' AND "+sql_visible(self.db),(action['account_key'],action['conversation_key'],correlation)).fetchall()
                for row in links:self.link_success(row[0],emit_event=emit_event)
        return {'status':'accepted','action_key':key,'provenance':'verified' if trusted else 'candidate','committed':True}

    @guarded
    def link_success(self,message_key,*,emit_event=True):
        with self.db.transaction() as conn:
            message=conn.execute('SELECT * FROM message WHERE message_key=?',(message_key,)).fetchone()
            if not message:
                raise AppError(404,'MESSAGE_NOT_FOUND','Message was not found.')
            reject_hidden(self.db,message['account_key'],message_key=message_key)
            quality=json.loads(message['quality_json'])
            eligible=bool(message['direction']=='out' and message['success_state']=='confirmed'
                          and message['identity_state']=='exact' and message['platform_correlation_key']
                          and message['time_precision']=='second' and message['occurred_lower_utc']==message['occurred_upper_utc']
                          and message['occurred_lower_utc'] and quality.get('body')=='verified'
                          and quality.get('direction')=='verified' and quality.get('success')=='verified'
                          and quality.get('time')=='verified' and 'conflict' not in quality.values())
            candidates=[]
            if eligible:
                rows=conn.execute('SELECT * FROM manual_action WHERE account_key=? AND conversation_key=? AND platform_correlation_key=? AND trusted=1',(message['account_key'],message['conversation_key'],message['platform_correlation_key'])).fetchall()
                for action in rows:
                    if is_hidden_action(self.db,message['account_key'],action['action_key']):continue
                    raw=json.loads(action['payload_json'])
                    lower=normalize_time(raw.get('occurred_lower_utc'));upper=normalize_time(raw.get('occurred_upper_utc'))
                    # Platform correlation establishes match; time must be independently consistent, never the sole proof.
                    if lower and upper and lower==upper and lower<=message['occurred_upper_utc'] and upper>=message['occurred_lower_utc']:
                        candidates.append(action)
            action=candidates[0] if len(candidates)==1 else None
            if action:
                reused=conn.execute('SELECT message_key FROM send_link WHERE action_key=? AND message_key<>?',(action['action_key'],message_key)).fetchone()
                if reused: action=None
            state='attributed' if action else 'actor_unknown'
            operator=action['operator_key'] if action else None;action_key=action['action_key'] if action else None
            old=conn.execute('SELECT * FROM send_link WHERE message_key=?',(message_key,)).fetchone()
            changed=not old or (old['link_state'],old['action_key'],old['operator_key'])!=(state,action_key,operator)
            conn.execute('INSERT INTO send_link VALUES (?,?,?,?,?) ON CONFLICT(message_key) DO UPDATE SET action_key=excluded.action_key,link_state=excluded.link_state,operator_key=excluded.operator_key,linked_at_utc=excluded.linked_at_utc',(message_key,action_key,state,operator,utc_now()))
            conn.execute('UPDATE message SET actor_state=?,actor_key=? WHERE message_key=?',(state,operator,message_key))
            if changed and emit_event:
                self.db.append_event(conn,account_key=message['account_key'],target_key=message_key,event_type='send.attribution',channel='actions',payload={'message_key':message_key,'action_key':action_key,'actor_state':state,'operator_key':operator})
            return {'message_key':message_key,'actor_state':state,'operator_key':operator,'action_key':action_key,'account_success':message['direction']=='out' and message['success_state']=='confirmed','manual_state':'verified_manual' if action else 'manual_unknown'}
