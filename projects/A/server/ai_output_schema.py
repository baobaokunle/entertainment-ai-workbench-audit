"""Generation contract for the existing annotation shape; final source checks remain authoritative."""
import json
from .common import AppError

EMOTIONS=('可能疲惫','可能开心','可能难过','可能烦躁','平静','混合')
def output_schema(prompt):
    try:value=json.loads(prompt.split('\n',1)[1]);purpose=value['purpose'];context=value['source_context']
    except (ValueError,TypeError,KeyError,IndexError):raise AppError(422,'AI_OUTPUT_SCHEMA_INVALID','模型输入合约不可用') from None
    if purpose not in ('emotion','topics','reply') or not isinstance(context,dict):raise AppError(422,'AI_OUTPUT_SCHEMA_INVALID','模型输入用途不可用')
    sources=context.get('messages',[])
    if not isinstance(sources,list) or len(sources)>24:raise AppError(422,'AI_OUTPUT_SCHEMA_INVALID','模型来源超过固定范围')
    choices=[]
    for row in sources:
        if not isinstance(row,dict) or not isinstance(row.get('message_key'),str) or type(row.get('body_revision')) is not int or row['body_revision']<1 or not isinstance(row.get('body'),str) or not row['body']:continue
        # Each choice binds the complete key/revision/quote tuple, not independent enums.
        choices.append({'type':'object','properties':{'message_key':{'const':row['message_key']},'body_revision':{'const':row['body_revision']},'quote':{'const':row['body'][:48]}},'required':['message_key','body_revision','quote'],'additionalProperties':False})
    ref={'anyOf':choices} if choices else {'not':{}}
    refs={'type':'array','items':ref,'minItems':1,'maxItems':4}
    item={'type':'object','properties':{'text':{'type':'string','minLength':1,'maxLength':120},'refs':refs},'required':['text','refs'],'additionalProperties':False}
    emotion={'type':'object','properties':{'label':{'type':'string','enum':list(EMOTIONS)},'refs':refs},'required':['label','refs'],'additionalProperties':False}
    # Empty/null outcomes remain legal. No grammar forces a reference or a reply.
    return {'type':'object','properties':{'emotion':{'anyOf':[{'type':'null'},emotion]} if choices and purpose=='emotion' else {'type':'null'},
        'topics':{'type':'array','items':item,'maxItems':2 if choices and purpose=='topics' else 0},
        'replies':{'type':'array','items':item,'maxItems':2 if choices and purpose=='reply' and context.get('order_verified') is True else 0},
        'intent':{'type':'string','enum':['unknown','open','closing']}},
        'required':['emotion','topics','replies','intent'],'additionalProperties':False}

def wire_schema(prompt):
    """The engine chooses a frozen evidence slot; final storage keeps original refs."""
    result=output_schema(prompt);sources=json.loads(prompt.split('\n',1)[1])['source_context'].get('messages',[])
    choices=[]
    for index,row in enumerate(sources,1):
        if not isinstance(row,dict) or not isinstance(row.get('message_key'),str) or type(row.get('body_revision')) is not int or row['body_revision']<1 or not isinstance(row.get('body'),str) or not row['body']:continue
        choices.append({'type':'object','properties':{'source_index':{'type':'integer','const':index},'quote':{'type':'string','const':row['body'][:48]}},'required':['source_index','quote'],'additionalProperties':False})
    payload=json.loads(prompt.split('\n',1)[1])
    if payload['purpose']=='reply' and payload['source_context'].get('order_verified') is True:
        incoming=[index for index,row in enumerate(sources,1) if isinstance(row,dict) and row.get('direction')=='in']
        if incoming:choices.sort(key=lambda choice:choice['properties']['source_index']['const']!=incoming[-1])
    refs={'type':'array','minItems':1,'maxItems':4,'items':{'anyOf':choices} if choices else {'type':'object'}}
    for name in ('topics','replies'):result['properties'][name]['items']['properties']['refs']=refs
    if choices and json.loads(prompt.split('\n',1)[1])['purpose']=='emotion':result['properties']['emotion']['anyOf'][1]['properties']['refs']=refs
    return result

def decode_wire_response(prompt,raw):
    """Map only references actually chosen by the model, never fabricate a citation."""
    from .ai_annotations import parse_output
    value=parse_output(raw);sources=json.loads(prompt.split('\n',1)[1])['source_context'].get('messages',[])
    if not isinstance(value,dict) or set(value)!={'emotion','topics','replies','intent'}:raise AppError(422,'AI_STRUCTURE_INVALID','模型输出字段不可用')
    def refs(items):
        if not isinstance(items,list) or not 1<=len(items)<=4:raise AppError(422,'AI_REFERENCE_INVALID','模型引用不可用')
        converted=[];seen=set()
        for ref in items:
            if not isinstance(ref,dict) or set(ref)!={'source_index','quote'} or type(ref['source_index']) is not int or not 1<=ref['source_index']<=len(sources) or ref['source_index'] in seen or not isinstance(ref['quote'],str) or not 1<=len(ref['quote'])<=120:raise AppError(422,'AI_REFERENCE_INVALID','模型引用索引不可用')
            row=sources[ref['source_index']-1]
            if not isinstance(row,dict) or not isinstance(row.get('message_key'),str) or type(row.get('body_revision')) is not int or row['body_revision']<1 or not isinstance(row.get('body'),str) or ref['quote'] not in row['body']:raise AppError(422,'AI_REFERENCE_INVALID','模型引用并非冻结原文')
            seen.add(ref['source_index']);converted.append({'message_key':row['message_key'],'body_revision':row['body_revision'],'quote':ref['quote']})
        return converted
    if value['emotion'] is not None:
        if not isinstance(value['emotion'],dict) or set(value['emotion'])!={'label','refs'}:raise AppError(422,'AI_STRUCTURE_INVALID','情绪参考字段不可用')
        value['emotion']['refs']=refs(value['emotion']['refs'])
    for name in ('topics','replies'):
        if not isinstance(value[name],list) or len(value[name])>2:raise AppError(422,'AI_STRUCTURE_INVALID','参考列表不可用')
        for item in value[name]:
            if not isinstance(item,dict) or set(item)!={'text','refs'}:raise AppError(422,'AI_STRUCTURE_INVALID','参考字段不可用')
            item['refs']=refs(item['refs'])
    from .common import canonical_json
    return canonical_json(value).decode('utf-8')
