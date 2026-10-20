"""Distinct verified manual contacts; contribution is a separate source window."""
from datetime import date,timedelta
from decimal import Decimal,InvalidOperation
import json
from zoneinfo import ZoneInfo
from .common import AppError
from .activity import seconds
from .local_deletion import guarded,sql_visible

def summarize(messages,*,operator_key,start_day,end_day,contributions=None,coverage_complete=False):
    try:lower=date.fromisoformat(start_day);upper=date.fromisoformat(end_day)
    except (ValueError,TypeError):raise AppError(400,'invalid_contact_window','联系窗口需要明确公历日期') from None
    if lower>upper:raise AppError(400,'invalid_contact_window','联系窗口无效')
    from datetime import datetime,timezone
    account=set();personal=set();unknown=set();automatic=set()
    for message in messages:
        if message.get('direction')!='out' or message.get('success_state')!='confirmed' or message.get('object_kind','person')!='person' or message.get('kind') in ('system','unknown'):continue
        time=message.get('occurred_lower_utc')
        if not time or message.get('time_precision') not in ('second','minute'):
            unknown.add(message.get('person_key'));continue
        day=datetime.fromtimestamp(seconds(time),timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
        endpoint=message.get('occurred_upper_utc') or time
        endpoint_day=datetime.fromtimestamp(seconds(endpoint),timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
        if endpoint_day<lower or day>upper:continue
        if endpoint_day<day or not (lower<=day and endpoint_day<=upper):
            unknown.add(message.get('person_key'));continue
        person=message['person_key'];quality=message.get('quality',{})
        manual=message.get('manual_state',quality.get('manual','manual_unknown'))
        if manual=='automated':automatic.add(person);continue
        if manual not in ('verified_manual','manual_verified','manual'):
            unknown.add(person);continue
        account.add(person)
        if message.get('actor_state')=='attributed' and message.get('actor_key')==operator_key:personal.add(person)
    values=contributions or {};known=0;missing=[];units=set();windows=set()
    for person in sorted(personal):
        fact=values.get(person)
        if not fact or fact.get('state')!='known' or fact.get('precision')!='exact':missing.append(person);continue
        units.add(fact.get('unit'))
        if fact.get('start_utc') and fact.get('end_utc'):windows.add(('utc',fact['start_utc'],fact['end_utc']))
        else:windows.add(('day',fact.get('start_day'),fact.get('end_day')))
        try:
            if type(fact.get('value')) not in (int,str):raise ValueError()
            amount=Decimal(fact['value'])
            if not amount.is_finite() or amount<0:raise ValueError()
        except (InvalidOperation,ValueError,TypeError):missing.append(person);continue
        known+=amount
    compatible=len(units)<=1 and len(windows)<=1 and all(x is not None for x in units)
    if windows:
        try:
            kind,first,last=next(iter(windows))
            compatible=compatible and (seconds(last)-seconds(first)==30*86400 if kind=='utc' else (date.fromisoformat(last)-date.fromisoformat(first)).days==29)
        except (ValueError,TypeError,AppError):compatible=False
    window=next(iter(windows)) if compatible and windows else None
    return {'contact_window':{'start_day':start_day,'end_day':end_day},'operator_key':operator_key,
            'personal_contact_keys':sorted(personal),'account_manual_contact_keys':sorted(account),'unknown_manual_keys':sorted(unknown),
            'personal_count':len(personal),'account_observed_manual_count':len(account),
            'ratio':len(personal)/len(account) if account and coverage_complete and not unknown else None,'ratio_state':'complete' if coverage_complete and not unknown else 'restricted',
            'observed_ratio':len(personal)/len(account) if account else None,
            'automatic_keys':sorted(automatic),'contribution':{'known_subtotal':int(known) if compatible and known==int(known) else str(known) if compatible else None,'unknown_keys':missing,
                'unit':next(iter(units)) if compatible and units else None,'source_window':list(window[1:]) if window else None,'source_window_kind':window[0] if window else None,
                'state':'known_subtotal' if compatible else 'incompatible_windows','causal_income_attribution':False}}

class ContactStatistics:
    """Read persisted facts only; caller-supplied attribution labels have no path here."""
    def __init__(self,db):self.db=db

    @guarded
    def read(self,account,principal,*,start_day,end_day,operator_key=None,now=None):
        from datetime import datetime,timezone
        from .rank_facts import RankFacts
        principal.require('history:read',account)
        if principal.role=='manager':principal.require('activities:read')
        else:
            principal.require('activities:self')
            if operator_key is not None and operator_key!=principal.operator_key:raise AppError(403,'forbidden','仅能查看本人的已核验联系')
        operator_key=operator_key or principal.operator_key
        # Validate the window before touching stored facts; the projection needs no body.
        summarize([],operator_key=operator_key,start_day=start_day,end_day=end_day)
        with self.db.lock:
            rows=self.db.conn.execute("SELECT m.person_key,m.direction,m.success_state,m.kind,m.occurred_lower_utc,m.occurred_upper_utc,m.time_precision,m.actor_state,m.actor_key,m.quality_json,c.kind AS object_kind FROM message m JOIN conversation c USING(conversation_key) WHERE m.account_key=? AND m.direction='out' AND c.kind='person' AND m.kind NOT IN ('system','unknown') AND "+sql_visible(self.db),(account,)).fetchall()
        messages=[]
        for row in rows:
            item=dict(row);item['quality']=json.loads(item.pop('quality_json'))
            item['manual_state']='verified_manual' if item['actor_state']=='attributed' else item['quality'].get('manual','manual_unknown')
            messages.append(item)
        current=now or datetime.now(timezone.utc)
        facts={};period_info=None
        ranks=RankFacts(self.db)
        parse=lambda text:datetime.fromisoformat(text.replace('Z','+00:00'))
        periods=[p for p in ranks.periods(account) if p['rank_kind']=='contribution' and parse(p['period_start_utc'])<=current<parse(p['period_end_utc']) and parse(p['period_end_utc'])-parse(p['period_start_utc'])==timedelta(days=30)]
        if periods:
            period=max(periods,key=lambda p:(parse(p['period_start_utc']),parse(p['effective_cutoff'])))
            source=ranks.get_active(account,'contribution',period['period_key']);period_info={k:period[k] for k in ('period_key','period_start_utc','period_end_utc','effective_cutoff','revision_id')}
            first=parse(period['period_start_utc']).astimezone(ZoneInfo('Asia/Shanghai')).date()
            last=(parse(period['period_end_utc'])-timedelta(microseconds=1)).astimezone(ZoneInfo('Asia/Shanghai')).date()
            for row in source['items']:
                facts[row['person_key']]={**row['metric'],'start_day':first.isoformat(),'end_day':last.isoformat(),'start_utc':period['period_start_utc'],'end_utc':period['period_end_utc']}
        result=summarize(messages,operator_key=operator_key,start_day=start_day,end_day=end_day,contributions=facts,coverage_complete=False)
        result['account_key']=account;result['contribution_period']=period_info
        result['coverage']='observed_only';result['causal_work_score']=None
        return result
