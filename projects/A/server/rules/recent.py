from datetime import timedelta
from fractions import Fraction
from .daily import BJ

def fast_window(collection_day): return collection_day-timedelta(days=7),collection_day-timedelta(days=1)

def aggregate_days(sessions,facts,uid,collection_day):
    begin,finish=fast_window(collection_day)
    selected=[s for s in sessions if s.get('formal') and begin<=s['start'].astimezone(BJ).date()<=finish]
    broadcast={s['start'].astimezone(BJ).date() for s in selected}
    totals={'support':0,'watch_seconds':0,'comments':0}
    active={k:set() for k in ('support','arrival','comment')}
    complete={k:True for k in active}
    duration=0
    for session in selected:
        row=facts.get((session['id'],uid),{});day=session['start'].astimezone(BJ).date()
        duration+=session.get('duration_seconds',0)
        if not session.get('ended'): complete={k:False for k in complete}
        for metric,direction in (('support','support'),('watch_seconds','arrival'),('comments','comment')):
            value=row.get(metric)
            if value is None or not row.get(metric+'_complete',False): complete[direction]=False
            if value is not None:
                totals[metric]+=value
                if value>0 and direction in ('support','comment'): active[direction].add(day)
        if row.get('arrival') is True: active['arrival'].add(day)
        elif row.get('arrival') is None: complete['arrival']=False
    return {'window':(begin,finish),'broadcast_days':len(broadcast),**totals,
            **{k+'_days':len(v) for k,v in active.items()},'complete':complete,'duration':duration,
            'arrival_ratio':Fraction(len(active['arrival']),len(broadcast)) if complete['arrival'] and broadcast else None}

def increases(prev,cur):
    labels,zero=[],[]
    for direction,value_key in (('support','support'),('arrival','arrival_days'),('comment','comment_days')):
        if not(prev['complete'].get(direction) and cur['complete'].get(direction)): continue
        before,after=prev[value_key],cur[value_key]
        if before==0:
            if after>0: zero.append(direction)
            continue
        if after>before and (direction=='support' or prev['broadcast_days'] and cur['broadcast_days'] and Fraction(after,cur['broadcast_days'])>Fraction(before,prev['broadcast_days'])): labels.append(direction)
    return {'eligible_directions':labels,'main_screen_max':min(len(labels),2),'observed_from_zero':zero}

def declines(prev,cur,identity=True,directory_complete=True,explanation=None,explanation_valid=False):
    directions=[]
    if identity and directory_complete and min(prev['broadcast_days'],cur['broadcast_days'])>=3:
        for direction,field in (('arrival','arrival_days'),('comment','comment_days')):
            if not(prev['complete'].get(direction) and cur['complete'].get(direction)): continue
            before,after=prev[field],cur[field]
            drop=Fraction(before,prev['broadcast_days'])-Fraction(after,cur['broadcast_days'])
            if before>=3 and before-after>=2 and drop>=Fraction(3,10): directions.append(direction)
        if prev['complete'].get('support') and cur['complete'].get('support') and prev['support_days']>=2 and prev['support']>0 and 2*cur['support']<=prev['support'] and cur['broadcast_days']>=prev['broadcast_days'] and cur['duration']>=prev['duration']: directions.append('support')
    observed=len(directions)>=2 and bool(set(directions)&{'arrival','comment'})
    suppressed=explanation_valid and explanation in ('pause','busy','periodic_support','opportunity_changed')
    return {'directions':directions,'observation':observed,'proactive_suggestion':observed and not suppressed}
