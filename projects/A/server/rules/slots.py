from collections import defaultdict
from datetime import timedelta
from .daily import BJ

def time_bins(events,today):
    first=today-timedelta(days=29)
    bins=defaultdict(lambda:defaultdict(set));dates=defaultdict(set)
    for event in events:
        at=event.get('at');source=event.get('source')
        if at is None or at.tzinfo is None or not event.get('identity',True) or not event.get('reliable',True): continue
        if source not in ('incoming','entry','interaction'): continue
        if source=='incoming' and event.get('direction','in')!='in': continue
        local=at.astimezone(BJ);day=local.date()
        if first<=day<=today:
            bins[source][local.hour//2].add(day);dates[source].add(day)
    result={}
    for source,by_bin in bins.items():
        n=len(dates[source]);threshold=(n+1)//2
        qualified=sorted(((b,len(ds)) for b,ds in by_bin.items() if n>=5 and len(ds)>=threshold),key=lambda row:(-row[1],row[0]))
        selected=qualified[:2]
        if len(qualified)>=3 and qualified[1][1]==qualified[2][1]: selected=[r for r in qualified if r[1]>qualified[1][1]][:2]
        result[source]={'days':n,'threshold':threshold,'selected':[b for b,_ in selected],'dispersed':bool(qualified and not selected),'counts':{b:len(ds) for b,ds in sorted(by_bin.items())}}
    preferred=next((s for s in ('entry','interaction') if result.get(s,{}).get('selected')),None)
    return {'sources':result,'live_preferred':preferred}
