from datetime import datetime, timedelta, timezone

def reply_segments(events):
    pending,closed,last_exchange,unknown_time = None,[],None,False
    timed=[]
    for event in events:
        if event.get('kind','personal')!='personal': continue
        at=event.get('at')
        if at is None or at.tzinfo is None:
            unknown_time=True
        elif event.get('identity_verified',True):
            timed.append(event)
    by_time={}
    for event in timed: by_time.setdefault(event['at'],[]).append(event)
    ambiguous={stamp for stamp,rows in by_time.items() if {'in','out'}<={r.get('direction') for r in rows} and not all(r.get('order_verified') is True and type(r.get('platform_order'))is int for r in rows)}
    if ambiguous: unknown_time=True
    for event in sorted(timed,key=lambda e:(e['at'],e.get('platform_order',0),e.get('msgid',''))):
        direction,stamp=event.get('direction'),event['at']
        if direction=='out' and (not event.get('success',False) or event.get('reply_eligible') is False or stamp in ambiguous):
            if event.get('success',False) and event.get('reply_eligible') is False: unknown_time=True
            continue
        if direction=='in':
            if pending is None:
                pending={'start':stamp,'new_session':last_exchange is not None and stamp-last_exchange>=timedelta(seconds=1800),'count':1}
            else: pending['count']+=1
        elif direction=='out' and pending is not None:
            closed.append({**pending,'out_at':stamp,'valid_first_response':event.get('valid_first_response','unverified')})
            pending=None
        if direction in ('in','out'): last_exchange=stamp
    return {'pending':pending,'closed':closed,'unknown_time':unknown_time}

def route(pending,tier_value,formal_wechat=False,free_words=()):
    if not pending: return 'none'
    if formal_wechat: return 'wechat'
    return 'douyin' if tier_value in ('S','A','B','C') else 'outside_tier_scope'

def next_action(s_pending,coverage_verified,non_s='optional'):
    return 'S' if s_pending else non_s if coverage_verified else 'verify_coverage'

def response_target(pending,tier_value,time_precision):
    if not pending or tier_value!='S' or not pending['new_session']: return None
    if time_precision not in ('second','millisecond','exact'): return {'state':'verify_precision'}
    return {'state':'target','deadline':pending['start']+timedelta(seconds=60),'valid_first_response':'unverified'}
