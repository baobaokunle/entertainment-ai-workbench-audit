"""Complete canonical event semantics compatible with phase A domain_event rows."""
import json
from ..common import AppError,canonical_json
from ..db import digest,reject_floats,stable_key
from ..message_facts import normalize_time

FIELDS={'event_id','device_key','epoch','channel','seq','account_key','target_key','event_type','actor','parents','created_at_utc','payload','payload_hash','schema_version'}
EVENT_CHANNELS = {
    'message.observed':'messages', 'observation.pending':'messages', 'source.revoked':'messages',
    'action.candidate':'actions', 'send.attribution':'actions',
    'scalar.set':'profile', 'scalar.clear':'profile', 'scalar.add':'profile', 'scalar.remove':'profile',
    'relation.observation':'profile', 'spark.observation':'profile',
    'rank.revision':'collection', 'session.revision':'collection', 'session.catalog':'collection',
    'collection.config.request':'config', 'collection.config.result':'config',
    'activity.observed':'activities',
    'ai.request':'model', 'ai.result':'model', 'ai.control':'model', 'ai.preference':'model',
}

def parse_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    try:
        return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,TypeError,UnicodeError,RecursionError) as exc:
        raise AppError(422,'INVALID_SYNC_JSON','Package JSON is invalid.') from exc

def validate_event(event):
    if not isinstance(event,dict) or set(event)!=FIELDS|{'event_digest'}:
        raise AppError(422,'INVALID_EVENT','Complete registered event fields required.')
    stack=[(event,0)];nodes=0
    while stack:
        value,depth=stack.pop();nodes+=1
        if depth>32 or nodes>200000:raise AppError(413,'EVENT_STRUCTURE_LIMIT','Event structure exceeds finite limits.')
        if isinstance(value,dict):stack.extend((v,depth+1) for v in value.values())
        elif isinstance(value,list):stack.extend((v,depth+1) for v in value)
    reject_floats(event)
    for name in ('event_id','device_key','epoch','channel','account_key','target_key','event_type'):
        if not isinstance(event[name],str) or not event[name] or len(event[name])>200:
            raise AppError(422,'INVALID_EVENT','Event identity is invalid.')
    expected_channel = EVENT_CHANNELS.get(event['event_type'])
    if event['channel']=='control' or expected_channel is not None and event['channel']!=expected_channel:
        raise AppError(403,'EVENT_CHANNEL_INVALID','Domain event type does not permit this channel.')
    if type(event['seq']) is not int or event['seq']<1 or event['seq']>2**63-1 or type(event['schema_version']) is not int or event['schema_version']!=2:
        raise AppError(422,'INVALID_EVENT','Unsupported sequence or schema version.')
    parents=event['parents']
    if not isinstance(parents,list) or len(parents)>100 or any(not isinstance(x,str) or len(x)>200 for x in parents) or len(set(parents))!=len(parents) or parents!=sorted(parents):
        raise AppError(422,'INVALID_EVENT','Parents must be a bounded canonical set.')
    if event['event_id'] != stable_key('event',event['device_key'],event['epoch'],event['channel'],event['seq']) or event['event_id'] in parents:
        raise AppError(422,'INVALID_EVENT','Event identifier does not bind its channel and sequence.')
    if event['actor'] is not None and (not isinstance(event['actor'],dict) or set(event['actor'])!={'operator_key','device_key','session_id'} or any(not isinstance(x,str) or not x or len(x)>200 for x in event['actor'].values())):
        raise AppError(422,'INVALID_EVENT','Actor reference must be server-issued and complete.')
    if event['actor'] is not None and event['actor']['device_key']!=event['device_key']:raise AppError(403,'ACTOR_DEVICE_INVALID','Actor device must match the signed source device.')
    normalize_time(event['created_at_utc'])
    if not isinstance(event['payload'],dict) or event['payload_hash']!=digest(event['payload']):
        raise AppError(422,'EVENT_HASH_INVALID','Event payload digest mismatch.')
    semantics={key:event[key] for key in FIELDS}
    if event['event_digest']!=digest(semantics):
        raise AppError(422,'EVENT_HASH_INVALID','Complete event digest mismatch.')
    return event

def from_row(row):
    event=parse_json(row['event_json']);event['event_digest']=row['event_digest']
    return validate_event(event)
