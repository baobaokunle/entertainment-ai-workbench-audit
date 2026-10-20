from datetime import date
DEFAULT_REMINDERS={'vip':7,'star':7,'birthday':0,'anniversary':0}

def term_status(start,finish,today,advance=7):
    if start is None or finish is None: return {'status':'unknown','remind':False}
    if finish<start: return {'status':'invalid','remind':False}
    if today<start: return {'status':'future','remind':False}
    if today>finish: return {'status':'expired','days':(today-finish).days,'remind':False}
    remaining=(finish-today).days
    return {'status':'today_expiry' if remaining==0 else 'active','days':remaining,'remind':remaining<=advance}

def next_anniversary(month,day,today,calendar_kind='gregorian',advance=0):
    if calendar_kind!='gregorian': return {'status':'verify','remind':False}
    try:
        candidate=date(today.year,month,day)
        if candidate<today: candidate=date(today.year+1,month,day)
    except (ValueError,TypeError): return {'status':'verify','remind':False}
    delta=(candidate-today).days
    return {'status':'known','next':candidate,'days':delta,'remind':delta<=advance}

def reminder_by_kind(kind,payload,today,settings):
    return term_status(payload[0],payload[1],today,settings[kind])['remind'] if kind in ('vip','star') else next_anniversary(payload[0],payload[1],today,advance=settings[kind])['remind']

def completed_anniversary(origin,year):
    if year<=origin.year: return None
    try: return date(year,origin.month,origin.day)
    except ValueError: return None
