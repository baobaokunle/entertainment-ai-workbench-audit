from datetime import datetime, timedelta, timezone
BJ = timezone(timedelta(hours=8))

def occurred_day(message):
    stamp = message.get('at')
    if stamp is not None:
        return stamp.astimezone(BJ).date() if stamp.tzinfo is not None else None
    lower,upper = message.get('occurred_lower'),message.get('occurred_upper')
    if lower is None or upper is None or lower.tzinfo is None or upper.tzinfo is None or upper <= lower:
        return None
    first = lower.astimezone(BJ).date()
    return first if (upper-timedelta(microseconds=1)).astimezone(BJ).date()==first else None

def daily_out(uid,messages,day):
    return [m for m in messages if m.get('uid')==uid and m.get('direction')=='out' and m.get('success') is True
            and m.get('kind')=='personal' and m.get('identity_verified',True) and occurred_day(m)==day]

def maintenance(uid,messages,day,tagged=True,complete=True):
    if not tagged: return 'stopped'
    if daily_out(uid,messages,day): return 'completed'
    return 'pending' if complete else 'unknown'
