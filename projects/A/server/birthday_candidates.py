"""Read-only ordinary-rule suggestions. Never changes the formal birthday register."""
import json
import re
from datetime import date
from .common import AppError
from .db import digest
from .local_deletion import guarded

FORMAT='birthday-candidates-1'
DATE=re.compile(r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]')


def extract(body):
    if not isinstance(body,str) or len(body)>10000 or '生日' not in body:return []
    result=[]
    for clause in re.split(r'[。！？!?\n；;]',body):
        if '生日' not in clause:continue
        matches=list(DATE.finditer(clause))
        for match in matches[:8]:
            month,day=map(int,match.groups())
            calendar='lunar_leap' if '闰' in clause and ('农历' in clause or '阴历' in clause) else 'lunar' if '农历' in clause or '阴历' in clause else 'gregorian' if '公历' in clause or '阳历' in clause else 'unknown'
            if not 1<=month<=12 or not 1<=day<=31:continue
            flags=[]
            if calendar=='gregorian':
                try:date(2000,month,day)
                except ValueError:flags.append('invalid_date')
            elif calendar in ('lunar','lunar_leap') and day>30:flags.append('invalid_date')
            subject='self' if re.search(r'(?:^|[，,\s])(?:我|本人)的?生日',clause) else 'unknown'
            if re.search(r'(?:妈妈|爸爸|朋友|女友|男友|姐姐|妹妹|哥哥|弟弟|孩子|女儿|儿子|同事|他|她|你)(?:的)?生日',clause):subject='other'
            if len(matches)>1 or re.search(r'(?:不是|并非|不确定|好像|可能|如果|假如|听说)',clause):subject='unknown';flags.append('ambiguous_expression')
            if subject!='self':flags.append('verify_subject')
            if calendar=='unknown':flags.append('verify_calendar')
            if calendar=='lunar_leap':flags.append('verify_leap_month')
            if (month,day)==(2,29):flags.append('verify_leap_day')
            result.append({'month':month,'day':day,'calendar':calendar,'subject':subject,'flags':flags})
            if len(result)>=8:return result
    return result


class BirthdayCandidates:
    def __init__(self,app):self.app=app;self.db=app.db

    def person(self,account,person):
        if not self.app.profiles:raise AppError(503,'BIRTHDAY_NOT_INITIALIZED','资料模块尚未初始化')
        p=self.app.profiles.get(account,person)
        if not p['verified'] or p['object_kind']!='person':raise AppError(409,'BIRTHDAY_PERSON_REQUIRED','仅核对已登记个人的生日候选')

    def reliable(self,row,account,person):
        q=row.get('quality',{})
        return row['account_key']==account and row['person_key']==person and row['direction']=='in' and row['kind']=='text' and row['identity_state']=='exact' and all(q.get(k)=='verified' for k in ('identity','body','conversation','direction')) and q.get('message_identity')=='exact'

    def candidate(self,row,index,value):
        source={k:row[k] for k in ('message_key','body_sha256')};source['body_revision']=row['active_body_revision']
        return {'candidate_key':digest([FORMAT,row['account_key'],row['person_key'],source,index,value]),'source':source,'value':value,'observed_at':row.get('occurred_lower_utc'),'time_precision':row.get('time_precision')}

    @guarded
    def read(self,account,person,cursor=None):
        self.person(account,person)
        before=None
        if cursor is not None:
            try:
                if not isinstance(cursor,str) or len(cursor)>4096:raise ValueError()
                before=json.loads(cursor)
                if not isinstance(before,dict):raise ValueError()
            except (ValueError,TypeError):raise AppError(400,'INVALID_CURSOR','生日候选游标无效') from None
        page=self.app.messages.list_messages(account,person_key=person,keyword='生日',before=before,limit=20)
        items=[];skipped=0
        for row in page['items']:
            if not self.reliable(row,account,person):skipped+=1;continue
            values=extract(row['body'])
            if not values:skipped+=1
            items.extend(self.candidate(row,i,v) for i,v in enumerate(values))
        return {'format':FORMAT,'account_key':account,'person_key':person,'items':items,'scanned':len(page['items']),'skipped':skipped,'next_cursor':json.dumps(page['next_cursor'],separators=(',',':')) if page['next_cursor'] else None,'coverage':'saved_messages_only','automatic_write':False}

    @guarded
    def inspect(self,account,person,message_key,candidate_key):
        self.person(account,person)
        row=self.app.messages.get_message(message_key)
        if not row or not self.reliable(row,account,person):raise AppError(409,'BIRTHDAY_SOURCE_STALE','候选来源已变化，请重新读取')
        for index,value in enumerate(extract(row['body'])):
            candidate=self.candidate(row,index,value)
            if candidate['candidate_key']==candidate_key:
                return {'format':FORMAT,'account_key':account,'person_key':person,**candidate,'body':row['body'],'profile_value':{k:value[k] for k in ('month','day','calendar','subject')}|{'source_text':row['body']}}
        raise AppError(409,'BIRTHDAY_SOURCE_STALE','原文或正文修订已变化，请重新读取')
