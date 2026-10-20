from datetime import date,datetime,timezone
from .profile import ProfileService,pinned_timezone
from .rules.dates import DEFAULT_REMINDERS,term_status,next_anniversary

class DateService:
    def __init__(self,db): self.profiles=ProfileService(db)

    def project(self,account,person,today=None,now=None,earliest_message=None,history_complete=False):
        today=today or (now or datetime.now(timezone.utc)).astimezone(pinned_timezone('Asia/Shanghai')).date()
        fields=self.profiles.get(account,person)['fields']
        values={k:v['effective_value'] for k,v in fields.items()};settings=values.get('reminder_settings') or DEFAULT_REMINDERS
        terms={kind:[] for kind in ('vip','star')}
        for field,value in values.items():
            if field.startswith('entitlement.') and isinstance(value,dict):
                kind=field.split('.')[1]
                terms[kind].append({'field':field,'source_text':value['source_text'],**term_status(date.fromisoformat(value['start']),date.fromisoformat(value['end']),today,settings[kind])})
        birthday=values.get('birthday')
        birth={'status':'unknown','remind':False}
        if isinstance(birthday,dict):
            birth=next_anniversary(birthday['month'],birthday['day'],today,birthday['calendar'],settings['birthday']) if birthday['subject']=='self' else {'status':'verify_subject','remind':False}
            birth['source_text']=birthday['source_text']
        anniversary=values.get('anniversary');ann={'status':'unknown','remind':False}
        if isinstance(anniversary,dict):
            origin=date.fromisoformat(anniversary['origin_date']);basis='manual' if anniversary['manual'] else 'recorded'
        elif earliest_message:
            origin=earliest_message['date'] if isinstance(earliest_message,dict) else earliest_message;basis='first_chat' if history_complete else 'earliest_recorded'
        else: origin=None;basis='unknown'
        if origin:
            ann=next_anniversary(origin.month,origin.day,today,advance=settings['anniversary'])
            if ann.get('next') and ann['next'].year<=origin.year: ann={'status':'not_completed_year','remind':False}
        ann['basis']=basis
        if isinstance(anniversary,dict): ann['source_text']=anniversary['source_text']
        elif isinstance(earliest_message,dict):
            ann['source_text']=earliest_message.get('body','');ann['message_key']=earliest_message.get('message_key')
        local=None
        if values.get('formal_tag.local_time') is True and isinstance(values.get('location'),dict):
            location=values['location'];local={**location,'time':(now or datetime.now(timezone.utc)).astimezone(pinned_timezone(location['timezone'])).strftime('%H:%M')}
        return {'terms':terms,'birthday':birth,'anniversary':ann,'settings':dict(settings),'local_time':local}
