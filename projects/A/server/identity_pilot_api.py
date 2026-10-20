"""Finite read-only pilot routes, sharing candidate13 authentication/boundary.

No Application/business database, jobs, collection or synchronization is loaded.
Identifiers travel only in authenticated POST bodies, never URLs or logs.
"""
import re
from urllib.parse import urlsplit
from .common import AppError
from .identity_candidate_files import strict_json
from .pending_profile_lookup import lookup_profile_candidate

VALIDATION_BUDGET_SCOPE='task001-selection-receipt-20261013'

class IdentityPilotAPI:
    def __init__(self, app, *, index_path, index_sha256, account_key, namespace, lookup=lookup_profile_candidate, observe_connection=None, pairing=None, observe_pairing=None, historical_export_source_statement='unknown', validation_budget_scope=None, history_summary=None):
        self.app=app; self.index_path=index_path; self.pin=index_sha256
        if history_summary is not None and not callable(history_summary):raise ValueError('history summary provider required')
        self.history_summary=history_summary
        self.account=account_key; self.namespace=namespace; self.lookup=lookup
        self.observe_connection=observe_connection
        self.pairing=pairing; self.observe_pairing=observe_pairing
        if historical_export_source_statement not in ('unknown', 'user_confirmed'):
            raise ValueError('invalid source statement')
        self.export_source_statement=historical_export_source_statement
        if validation_budget_scope is not None and validation_budget_scope!=VALIDATION_BUDGET_SCOPE:
            raise ValueError('invalid validation budget scope')
        self.validation_budget_scope=validation_budget_scope
        if not re.fullmatch(r'[0-9a-f]{64}',index_sha256): raise ValueError('invalid pin')

    def envelope(self,data=None,code=None):
        return {'schema_version':2,'data_version':0,'coverage':{'source':'unverified'},
                'data':data,'errors':[] if code is None else [{'code':code,'message':'身份试点请求未通过核验。'}]}

    def principal(self,headers,scope,account=None):
        token=headers.get('Authorization','')
        if not token.startswith('Bearer ') or not 1 <= len(token[7:]) <= 1024:
            raise AppError(401,'unauthorized','Authentication required.')
        return self.app.auth.authenticate(token[7:],scope=scope,account_key=account)

    def handle(self,method,target,headers,raw=b''):
        try:
            u=urlsplit(target)
            if u.query or u.fragment or u.scheme or u.netloc: raise AppError(400,'invalid_request','Finite path required.')
            if method=='GET' and u.path=='/api/v1/health':
                return 200,self.envelope({'service':'identity-pilot-readonly','business_enabled':False})
            if method=='GET' and u.path=='/api/v1/identity-pilot/status':
                self.principal(headers,'history:read',self.account)
                if self.pairing is not None and self.observe_pairing is not None:
                    for eid in self.app.config['extension_ids']:
                        if headers.get('Origin')=='chrome-extension://'+eid:
                            self.observe_pairing(eid,'authenticated_extension_status')
                status={'account_key':self.account,'provider_namespace':self.namespace,
                    'artifact_sha256':self.pin,'private_context_verified':False,'owner_verified':False,
                    'historical_export_source_statement':self.export_source_statement}
                if self.validation_budget_scope is not None:
                    status['validation_budget_scope']=self.validation_budget_scope
                return 200,self.envelope(status)
            if method!='POST' or len(raw)>8192 or not headers.get('Content-Type','').startswith('application/json'):
                raise AppError(400,'invalid_request','Finite JSON request required.')
            body=strict_json(raw)
            if type(body) is not dict: raise AppError(400,'invalid_request','Object required.')
            device_routes={
                '/api/v1/identity-pilot/pair':{'extension_id','pairing_code'},
                '/api/v1/identity-pilot/device-challenge':{'extension_id','client_challenge'},
                '/api/v1/identity-pilot/device-session':{'extension_id','client_challenge','server_challenge','proof'},
            }
            if u.path in device_routes:
                if self.validation_budget_scope is not None and u.path.endswith('/pair'):
                    raise AppError(404,'not_found','New pairing unavailable in this validation round.')
                if self.pairing is None:raise AppError(404,'not_found','Device pairing unavailable.')
                if (headers.get('X-DSH-Schema')!='2' or set(body)!=device_routes[u.path] or
                    any(type(v)is not str for v in body.values())):
                    raise AppError(400,'invalid_request','Exact device request required.')
                eid=body['extension_id']
                if eid not in self.app.config['extension_ids'] or headers.get('Origin')!='chrome-extension://'+eid:
                    raise AppError(403,'forbidden','Configured extension required.')
                if u.path.endswith('/device-challenge'):
                    return 200,self.envelope(self.pairing.challenge(eid,body['client_challenge']))
                if u.path.endswith('/device-session'):
                    result=self.pairing.resume(eid,body['client_challenge'],body['server_challenge'],body['proof'])
                else:result=self.pairing.redeem(eid,body['pairing_code'])
                reply={'token':result['token'],'principal':result['principal'].as_dict()}
                if u.path.endswith('/pair'):
                    reply['device_credential']=result['device_credential']
                    if self.observe_pairing is not None:self.observe_pairing(eid,'pairing_session_issued')
                elif self.observe_pairing is not None:self.observe_pairing(eid,'device_session_issued')
                return 200,self.envelope(reply)
            if u.path=='/api/v1/health':
                if (set(body)!={'extension_id','build'} or
                    type(body['extension_id'])is not str or type(body['build'])is not str or
                    not re.fullmatch('[a-p]{32}',body['extension_id']) or
                    body['build']!='F-identity-pilot-connection-1' or headers.get('X-DSH-Schema')!='2'):
                    raise AppError(400,'invalid_request','Finite connection diagnostic required.')
                eid=body['extension_id']
                if eid not in self.app.config['extension_ids'] or headers.get('Origin')!='chrome-extension://'+eid:
                    raise AppError(403,'forbidden','Configured extension required.')
                if self.observe_connection is not None:self.observe_connection(eid)
                return 200,self.envelope({'service':'identity-pilot-readonly','business_enabled':False})
            if u.path=='/api/v1/auth/startup':
                keys={'operator_key','device_key','credential','client_kind','extension_id'}
                if set(body)!=keys or any(type(v)is not str for v in body.values()):
                    raise AppError(400,'invalid_request','Exact login required.')
                eid=body['extension_id']
                if body['client_kind']!='extension' or eid not in self.app.config['extension_ids'] or headers.get('Origin')!='chrome-extension://'+eid:
                    raise AppError(403,'forbidden','Registered extension required.')
                session=self.app.auth.login(body['operator_key'],body['credential'],device_key=body['device_key'],client_kind='extension',extension_id=eid)
                return 200,self.envelope({'token':session['token'],'principal':session['principal'].as_dict()})
            if u.path=='/api/v1/auth/logout':
                self.principal(headers,'capabilities:read')
                if body: raise AppError(400,'invalid_request','Empty logout required.')
                self.app.auth.logout(headers['Authorization'][7:])
                return 200,self.envelope({'authenticated':False})
            if u.path!='/api/v1/identity-pilot/lookup': raise AppError(404,'not_found','Route unavailable.')
            if headers.get('X-DSH-Schema')!='2' or set(body)!={'account_key','provider_namespace','artifact_sha256','sec_uid'}:
                raise AppError(400,'invalid_request','Exact lookup required.')
            if (body['account_key'],body['provider_namespace'],body['artifact_sha256'])!=(self.account,self.namespace,self.pin):
                raise AppError(403,'forbidden','Pinned dataset required.')
            expected=(self.account,self.namespace,self.pin,body['sec_uid'])
            def dependency(scope):
                if scope!=expected: raise AppError(403,'forbidden','Dependency changed.')
                self.principal(headers,'history:read',self.account)
            result=self.lookup(index_path=self.index_path,expected_index_sha256=self.pin,
                account_key=self.account,provider_namespace=self.namespace,sec_uid=body['sec_uid'],require_dependency=dependency)
            # The UI needs an exact echoed token and status, not the historical
            # UID, conversation references or full candidate records.
            keys=('format','account_key','provider_namespace','artifact_sha256','sec_uid','status','candidate_count',
                  'private_context_verified','owner_verified','formal_identity_match')
            data={k:result[k] for k in keys}
            if self.history_summary is not None:
                from .historical_sender_summary import unknown_summary,normalize_summary
                try:data['history_summary']=normalize_summary(self.history_summary(result),result['status']=='pending' and type(result['candidate_count']) is int and result['candidate_count']==1)
                except Exception:data['history_summary']=unknown_summary()
                dependency(expected)
            return 200,self.envelope(data)
        except AppError as e: return e.status,self.envelope(code=e.code)
        except Exception: return 400,self.envelope(code='invalid_request')
