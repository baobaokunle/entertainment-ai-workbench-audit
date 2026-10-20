"""Host-only finite rank adoption. No capture, page authority or nickname joins.

The host reviews the resulting plan before applying its exact digest. Preparing
a plan is not approval. B's private reader authenticates custody, not semantics.
"""
from collections import Counter
from datetime import date,datetime,timedelta,timezone
import json
import re

from . import backend_candidates as backend
from .common import AppError,canonical_json
from .db import digest,stable_key
from .fixed_handoff_candidates import inspect_payload,MAX_BYTES
from .identity import IdentityService
from .rank_facts import RankFacts
from .rules.daily import BJ

KINDS={'score:30d':'contribution','comment_cnt:30d':'comment','watch_duration:30d':'watch'}

def fail(code):
    raise AppError(409,code,'本次有限榜单采纳未通过；未扩大来源或页面权限。')

def exact(field,*,numeric=False):
    if not isinstance(field,dict):fail('RANK_IDENTITY_FIELD_MISSING')
    backend.scalar(field)
    if field['lossless'] is not True:fail('RANK_SCALAR_LOSSY')
    if numeric:
        if field['type'] not in ('string','number_token','number'):fail('RANK_NUMERIC_INVALID')
        value=field['value']
        if type(value) is int and field['type']=='number':value=str(value)
        if type(value) is not str or re.fullmatch('[0-9]{1,64}',value) is None:fail('RANK_NUMERIC_INVALID')
        return value
    if field['type']!='string' or not field['value']:fail('RANK_IDENTITY_FIELD_MISSING')
    return field['value']

def prepare(raw,package_sha256,application_day,db,*,now=None):
    now=now or datetime.now(timezone.utc)
    receipt=inspect_payload(raw,package_sha256,'rank30d',now=now)
    data=backend.decode(raw,MAX_BYTES)
    day=date.fromisoformat(application_day)
    if day.isoformat()!=application_day or receipt['rank_observed_beijing_days']!=[application_day]:fail('RANK_BUSINESS_DAY_MISMATCH')
    owner=exact(data['owner'],numeric=True)
    accounts=db.conn.execute('SELECT account_key,platform_uid,verified FROM own_account WHERE platform=?',('douyin',)).fetchall()
    matches=[r for r in accounts if r['platform_uid']==owner and r['verified']]
    if len(matches)!=1:fail('RANK_REGISTERED_OWNER_MISMATCH')
    account=matches[0]['account_key']
    account_rows=[r for r in data['observations'] if r['observation']['domain']=='account']
    if (len(account_rows)!=1 or account_rows[0]['observation']['fields'].get('user_id_str')!=data['owner']
            or account_rows[0]['observation']['fields'].get('app_id')!=data['app_scope']):fail('RANK_ACCOUNT_CONTEXT_MISMATCH')
    if (len(data['runs'])!=1 or 'fresh_batch' not in data['runs'][0]
            or data['runs'][0]['fresh_batch']['owner']!=data['owner']
            or data['runs'][0]['fresh_batch']['app']!=data['app_scope']
            or data['operation_receipts']!=4):fail('RANK_FRESH_RECEIPT_REQUIRED')
    source='adopted-rank-'+package_sha256
    adoption={'format':'rank-observed-adoption-1','time_basis':'user_confirmed_previous_broadcast',
        'statistics_through_day':(day-timedelta(days=1)).isoformat(),
        'package_sha256':package_sha256,'expires_at':data['expires_at']}
    groups={k:[] for k in KINDS};people={};ids=IdentityService(db);excluded=Counter()
    for outer in data['observations']:
        ob=outer['observation']
        if ob['domain']!='rank':continue
        label=ob['context']['period_label'];f=ob['fields']
        if label not in groups or ob['context']['session'] is not None:fail('RANK_SCOPE_INVALID')
        # A redacted/no-user source row is not a person with UID "0". Keep its
        # response position for custody checks, but never enroll or repair it.
        if (f.get('user.id_str',{}).get('value')=='' and f.get('user.id',{}).get('value')=='0'
                and f.get('user.sec_uid',{}).get('value')==''):
            groups[label].append((outer,None));excluded['empty_source_identity']+=1;continue
        uid=exact(f.get('user.id_str',f.get('user.id')),numeric=True)
        if int(uid)==0:fail('RANK_IDENTITY_FIELD_MISSING')
        if f.get('user.id') and f['user.id']['type']!='null' and exact(f['user.id'],numeric=True)!=uid:fail('RANK_UID_FIELDS_CONFLICT')
        sec=None
        if f.get('user.sec_uid') and f['user.sec_uid']['type']!='null':
            sec=exact(f['user.sec_uid'])
            if re.fullmatch('[A-Za-z0-9_-]{1,256}',sec) is None:fail('RANK_SEC_UID_INVALID')
        position=int(exact(f.get('rank'),numeric=True))
        if not 1<=position<=1000000:fail('RANK_POSITION_INVALID')
        person=stable_key('person',account,'douyin','uid',uid)
        # Any cross-key disagreement requires a separate reconciliation.
        for kind,value in [('uid',uid)]+([('sec_uid',sec)] if sec else []):
            old=ids.resolve(account,'person',kind,value)
            if old and (old['person_key']!=person or not old['verified']):fail('RANK_IDENTITY_RECONCILIATION_REQUIRED')
        old=people.get(uid)
        if old and sec and old['sec_uid'] and old['sec_uid']!=sec:fail('RANK_IDENTITY_RECONCILIATION_REQUIRED')
        if not old or (sec and not old['sec_uid']):
            people[uid]={'uid':uid,'sec_uid':sec,'person_key':person,'observation_id':outer['observation_id']}
        value=f.get('value',{})
        raw_value=str(value['value']) if value.get('lossless') is True and value.get('type') in ('number_token','string','number') else ''
        if len(raw_value)>200:fail('RANK_METRIC_LIMIT')
        groups[label].append((outer,{'person_key':person,'position':position,'metric':{
            'state':'unknown','value':None,'unit':'未知（原单位未核验）','raw_display':raw_value,'precision':'unknown'}}))
    period='business-30d-'+application_day+'-'+package_sha256[:16]
    payloads=[]
    for label,selected in groups.items():
        if not selected:fail('RANK_TRIO_REQUIRED')
        observations=[item[0]['observation'] for item in selected];first=observations[0]
        for number,ob in enumerate(observations):
            if (ob['source']['record_ref']!='/projection/rows/'+str(number)
                    or ob['observed_at']!=first['observed_at'] or ob['context']!=first['context']
                    or ob['coverage']!=first['coverage'] or ob['coverage']['received']!=len(selected)
                    or backend.stamp(ob['observed_at'])>now
                    or backend.stamp(ob['observed_at']).astimezone(BJ).date()!=day):fail('RANK_RESPONSE_GROUP_INVALID')
        rows=[item[1] for item in selected if item[1] is not None]
        if not rows:fail('RANK_QUALIFIED_ROWS_MISSING')
        if len({r['position'] for r in rows})!=len(rows) or len({r['person_key'] for r in rows})!=len(rows):fail('RANK_DUPLICATE_POSITION_OR_PERSON')
        payloads.append({'revision_id':source+'-'+KINDS[label],'account_key':account,'rank_kind':KINDS[label],
            'period_key':period,'observed_period_key':period,'period_start_utc':None,'period_end_utc':None,
            'period_label':'近30天，截至'+adoption['statistics_through_day']+'直播后（用户确认口径）',
            'source_id':source,'source_version':'1','source_revision':1,
            'captured_at_utc':first['observed_at'],'period_basis':'rolling_30d','rank_as_of_day':application_day,
            'pages':[{'page_number':1,'rows':rows}],'pagination_terminal':False,'adoption':adoption})
    evidence={'format':'reviewed-rank-handoff-1','rank_adoption':adoption,
        'rank_payload_digests':[digest(p) for p in payloads],
        'input_sha256':__import__('hashlib').sha256(raw).hexdigest(),
        'owner':data['owner'],'app_scope':data['app_scope'],
        'source_run_approval_sha256':data['runs'][0]['fresh_batch']['code_hash'],
        'excluded_rows':dict(excluded),'selected_rank_rows':sum(len(v) for v in groups.values()),
        'scope':'original_returned_rank_positions_only','platform_complete':False,
        'direct_identity_records':[{'account_key':account,'uid':p['uid'],'sec_uid':p['sec_uid'],
            'observation_id':p['observation_id']} for p in people.values() if p['sec_uid']]}
    return {'format':'rank-handoff-plan-1','account_key':account,'source_id':source,
        'source_evidence':evidence,'people':list(people.values()),'payloads':payloads}

def summary(plan):
    a=plan['source_evidence']['rank_adoption']
    return {'format':'rank-handoff-plan-summary-1','plan_sha256':digest(plan),
        'package_sha256':a['package_sha256'],'application_day':plan['payloads'][0]['rank_as_of_day'],
        'statistics_through_day':a['statistics_through_day'],'expires_at':a['expires_at'],
        'people':len(plan['people']),'same_record_pairs':sum(bool(p['sec_uid']) for p in plan['people']),
        'rank_counts':{p['rank_kind']:len(p['pages'][0]['rows']) for p in plan['payloads']},
        'selected_rank_rows':plan['source_evidence']['selected_rank_rows'],'excluded_rows':plan['source_evidence']['excluded_rows'],
        'platform_complete':False,'source_exact_window':False,'chat_bodies_read':0}

def apply(plan,expected_digest,db,principal,*,now=None):
    """Explicit local host call. Never exposed as an HTTP/browser endpoint."""
    plan=json.loads(canonical_json(plan));now=now or datetime.now(timezone.utc)
    if digest(plan)!=expected_digest or plan.get('format')!='rank-handoff-plan-1':fail('RANK_REVIEW_DIGEST_MISMATCH')
    account=plan['account_key'];ids=IdentityService(db)
    if principal.role!='manager':fail('RANK_MANAGER_REQUIRED')
    for cap in ('source:register','identities:write'):principal.require(cap,account)
    if backend.stamp(plan['source_evidence']['rank_adoption']['expires_at'])<=now:fail('RANK_SOURCE_EXPIRED')
    with db.transaction() as conn:
        from .local_deletion import require_ready
        require_ready(db)
        old=ids.get_source(plan['source_id'],'1')
        if old and not old['verified']:fail('RANK_SOURCE_WITHDRAWN')
        if ids.origin_withdrawn(principal.device_key,account,plan['source_id'],'1'):fail('RANK_SOURCE_WITHDRAWN')
        # Do not replace the existing account source or its runtime authorization.
        owner=exact(plan['source_evidence']['owner'],numeric=True)
        registered=conn.execute('SELECT platform_uid,verified FROM own_account WHERE account_key=?',(account,)).fetchone()
        if not registered or not registered['verified'] or registered['platform_uid']!=owner:fail('RANK_REGISTERED_OWNER_MISMATCH')
        caps={key:True for key in ('person_identity','rank_identity','rank_rows','rank_observed_rows','rank_business_day','rank_rolling_day','direct_identity_mapping')}
        caps['account_keys']=[account]
        ids.register_source(plan['source_id'],'1',source_kind='qualified_adapter',capabilities=caps,verified=True,evidence=plan['source_evidence'])
        for p in plan['people']:
            prior=ids.resolve(account,'person','uid',p['uid'])
            if prior:
                if not prior['verified'] or prior['person_key']!=p['person_key']:fail('RANK_IDENTITY_RECONCILIATION_REQUIRED')
            else:ids.register_identity(account,'person','uid',p['uid'],source_id=plan['source_id'],verified=True,evidence={'observation_id':p['observation_id']})
            if p['sec_uid']:
                alias=ids.resolve(account,'person','sec_uid',p['sec_uid'])
                if alias:
                    if not alias['verified'] or alias['person_key']!=p['person_key']:fail('RANK_IDENTITY_RECONCILIATION_REQUIRED')
                    # Existing direct identity approvals stay owned by their source.
                elif prior is None:
                    ids.register_direct_person_mapping(account,p['uid'],p['sec_uid'],source_id=plan['source_id'],observation_id=p['observation_id'])
                # An older UID-only source is retained unchanged. Adding its
                # second alias needs its own reconciliation; ranks still use UID.
        # This reviewed host submission imports sealed observations; it does not
        # grant collection:run or invoke the collector/page collection endpoint.
        results=[RankFacts(db).stage(p,now=now) for p in plan['payloads']]
        if any(r['state']!='observed' for r in results):fail('RANK_ADOPTION_NOT_ACTIVE')
        if any(RankFacts(db).get_active(account,p['rank_kind'],p['period_key'],now=now)['state']!='observed' for p in plan['payloads']):fail('RANK_ADOPTION_NOT_ACTIVE')
    return {**summary(plan),'status':'duplicate' if all(r['status']=='duplicate' for r in results) else 'adopted',
        'formal_acceptance':True,'history_merges':0,'runtime_permissions_changed':False}
