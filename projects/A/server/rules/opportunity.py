from datetime import datetime,timedelta
from .daily import BJ

def recent_sessions(sessions,now):
    first=datetime.combine(now.astimezone(BJ).date()-timedelta(days=6),datetime.min.time(),BJ)
    candidates=(s for s in sessions if s.get('formal') and s.get('ended') and s.get('end') is not None and first<=s['end']<=now)
    return sorted(candidates,key=lambda s:(s['end'],s['id']),reverse=True)[:3]

def opportunity(uid,sessions,facts,now,directory_complete=True):
    if not directory_complete: return {'selected':[],'reasons':[],'status':'sample_unknown'}
    selected=recent_sessions(sessions,now)
    rows=[facts.get((s['id'],uid),{}) for s in selected]
    reasons=[]
    if sum(r.get('watch_seconds') is not None and r['watch_seconds']>=600 for r in rows)>=2: reasons.append('watch')
    if sum(r.get('comments') is not None and r['comments']>0 for r in rows)>=2: reasons.append('comment')
    if any(r.get('pk_verified') and r.get('pk_value',0)>0 for r in rows): reasons.append('pk')
    incomplete=any(any(r.get(k) is None for k in ('watch_seconds','comments')) for r in rows)
    return {'selected':[s['id'] for s in selected],'reasons':reasons,'status':'eligible' if reasons else 'insufficient' if incomplete else 'not_eligible'}

def opportunity_round(day,canceled_day=None,displayed_day=None,change_time=False):
    state='canceled' if canceled_day==day else 'displayed' if displayed_day==day else 'planned'
    return {'state':state,'time_change_effect':('today' if state=='planned' else 'tomorrow') if change_time else 'unchanged'}
