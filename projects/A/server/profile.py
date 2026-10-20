"""Shared typed personal fields on causal scalar registers."""
import re
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from .common import AppError
from .sync.scalar import ScalarStore

FORMAL_TAGS=('wechat','maintenance','spark','clip','vip','star','local_time')
FIELDS=('display_name','note','location','birthday','anniversary','reminder_settings')+tuple('formal_tag.'+t for t in FORMAL_TAGS)
TZDATA_VERSION='2026.5'

def pinned_timezone(name):
    import tzdata
    if tzdata.__version__!=TZDATA_VERSION:
        raise AppError(409,'TZDATA_VERSION','Pinned timezone database is required.')
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_+\-/]+',name) or '..' in name or name.startswith('/'):
        raise AppError(422,'INVALID_TIMEZONE','Explicit IANA timezone is required.')
    path=Path(tzdata.__file__).parent/'zoneinfo'/name
    try:
        with path.open('rb') as stream: return ZoneInfo.from_file(stream,key=name)
    except (OSError,ValueError): raise AppError(422,'INVALID_TIMEZONE','Explicit IANA timezone is required.')

def validate_field(field,value,clear=False):
    dynamic=re.fullmatch(r'(free_term|entitlement\.(?:vip|star))\.[A-Za-z0-9_-]{1,100}',field or '')
    if field not in FIELDS and not dynamic:
        raise AppError(422,'INVALID_PROFILE_FIELD','Unsupported profile field.')
    if clear:
        if value!='': raise AppError(422,'INVALID_CLEAR','Clear requires explicit empty text.')
        return
    if field.startswith('formal_tag.'):
        if type(value) is not bool: raise AppError(422,'INVALID_TAG','Formal tag requires a boolean.')
    elif field in ('display_name','note') or field.startswith('free_term.'):
        if not isinstance(value,str) or len(value)>10000 or '\x00' in value: raise AppError(422,'INVALID_TEXT','Bounded plain text is required.')
    elif not isinstance(value,dict): raise AppError(422,'INVALID_PROFILE_VALUE','Structured field requires an object.')
    elif field=='location':
        if set(value)!={'country','city','timezone'} or not all(isinstance(v,str) and v.strip() and len(v)<=200 for v in value.values()): raise AppError(422,'INVALID_LOCATION','Country, city and explicit timezone are required.')
        pinned_timezone(value['timezone'])
    elif field=='reminder_settings':
        if set(value)!={'vip','star','birthday','anniversary'} or not all(type(v)is int and 0<=v<=365 for v in value.values()): raise AppError(422,'INVALID_REMINDER_SETTINGS','Four independent advance days are required.')
    elif field.startswith('entitlement.'):
        if set(value)!={'start','end','source_text'} or not isinstance(value['source_text'],str): raise AppError(422,'INVALID_TERM','Paired dates and source text are required.')
        try: start,end=date.fromisoformat(value['start']),date.fromisoformat(value['end'])
        except (ValueError,TypeError): raise AppError(422,'INVALID_TERM','Paired ISO dates are required.')
        if end<start: raise AppError(422,'INVALID_TERM','End date precedes start date.')
    elif field=='birthday':
        if set(value)!={'month','day','calendar','subject','source_text'} or type(value['month'])is not int or type(value['day'])is not int or not 1<=value['month']<=12 or not 1<=value['day']<=31 or value['calendar'] not in ('gregorian','lunar','lunar_leap','unknown') or value['subject'] not in ('self','other','unknown') or not isinstance(value['source_text'],str): raise AppError(422,'INVALID_BIRTHDAY','Birthday requires calendar, subject and original text.')
    elif field=='anniversary':
        if set(value)!={'origin_date','source_text','manual'} or type(value['manual'])is not bool or not isinstance(value['source_text'],str): raise AppError(422,'INVALID_ANNIVERSARY','Anniversary requires origin and evidence.')
        try: date.fromisoformat(value['origin_date'])
        except (ValueError,TypeError): raise AppError(422,'INVALID_ANNIVERSARY','ISO origin date is required.')

def validate_scalar(payload):
    """Invoked by the shared register for imported and locally created fields."""
    field,value,operation=payload['field'],payload['value'],payload['operation']
    if field.startswith('custom_group.'):
        if not re.fullmatch(r'custom_group\.[a-f0-9]{32}',field) or operation!='set' or not isinstance(value,dict) or set(value)!={'name','deleted'} or not isinstance(value['name'],str) or not 1<=len(value['name'].strip())<=60 or type(value['deleted'])is not bool:
            raise AppError(422,'INVALID_GROUP_DEFINITION','自定义分组需要明确名称及删除状态')
        return
    if field.startswith('group.'):
        if not re.fullmatch(r'group\.[A-Za-z0-9_-]{1,100}',field) or operation not in ('add','remove') or type(value)is not bool or value!=(operation=='add'): raise AppError(422,'INVALID_GROUP','Membership requires a boolean tombstone.')
        return
    if field.startswith('opportunity_round.'):
        try: date.fromisoformat(field[len('opportunity_round.'):])
        except ValueError: raise AppError(422,'INVALID_ROUND','Dated opportunity round required.')
        if operation!='set' or value not in ('canceled','displayed'): raise AppError(422,'INVALID_ROUND','Finite immutable round state required.')
        return
    if field=='opportunity_time':
        if operation!='set' or not isinstance(value,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',value): raise AppError(422,'INVALID_OPPORTUNITY_TIME','24-hour HH:mm required.')
        return
    if operation not in ('set','clear'): raise AppError(422,'INVALID_PROFILE_OPERATION','Profile fields support set and explicit clear.')
    validate_field(field,value,operation=='clear')

class ProfileService:
    def __init__(self,db): self.db=db;self.scalars=ScalarStore(db)

    def _contact(self,account,person):
        with self.db.lock: row=self.db.conn.execute('SELECT * FROM contact WHERE account_key=? AND person_key=?',(account,person)).fetchone()
        if row is None: raise AppError(404,'CONTACT_NOT_FOUND','Stable contact is missing.')
        return dict(row)

    def get(self,account,person):
        contact=self._contact(account,person);fields=self.scalars.list_fields(account,person)
        for field in FIELDS: fields.setdefault(field,self.scalars.get(account,person,field))
        for state in fields.values(): state['value']=state['effective_value']
        return {**contact,'fields':fields}

    def set_field(self,account,person,field,value,*,expected_revision,actor,clear=False):
        self._contact(account,person);validate_field(field,value,clear)
        result=self.scalars.set(account,person,field,value,expected_revision=expected_revision,actor=actor,clear=clear)
        return {'field':field,**result,'value':result['effective_value']}

    def history(self,account,person,field=None):
        self._contact(account,person)
        sql='SELECT event_id,field,operation,value_json,event_json FROM causal_revision WHERE account_key=? AND target_key=?';params=[account,person]
        if field is not None: sql+=' AND field=?';params.append(field)
        with self.db.lock: rows=self.db.conn.execute(sql+' ORDER BY rowid',params).fetchall()
        import json
        return [{'event_id':r['event_id'],'field':r['field'],'operation':r['operation'],'value':json.loads(r['value_json']),'actor':json.loads(r['event_json']).get('actor'),'created_at_utc':json.loads(r['event_json']).get('created_at_utc')} for r in rows]

    def contacts(self,account):
        with self.db.lock: rows=[dict(r) for r in self.db.conn.execute('SELECT * FROM contact WHERE account_key=? ORDER BY person_key',(account,))]
        for row in rows:
            row['verified']=bool(row['verified']);row['display_name']=self.scalars.get(account,row['person_key'],'display_name')['effective_value'] or '未记录称呼'
        return {'items':rows}
