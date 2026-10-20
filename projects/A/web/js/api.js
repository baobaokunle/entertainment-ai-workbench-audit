(function (root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHApi = value;
})(globalThis, function () {
  'use strict';
  const BASE = 'http://127.0.0.1:8801';
  const allowed = new Set(['health','capabilities','auth/startup','auth/logout','history','sidebar','contacts','overview','profile/field','groups','groups/contacts','groups/create','groups/update','groups/membership','ai/status','ai/job','ai/request','ai/preference','ai/control','import/binding-options','import/binding-inspect','import/candidate-context','import/candidate-plan','import/context','import/plan','import/commit','import/observations','import/withdraw','activity/devices','activity/timeline','activity/archives','deletion/plan','diagnostics/status','diagnostics/events']);
  function validateQuery(query,keys) {
    if (!query || typeof query !== 'object' || Array.isArray(query) || Object.keys(query).some(key => !keys.includes(key)) || typeof query.account_key !== 'string' || !query.account_key || query.account_key.length > 256) throw new Error('invalid_request');
    for (const key of ['person_key','conversation_key','keyword']) if (query[key] !== undefined && (typeof query[key] !== 'string' || query[key].length > (key === 'keyword' ? 200 : 256))) throw new Error('invalid_request');
    if (query.cursor !== undefined && query.cursor !== null && (typeof query.cursor !== 'string' || query.cursor.length > 4096)) throw new Error('invalid_request');
  }
  function createApi(fetcher = fetch) {
    allowed.add('auth/web-login');
    for(const endpoint of ['opportunity/time','opportunity/cancel','opportunity/display','contact-statistics','profile/birthday-candidates','profile/birthday-source'])allowed.add(endpoint);
    let token = null; let principal = null; let generation = 0;let writeMode=null;let readOnly=false;let healthGeneration=0;let noteAccount=null;
    function invalidateHealth() {healthGeneration++;writeMode=null;readOnly=false;noteAccount=null;}
    async function request(endpoint,{query,body,publicRequest = false,explicitToken} = {}) {
      if (!allowed.has(endpoint)) throw new Error('invalid_request');
      if (!publicRequest && !token && !explicitToken) throw new Error('unauthorized');
      const url = new URL(BASE + '/api/v1/' + endpoint);
      for (const [key,value] of Object.entries(query || {})) if (value !== '' && value !== null && value !== undefined) url.searchParams.set(key,key === 'cursor' && typeof value === 'object' ? JSON.stringify(value) : String(value));
      const headers = {Accept:'application/json'};
      if (explicitToken || token) headers.Authorization = 'Bearer ' + (explicitToken || token);
      if (body !== undefined) { headers['Content-Type'] = 'application/json'; if (endpoint !== 'auth/startup') headers['X-DSH-Schema'] = '2'; }
      const encoded=body === undefined ? undefined : JSON.stringify(body);
      if (endpoint.startsWith('import/') && encoded !== undefined && new TextEncoder().encode(encoded).byteLength > 512 * 1024) throw new Error('preview_too_large');
      // Explicit large review has a separate UI wait budget. This is not an
      // end-to-end server deadline: source checks may still be finishing.
      // Local reply rechecks protected storage before and after commit/read.
      // This client wait is not cancellation or a server execution deadline.
      const finiteAIWait=['ai/status','ai/job','ai/request'].includes(endpoint);
      const controller = new AbortController(); const timer = setTimeout(() => controller.abort(),endpoint==='import/candidate-plan'?1215000:endpoint==='profile/field'?30000:finiteAIWait?120000:8000); const stamp = generation;
      try {
        const response = await fetcher(url.href,{method:body === undefined ? 'GET' : 'POST',headers,body:encoded,signal:controller.signal,cache:'no-store',redirect:'error',credentials:'omit',referrerPolicy:'no-referrer'});
        if (!response.ok) throw new Error(endpoint==='auth/web-login'&&response.status===410?'web_login_expired':endpoint==='auth/web-login'&&response.status===503?'web_login_unavailable':response.status === 401 ? 'unauthorized' : response.status === 403 ? 'forbidden' : response.status === 409 ? 'conflict' : response.status===413&&endpoint.startsWith('activity/')?'activity_window_limit':response.status===413&&endpoint==='deletion/plan'?'deletion_plan_limit':[400,422].includes(response.status) ? 'invalid_value' : 'service_error');
        const raw = await response.text(); if (raw.length > 1024 * 1024) throw new Error('bad_response');
        const envelope = JSON.parse(raw); if (envelope.schema_version !== 2 || !envelope.data || typeof envelope.data !== 'object') throw new Error('bad_response');
        if (!publicRequest && !explicitToken && stamp !== generation) throw new Error('stale_session');
        return envelope.data;
      } catch(error){if(endpoint==='import/candidate-plan'&&error.name==='AbortError')throw new Error('candidate_review_timeout');if(finiteAIWait&&error.name==='AbortError')throw new Error('ai_wait_timeout');throw error;} finally { clearTimeout(timer); }
    }
    return {
      birthdayCandidates(query) {validateQuery(query,['account_key','person_key','cursor']);if(!query.person_key||!this.importReadAccess(query.account_key).allowed)throw Error('forbidden');return request('profile/birthday-candidates',{query});},
      birthdaySource(query) {validateQuery(query,['account_key','person_key','message_key','candidate_key']);if(!query.person_key||typeof query.message_key!=='string'||!query.message_key||query.message_key.length>200||!(/^[a-f0-9]{64}$/).test(query.candidate_key)||!this.importReadAccess(query.account_key).allowed)throw Error('invalid_request');return request('profile/birthday-source',{query});},
      contactStatisticsAccess(account) {return {allowed:!!token&&!!principal&&principal.account_keys.includes(account)&&Array.isArray(principal.scopes)&&principal.scopes.includes('history:read')&&((principal.role==='manager'&&principal.scopes.includes('activities:read'))||(principal.role==='baobao'&&principal.scopes.includes('activities:self'))),role:principal?.role,operator_key:principal?.operator_key};},
      contactStatistics(query) {validateQuery(query,['account_key','start_day','end_day','operator_key']);if(!this.contactStatisticsAccess(query.account_key).allowed)throw Error('forbidden');if(['start_day','end_day'].some(k=>typeof query[k]!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(query[k]))||query.start_day>query.end_day||(query.operator_key!==undefined&&(typeof query.operator_key!=='string'||!query.operator_key||query.operator_key.length>200||principal.role==='baobao'&&query.operator_key!==principal.operator_key)))throw Error('invalid_request');return request('contact-statistics',{query});},
      opportunity(action,body) {if(!['time','cancel','display'].includes(action))throw Error('invalid_request');validateQuery({account_key:body?.account_key,person_key:body?.person_key},['account_key','person_key']);const key=action==='time'?'time':'day';if(!body?.person_key||Object.keys(body).sort().join()!==['account_key','person_key',key,'expected_revision'].sort().join()||!Array.isArray(body.expected_revision)||body.expected_revision.some(v=>typeof v!=='string'||!v||v.length>256)||body.expected_revision.length>128||new Set(body.expected_revision).size!==body.expected_revision.length||!(action==='time'?/^(?:[01]\d|2[0-3]):[0-5]\d$/:/^\d{4}-\d{2}-\d{2}$/).test(body[key]))throw Error('invalid_request');if(!this.editingAccess(body.account_key,'opportunity_time').allowed)throw Error('editing_unavailable');return request('opportunity/'+action,{body});},
      diagnosticsAccess() {return {allowed:!!token&&!!principal&&Array.isArray(principal.scopes)&&((principal.role==='manager'&&principal.scopes.includes('diagnostics:read'))||(principal.role==='baobao'&&principal.scopes.includes('diagnostics:self'))),device_key:principal&&principal.device_key};},
      diagnosticsStatus(query) {if(!query||Object.keys(query).join()!=='device_key'||typeof query.device_key!=='string'||!query.device_key||query.device_key.length>200)throw Error('invalid_request');return request('diagnostics/status',{query});},
      diagnosticsEvents(query) {if(!query||typeof query!=='object'||Array.isArray(query)||Object.keys(query).some(key=>!['device_key','start_utc','end_utc','cursor','limit'].includes(key))||['device_key','start_utc','end_utc'].some(key=>typeof query[key]!=='string'||!query[key]||query[key].length>200)||(query.cursor!==undefined&&(typeof query.cursor!=='string'||!query.cursor||query.cursor.length>4096))||(query.limit!==undefined&&(!Number.isInteger(query.limit)||query.limit<1||query.limit>200)))throw Error('invalid_request');return request('diagnostics/events',{query});},
      deletionAccess(account) {return {allowed:!!token&&!!principal&&principal.role==='manager'&&principal.account_keys.includes(account)&&Array.isArray(principal.scopes)&&principal.scopes.includes('deletion:plan')};},
      deletionPlan(body) {if(!body||typeof body!=='object'||Array.isArray(body)||Object.keys(body).sort().join()!=='account_key,target_key,target_kind'||!['message','observation'].includes(body.target_kind)||['account_key','target_key'].some(key=>typeof body[key]!=='string'||!body[key]||body[key].length>200))throw new Error('invalid_request');if(!token||!principal||principal.role!=='manager'||!principal.account_keys.includes(body.account_key)||!Array.isArray(principal.scopes)||!principal.scopes.includes('deletion:plan'))throw new Error('forbidden');return request('deletion/plan',{body});},
      activityAccess() {return {allowed:!!token&&!!principal&&Array.isArray(principal.scopes)&&((principal.role==='manager'&&principal.scopes.includes('activities:read'))||(principal.role==='baobao'&&principal.scopes.includes('activities:self'))),role:principal&&principal.role};},
      activityDevices:()=>request('activity/devices'),
      activityTimeline(query) {if(!query||typeof query!=='object'||Array.isArray(query)||Object.keys(query).some(key=>!['device_key','start_utc','end_utc','tau'].includes(key))||['device_key','start_utc','end_utc'].some(key=>typeof query[key]!=='string'||!query[key]||query[key].length>200)||![30,60,90].includes(query.tau))throw new Error('invalid_request');return request('activity/timeline',{query});},
      activityArchives(query) {if(!query||typeof query!=='object'||Array.isArray(query)||Object.keys(query).sort().join()!=='device_key,end_day,start_day,tau'||['device_key','start_day','end_day'].some(key=>typeof query[key]!=='string'||!query[key]||query[key].length>200)||![30,60,90].includes(query.tau))throw new Error('invalid_request');return request('activity/archives',{query});},
      importReadAccess(account) {return {allowed:!!token&&!!principal&&['manager','baobao'].includes(principal.role)&&principal.account_keys.includes(account)&&Array.isArray(principal.scopes)&&principal.scopes.includes('history:read')};},
      importBindingAccess(account) {return {allowed:!!token&&!!principal&&principal.role==='manager'&&principal.account_keys.includes(account)&&Array.isArray(principal.scopes)&&['identities:write','observations:write'].every(scope=>principal.scopes.includes(scope))};},
      importBindingOptions(query) {validateQuery(query,['account_key']);if(query.account_key.length>200)throw Error('invalid_request');if(!this.importBindingAccess(query.account_key).allowed)throw Error('forbidden');return request('import/binding-options',{query});},
      importBindingInspect(body) {const binding=body&&body.binding;if(!body||Object.keys(body).join()!=='binding'||!binding||typeof binding!=='object'||Array.isArray(binding)||Object.keys(binding).sort().join()!=='account_key,account_namespace,source_id,source_version'||Object.entries(binding).some(([key,value])=>typeof value!=='string'||!value.trim()||value.includes('\u0000')||value.length>(key==='source_version'?80:key==='account_namespace'?256:200)))throw Error('invalid_request');if(!this.importBindingAccess(binding.account_key).allowed)throw Error('forbidden');return request('import/binding-inspect',{body});},
      candidateReviewContext(query) {validateQuery(query,['account_key']);if(!this.importBindingAccess(query.account_key).allowed)throw Error('forbidden');return request('import/candidate-context',{query});},
      candidateReviewPlan(body) {const binding=body&&body.binding;if(!body||Object.keys(body).sort().join()!=='binding,page_count,page_index,review_key,unknown_records'||!binding||typeof binding!=='object'||Array.isArray(binding)||Object.keys(binding).sort().join()!=='account_key,account_namespace,source_id,source_version'||Object.entries(binding).some(([key,value])=>typeof value!=='string'||!value.trim()||value.includes('\u0000')||value.length>(key==='source_version'?80:key==='account_namespace'?256:200))||!/^[0-9a-f]{64}$/.test(body.review_key)||!Number.isSafeInteger(body.page_index)||body.page_index<1||!Number.isInteger(body.page_count)||body.page_count<1||body.page_count>20||body.unknown_records!==true)throw Error('invalid_request');if(!this.importBindingAccess(binding.account_key).allowed)throw Error('forbidden');return request('import/candidate-plan',{body});},
      importContext(query) {validateQuery(query,['account_key']);return request('import/context',{query});},
      importPlan(body) {if(!body || Object.keys(body).sort().join()!=='mapping,preview')throw new Error('invalid_request');return request('import/plan',{body});},
      importCommit(body) {if(!body || Object.keys(body).sort().join()!=='expected_plan_hash,mapping,preview')throw new Error('invalid_request');return request('import/commit',{body});},
      importObservations(query) {validateQuery(query,['account_key','batch_id','keyword','before','limit','person_key','source_id','source_version','account_namespace']);return request('import/observations',{query});},
      importWithdraw(body) {if(!body || Object.keys(body).sort().join()!=='account_key,batch_id,expected_scope_hash'||typeof body.expected_scope_hash!=='string'||!body.expected_scope_hash)throw new Error('invalid_request');return request('import/withdraw',{body});},
      aiStatus(query) {validateQuery(query,['account_key','person_key']); if (!principal || !principal.account_keys.includes(query.account_key)) throw new Error('forbidden'); return request('ai/status',{query});},
      aiJob(query) {validateQuery(query,['account_key','job_key']); if (typeof query.job_key!=='string' || !query.job_key || query.job_key.length>256 || !principal || !principal.account_keys.includes(query.account_key)) throw new Error('invalid_request'); return request('ai/job',{query});},
      aiRequest(body) {if (!body || Object.keys(body).sort().join()!=='account_key,on_demand,person_key,purpose' || !['emotion','topics','reply'].includes(body.purpose) || typeof body.person_key!=='string' || typeof body.on_demand!=='boolean' || (body.purpose==='reply' && !body.on_demand) || !principal || !principal.account_keys.includes(body.account_key)) throw new Error('invalid_request'); return request('ai/request',{body});},
      aiPreference(body) {if (!body || Object.keys(body).sort().join()!=='account_key,enabled,expected_revision,person_key,purpose' || !['emotion','topics','reply'].includes(body.purpose) || typeof body.enabled!=='boolean' || !Array.isArray(body.expected_revision) || !principal || !principal.account_keys.includes(body.account_key)) throw new Error('invalid_request'); return request('ai/preference',{body});},
      aiControl(body) {if (!body || Object.keys(body).sort().join()!=='account_key,expected_revision,job_key,operation,value' || !['hide','cancel','correct'].includes(body.operation) || !Array.isArray(body.expected_revision) || !principal || !principal.account_keys.includes(body.account_key)) throw new Error('invalid_request'); return request('ai/control',{body});},
      async health() {
        const stamp=++healthGeneration,session=generation;writeMode=null;readOnly=false;noteAccount=null;
        const value=await request('health',{publicRequest:true});
        let note=null;
        if(value.local_notes_state==='explicit_local_notes'&&token&&principal){
          const capability=(await request('capabilities')).local_access;
          if(capability&&capability.state==='explicit_local_notes'&&typeof capability.reply_on_demand==='boolean'&&
              Array.isArray(capability.profile_fields)&&capability.profile_fields.length===1&&capability.profile_fields[0]==='note'&&
              principal.account_keys.includes(capability.account_key))note=capability.account_key;
        }
        if(stamp!==healthGeneration||session!==generation)throw new Error('stale_session');
        writeMode=typeof value.writes_enabled==='boolean'?value.writes_enabled:null;readOnly=value.read_only_rollback===true;noteAccount=note;return value;
      },
      editingAccess(account,field) {
        const limited=noteAccount===account&&field==='note';
        const reason=!token||!principal?'unauthorized':!principal.account_keys.includes(account)?'forbidden':readOnly?'read_only':writeMode===false&&!limited?(noteAccount===account?'local_notes_only':'writes_disabled'):writeMode===null?'unknown':!Array.isArray(principal.scopes)?'unknown':!principal.scopes.includes('profile:write')?'forbidden':null;
        return {allowed:reason===null,reason};
      },
      capabilities:() => request('capabilities'),
      async loginWithCode(code) {
        if (token) throw new Error('already_authenticated');
        if (typeof code!=='string'||!/^[A-Za-z0-9_-]{43}$/.test(code)) throw new Error('invalid_login_code');
        const stamp=++generation;invalidateHealth();
        const result=await request('auth/web-login',{body:{code},publicRequest:true});
        const p=result.principal;
        if (typeof result.token!=='string'||!/^[A-Za-z0-9_-]{43}$/.test(result.token)||!p||
            !['manager','baobao'].includes(p.role)||!Array.isArray(p.account_keys)||!Array.isArray(p.scopes)||
            [p.operator_key,p.device_key,...p.account_keys].some(x=>typeof x!=='string'||!/^[A-Za-z0-9_:.\-]{1,128}$/.test(x))) {
          if(typeof result.token==='string')await request('auth/logout',{body:{},explicitToken:result.token}).catch(()=>{});
          throw new Error('bad_response');
        }
        if(stamp!==generation){await request('auth/logout',{body:{},explicitToken:result.token}).catch(()=>{});throw new Error('stale_session');}
        token=result.token;principal=p;
        return {operator_key:p.operator_key,device_key:p.device_key,role:p.role,account_keys:[...p.account_keys]};
      },
      async login(operator_key,device_key,credential) {
        if (token) throw new Error('already_authenticated');
        const stamp = ++generation;
        invalidateHealth();
        const result = await request('auth/startup',{body:{operator_key,device_key,credential,client_kind:'workbench'},publicRequest:true});
        if (typeof result.token !== 'string' || !result.principal || !Array.isArray(result.principal.account_keys) || result.principal.operator_key !== operator_key || result.principal.device_key !== device_key) throw new Error('bad_response');
        if (stamp !== generation) { await request('auth/logout',{body:{},explicitToken:result.token}).catch(() => {}); throw new Error('stale_session'); }
        token = result.token; principal = result.principal;
        return {operator_key:principal.operator_key,device_key:principal.device_key,role:principal.role,account_keys:[...principal.account_keys]};
      },
      async logout() {
        const old = token; token = null; principal = null; generation++;
        invalidateHealth();
        if (old) await request('auth/logout',{body:{},explicitToken:old});
      },
      history(query) {
        validateQuery(query,['account_key','person_key','conversation_key','keyword','day','cursor','limit']);
        if (!principal || !principal.account_keys.includes(query.account_key)) throw new Error('forbidden');
        if (!Number.isInteger(query.limit || 50) || (query.limit || 50) < 1 || (query.limit || 50) > 100) throw new Error('invalid_request');
        return request('history',{query});
      },
      sidebar(query) { validateQuery(query,['account_key','person_key']); if (!query.person_key || !principal || !principal.account_keys.includes(query.account_key)) throw new Error('forbidden'); return request('sidebar',{query}); },
      contacts(query) {validateQuery(query,['account_key']); if (!principal || !principal.account_keys.includes(query.account_key)) throw new Error('forbidden'); return request('contacts',{query});},
      overview(query) {validateQuery(query,['account_key']); if (!principal || !principal.account_keys.includes(query.account_key)) throw new Error('forbidden'); return request('overview',{query});},
      groups(query) {validateQuery(query,['account_key']); if (!principal || !principal.account_keys.includes(query.account_key)) throw new Error('forbidden'); return request('groups',{query});},
      groupDirectory(query) {validateQuery(query,['account_key']); if (!principal || !principal.account_keys.includes(query.account_key)) throw new Error('forbidden'); return request('groups/contacts',{query});},
      groupCreate(body) {if (!body || Object.keys(body).sort().join()!=='account_key,name' || typeof body.name!=='string' || !principal || !principal.account_keys.includes(body.account_key)) throw new Error('invalid_request'); return request('groups/create',{body});},
      groupUpdate(body) {if (!body || Object.keys(body).sort().join()!=='account_key,deleted,expected_revision,group_id,name' || !Array.isArray(body.expected_revision) || typeof body.deleted!=='boolean' || !principal || !principal.account_keys.includes(body.account_key)) throw new Error('invalid_request'); return request('groups/update',{body});},
      groupMembership(body) {if (!body || Object.keys(body).sort().join()!=='account_key,expected_revision,group_id,member,person_key' || !Array.isArray(body.expected_revision) || typeof body.member!=='boolean' || !principal || !principal.account_keys.includes(body.account_key)) throw new Error('invalid_request'); return request('groups/membership',{body});},
      profileField(body) {
        const keys = ['account_key','person_key','field','value','expected_revision','clear'];
        if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some(key => !keys.includes(key)) || keys.some(key => !Object.prototype.hasOwnProperty.call(body,key)) || typeof body.person_key !== 'string' || !body.person_key || typeof body.field !== 'string' || typeof body.clear !== 'boolean' || !Array.isArray(body.expected_revision) || body.expected_revision.some(item=>typeof item!=='string'||!item)) throw new Error('invalid_request');
        if (!principal || !principal.account_keys.includes(body.account_key)) throw new Error('forbidden');
        return request('profile/field',{body});
      }
    };
  }
  return {BASE,createApi};
});
