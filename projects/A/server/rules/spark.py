from .daily import daily_out

def spark(uid,messages,day,state,state_day,complete=True,special=False):
    sent=bool(daily_out(uid,messages,day))
    if special: return {'state':'special_unverified','suppress_send':sent}
    if state_day!=day or state=='unknown': return {'state':'unknown','suppress_send':sent}
    if state in ('red','blue'): return {'state':'completed','suppress_send':True}
    if state=='gray' and sent: return {'state':'waiting','suppress_send':True}
    if state!='gray': return {'state':'unknown','suppress_send':sent}
    return {'state':'pending' if complete else 'unknown','suppress_send':not complete}
