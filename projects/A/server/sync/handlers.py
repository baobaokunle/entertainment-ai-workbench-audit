"""Recompute received facts against independently approved local provenance; no event echo."""
from ..auth import Principal
from ..common import AppError
from ..message_facts import MessageFacts
from ..send_evidence import SendEvidence
from ..identity import IdentityService,require_text
from ..db import stable_key

def validate_withdrawal_event(event):
    """Finite origin-bound authority reduction; no database writes."""
    p=event['payload']
    if event['event_type']!='source.revoked' or event['channel']!='messages' or set(p)!={'source_id','source_version','reason'}:
        raise AppError(422,'withdrawal_target_invalid','来源撤销必须绑定签名设备、账号及来源版本')
    for name in ('source_id','source_version','reason'):require_text(p[name],name,200)
    if event['target_key']!=stable_key('source-withdrawal',event['device_key'],event['account_key'],p['source_id'],p['source_version']):
        raise AppError(422,'withdrawal_target_invalid','来源撤销必须绑定签名设备、账号及来源版本')
    return p

def register_fact_handlers(importer,*,history=None):
    db=importer.db;messages=MessageFacts(db);actions=SendEvidence(db);identity=IdentityService(db)
    def principal(event,*,action=False):
        actor=event['actor'] or {}
        scopes={'observations:write','actions:write'}
        # Elevated provenance is assessed only using the locally approved, bound source record.
        raw=event['payload'].get('action',{})
        source=identity.get_source(raw.get('source_id'),raw.get('source_version','1')) if action else None
        # The signed sender-service classification preserves the original session
        # authorization. A locally approved source cannot elevate a signed candidate.
        if event['payload'].get('provenance')=='verified' and source and source['verified'] and source['evidence'].get('operator_key')==actor.get('operator_key') and source['evidence'].get('device_key')==event['device_key']:
            scopes.add('action_provenance:trusted')
        return Principal(actor.get('operator_key','sync:'+event['device_key']),'paired_device',event['device_key'],
                         actor.get('session_id','sync-observation'),frozenset(scopes),frozenset({event['account_key']}))
    def observe(conn,event):
        raw=event['payload'].get('observation')
        if not isinstance(raw,dict) or raw.get('account_key')!=event['account_key']:
            raise AppError(422,'event_target_mismatch','事件与原文账号不一致')
        try: result=messages.ingest(raw,principal(event),emit_event=False)
        except AppError as error:
            if error.code in ('SOURCE_NOT_REGISTERED','ACCOUNT_UNKNOWN'):raise AppError(409,'DEPENDENCY_PENDING','独立来源或账号登记待核验') from None
            raise
        target=result.get('message_key') or result['observation_key']
        if event['payload'].get('message_key') and not result.get('message_key'):
            raise AppError(409,'DEPENDENCY_PENDING','本地稳定对象或可靠来源证明待核验')
        if target!=event['target_key'] or event['payload'].get('message_key')!=result.get('message_key'):
            raise AppError(422,'event_target_mismatch','事件与独立计算对象不一致')
        if result.get('message_key'):
            message=messages.get_message(result['message_key'])
            if message['direction']=='out' and message['success_state']=='confirmed':actions.link_success(result['message_key'],emit_event=False)
            if history:history.update(result['message_key'])
    def action(conn,event):
        raw=event['payload'].get('action')
        if not isinstance(raw,dict) or raw.get('account_key')!=event['account_key'] or not event['actor']:
            raise AppError(422,'event_target_mismatch','动作缺少认证对象或操作者')
        if event['payload'].get('provenance') not in ('candidate','verified'):
            raise AppError(422,'action_provenance_missing','动作缺少来源服务签发的权限判断')
        try: result=actions.record_action(raw,principal(event,action=True),emit_event=False)
        except AppError as error:
            if error.code in ('SOURCE_NOT_REGISTERED','ACCOUNT_UNKNOWN','CONVERSATION_UNKNOWN'):raise AppError(409,'DEPENDENCY_PENDING','动作来源或会话依赖待核验') from None
            raise
        if result['action_key']!=event['target_key']:raise AppError(422,'event_target_mismatch','动作对象不一致')
        candidates=conn.execute("SELECT message_key FROM message WHERE account_key=? AND conversation_key=? AND platform_correlation_key=? AND direction='out' AND success_state='confirmed'",(raw['account_key'],raw['conversation_key'],raw.get('platform_correlation_key'))).fetchall()
        for row in candidates:actions.link_success(row[0],emit_event=False)
    def attribution(conn,event):
        key=event['payload'].get('message_key')
        if key!=event['target_key']:raise AppError(422,'event_target_mismatch','关联消息对象不一致')
        found=messages.get_message(key)
        if not found or found['account_key']!=event['account_key']:raise AppError(409,'DEPENDENCY_PENDING','成功消息依赖待核验')
        # The remote actor claim is never assigned directly.
        actions.link_success(key,emit_event=False)
    def withdrawal(conn,event):
        p=validate_withdrawal_event(event)
        if not conn.execute('SELECT 1 FROM own_account WHERE account_key=?',(event['account_key'],)).fetchone() or not identity.get_source(p['source_id'],p['source_version']):
            raise AppError(409,'DEPENDENCY_PENDING','来源撤销的独立账号或来源登记待核验')
        identity.withdraw_origin(event['device_key'],event['account_key'],p['source_id'],p['source_version'],reason=p['reason'],emit_event=False)
        if history:
            rows=conn.execute('SELECT DISTINCT message_key FROM message_observation WHERE device_key=? AND account_key=? AND source_id=? AND source_version=? AND message_key IS NOT NULL',(event['device_key'],event['account_key'],p['source_id'],p['source_version'])).fetchall()
            for row in rows:history.update(row[0])
    importer.register_handler('message.observed',observe)
    importer.register_handler('observation.pending',observe)
    importer.register_handler('action.candidate',action)
    importer.register_handler('send.attribution',attribution)
    importer.register_handler('source.revoked',withdrawal)
