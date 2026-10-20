"""One round of combinations of original, compatible ranking qualifications."""
from itertools import product

RULE_VERSION = 'tier-20261008-combo-v1'

def explain_tier(contribution, comments, watching, clip, windows=(None,None,None), identity=True):
    """Explain the existing rule; no session metrics or frontend recalculation."""
    grade = tier(contribution, comments, watching, clip, windows=windows, identity=identity)
    valid = all(v is None or type(v) is int and v > 0 for v in (contribution,comments,watching)) and (clip is None or type(clip) is bool)
    compatible = len(windows)==3 and all(w is not None for w in windows) and len(set(windows))==1
    raw = dict.fromkeys('SABCD')
    if valid and identity and compatible:
        between = lambda value,lo,hi: None if value is None else lo<=value<=hi
        b = (between(comments,1,50),between(watching,1,50))
        raw = {'S':between(contribution,1,50),'A':between(contribution,51,100),
               'B':False if False in b else None if None in b else True,
               'C':clip,'D':between(watching,1,100)}
    elif type(clip) is bool and identity:
        raw['C'] = clip
    combination = next((code for code,a,b,target in [('A+B','A','B','S'),('B+C','B','C','A'),('C+D','C','D','B')]
                        if grade==target and raw[a] is True and raw[b] is True), None)
    reason = ('identity_unknown' if not identity else 'input_invalid' if not valid else
              'period_missing' if len(windows)!=3 or any(w is None for w in windows) else
              'period_mismatch' if not compatible else 'qualification_incomplete' if grade=='unknown' else 'sufficient')
    missing = [name for name,value in zip(('contribution','comment','watch','clip'),(contribution,comments,watching,clip)) if value is None]
    return {'format':'tier-explanation-1','rule_version':RULE_VERSION,'final_grade':grade,
            'original_qualifications':raw,'combination':combination,'reason':reason,'missing_inputs':missing}

def raw_tier(contribution, comments, watching, clip, excluded=False):
    if excluded:
        return 'excluded'
    original_s = 1 <= contribution <= 50
    original_a = 51 <= contribution <= 100
    original_b = 1 <= comments <= 50 and 1 <= watching <= 50
    original_d = 1 <= watching <= 100
    qualifications = [('S', original_s or (original_a and original_b)),
                      ('A', original_a or (original_b and clip)),
                      ('B', original_b or (clip and original_d)),
                      ('C', clip), ('D', original_d)]
    return next((label for label, qualified in qualifications if qualified), 'E')

def tier(contribution, comments, watching, clip, windows=('w1','w1','w1'), identity=True, excluded=False):
    if not identity or len(windows) != 3 or any(w is None for w in windows) or len(set(windows)) != 1:
        return 'unknown'
    inputs = (contribution, comments, watching, clip)
    if any(v is not None and (isinstance(v,bool) or not isinstance(v,int) or v < 1) for v in inputs[:3]):
        return 'unknown'
    if clip is not None and not isinstance(clip,bool):
        return 'unknown'
    partitions = ((25,75,101),(25,101),(25,75,101),(False,True))
    outcomes = {raw_tier(*combination,excluded) for combination in product(*[d if v is None else (v,) for v,d in zip(inputs,partitions)])}
    return outcomes.pop() if len(outcomes)==1 else 'unknown'

def ranking_value(snapshot, *, account_key, metric, window):
    if not snapshot or snapshot.get('account_key') != account_key or snapshot.get('metric') != metric or snapshot.get('window') != window:
        return None
    if not snapshot.get('identity_verified') or not snapshot.get('coverage_complete'):
        return None
    rank = snapshot.get('rank')
    # A verified out-of-list bound is meaningful; missing entries are never zero.
    if rank is None and snapshot.get('known_outside_top'):
        rank = snapshot.get('top_limit',100)+1
    return rank if type(rank) is int and rank > 0 else None
