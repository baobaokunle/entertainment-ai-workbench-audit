(function (root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHProtocol = value;
})(globalThis, function () {
  'use strict';
  const BASE = 'http://127.0.0.1:8801';
  const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
  function fail() { throw new Error('invalid_request'); }
  function exact(value, keys) { if (!plain(value) || Object.keys(value).some(key => !keys.includes(key))) fail(); }
  function text(value, max = 256, optional = false) {
    if (optional && (value === undefined || value === null || value === '')) return;
    if (typeof value !== 'string' || !value.length || value.length > max || /[\u0000-\u0008]/.test(value)) fail();
  }
  function chatURL(value) {
    try { const u = new URL(value); return u.origin === 'https://www.douyin.com' && u.pathname === '/chat' && !u.username && !u.password; } catch { return false; }
  }
  function senderKind(sender, id) {
    if (!/^[a-p]{32}$/.test(id) || !sender || sender.id !== id) fail();
    if (sender.url === `chrome-extension://${id}/popup.html` && !sender.tab && (sender.frameId === undefined || sender.frameId === 0)) return 'popup';
    if (sender.frameId === 0 && sender.tab && Number.isInteger(sender.tab.id) && sender.tab.id >= 0 && chatURL(sender.url) && chatURL(sender.tab.url)) return 'chat';
    fail();
  }
  function history(query) {
    exact(query, ['account_key', 'person_key', 'conversation_key', 'keyword', 'cursor', 'limit']);
    text(query.account_key); text(query.person_key, 256, true); text(query.conversation_key, 256, true);
    text(query.keyword, 200, true); text(query.cursor, 4096, true);
    if (query.limit !== undefined && (!Number.isInteger(query.limit) || query.limit < 1 || query.limit > 100)) fail();
    return { ...query };
  }
  function observation(value) {
    exact(value, ['observation_id','source_id','source_version','account_key','object_kind','identity_kind','identity_value','platform_conversation_key','platform_message_key','platform_correlation_key','direction','kind','body','occurred_lower_utc','occurred_upper_utc','time_precision','platform_success','coverage']);
    for (const key of ['observation_id','source_id','source_version','account_key','identity_kind','identity_value','platform_conversation_key','kind','time_precision']) text(value[key]);
    if (!['person','group'].includes(value.object_kind) || !['in','out','system','unknown'].includes(value.direction) || !['confirmed','failed','pending','unknown'].includes(value.platform_success)) fail();
    if (typeof value.body !== 'string' || value.body.length > 32768) fail();
    text(value.platform_message_key, 256, true);
    text(value.platform_correlation_key, 256, true);
    for (const key of ['occurred_lower_utc','occurred_upper_utc']) {
      if (value[key] != null && (typeof value[key] !== 'string' || !/^\d{4}-\d\d-\d\dT.*(?:Z|\+00:00)$/.test(value[key]) || !Number.isFinite(Date.parse(value[key])))) fail();
    }
    if (!plain(value.coverage) || JSON.stringify(value.coverage).length > 4096) fail();
    return JSON.parse(JSON.stringify(value));
  }
  function message(value, kind) {
    if (!plain(value) || typeof value.type !== 'string') fail();
    if (['status','capabilities'].includes(value.type)) { exact(value, ['type']); return value; }
    if (value.type === 'history') { exact(value, ['type','query']); return {type:value.type, query:history(value.query)}; }
    if (value.type === 'sidebar') { exact(value, ['type','query']); exact(value.query, ['account_key','person_key']); text(value.query.account_key); text(value.query.person_key); return value; }
    if (value.type === 'observation') { exact(value, ['type','observation']); return {type:value.type, observation:observation(value.observation)}; }
    if (kind === 'popup' && value.type === 'login') {
      exact(value, ['type','operator_key','device_key','credential']); text(value.operator_key); text(value.device_key); text(value.credential, 1024); return value;
    }
    if (kind === 'popup' && ['logout','retry'].includes(value.type)) { exact(value, ['type']); return value; }
    fail();
  }
  function selectionRequest(value) {return candidateRequest(value,'selection_token');}
  function resumeRequest(value) {return candidateRequest(value,'resume_token');}
  // Used by a trusted background producer only; deliberately no message type.
  function nativeProfileRequest(value) {
    const limits={source_id:200,source_version:80,owner_uid:256,sec_uid:256,platform_conversation_key:256};
    if(!plain(value))fail();
    const fields=Object.getOwnPropertyDescriptors(value),keys=Object.keys(limits);
    if(Reflect.ownKeys(fields).length!==keys.length)fail();
    const result={};
    for(const key of keys){
      const field=fields[key];
      // Only the opaque native conversation key gains numeric colon syntax.
      // The end assertion rejects trailing newlines (unlike JS $ by itself).
      const pattern=key==='platform_conversation_key'
        ? /^(?:[A-Za-z0-9_-]+|[0-9]+(?::[0-9]+)+)(?![\s\S])/ : /^[A-Za-z0-9_-]+$/;
      if(!field||!Object.hasOwn(field,'value')||typeof field.value!=='string'||
         !field.value.length||field.value.length>limits[key]||!pattern.test(field.value))fail();
      result[key]=field.value;
    }
    if(result.sec_uid==='self')fail();
    return Object.freeze(result);
  }
  function metadataFields(value,keys) {
    if(!plain(value))fail();const fields=Object.getOwnPropertyDescriptors(value);
    if(Reflect.ownKeys(fields).length!==keys.length||keys.some(k=>!fields[k]||!Object.hasOwn(fields[k],'value')))fail();
    return Object.fromEntries(keys.map(k=>[k,fields[k].value]));
  }
  function nativeMetadataScope(value,withDocument=false) {
    const keys=['context_id','generation','head_token','header_bound'];
    if(withDocument)keys.push('tab_id','document_id');
    const scope=metadataFields(value,keys);
    if(scope.header_bound!==true||!Number.isSafeInteger(scope.generation)||scope.generation<0||
      ['context_id','head_token',...(withDocument?['document_id']:[])].some(k=>typeof scope[k]!=='string'||!/^[A-Za-z0-9_-]{1,128}(?![\s\S])/.test(scope[k]))||
      withDocument&&(!Number.isSafeInteger(scope.tab_id)||scope.tab_id<0))fail();
    return Object.freeze(scope);
  }
  // Background-only locator: page identity fields cannot request approval.
  function nativeRegistrationRequest(value) {
    const request=metadataFields(value,['source_id','source_version','account_key']);
    for(const [key,limit] of [['source_id',200],['source_version',80],['account_key',256]])
      if(typeof request[key]!=='string'||request[key].length>limit||!/^[A-Za-z0-9_-]+(?![\s\S])/.test(request[key]))fail();
    if(!request.source_id.startsWith('native-reviewed-')||request.source_id==='native-reviewed-')fail();
    return Object.freeze(request);
  }
  function nativeRegistrationResponse(value,request,device) {
    const result=metadataFields(value,['format','state','reason','request_digest','registration','native_page_verification','context_authorization']);
    const hash=value=>typeof value==='string'&&/^[0-9a-f]{64}(?![\s\S])/.test(value);
    if(result.format!=='native-registration-status-1'||!hash(result.request_digest)||
      result.native_page_verification!=='background_required'||result.context_authorization!=='not_granted')fail();
    if(result.state==='unknown'){
      if(result.registration!==null||!['registration_not_qualified','registration_changed'].includes(result.reason))fail();
    }else if(result.state==='registered'){
      if(result.reason!==null)fail();
      const r=metadataFields(result.registration,['source_id','source_version','registration_digest','approval','native_device_key','query','binding']);
      if(r.source_id!==request.source_id||r.source_version!==request.source_version||r.native_device_key!==device||!hash(r.registration_digest))fail();
      const approval=metadataFields(r.approval,['source_id','source_version','registration_digest']);
      for(const [key,limit] of [['source_id',200],['source_version',80]])
        if(typeof approval[key]!=='string'||approval[key].length>limit||!/^[A-Za-z0-9_-]+(?![\s\S])/.test(approval[key]))fail();
      if(approval.source_id===request.source_id||approval.source_id.startsWith('native-reviewed-')||!hash(approval.registration_digest))fail();
      const query=nativeProfileRequest(r.query),binding=metadataFields(r.binding,['account_key','person_key','conversation_key']);
      if(query.source_id!==request.source_id||query.source_version!==request.source_version||binding.account_key!==request.account_key||
        ['owner_uid','sec_uid'].some(key=>!/^[A-Za-z0-9_-]+(?![\s\S])/.test(query[key]))||
        Object.values(binding).some(v=>typeof v!=='string'||!/^[A-Za-z0-9_-]{1,256}(?![\s\S])/.test(v)))fail();
      result.registration=Object.freeze({...r,approval:Object.freeze(approval),query,binding:Object.freeze(binding)});
    }else fail();
    // A point-in-time registration result, not an authorizer or lease.
    return Object.freeze(result);
  }
  function nativeMetadataRequest(value) {
    const request=metadataFields(value,['type','scope']);
    if(request.type!=='native-metadata-read')fail();
    return Object.freeze({type:request.type,scope:nativeMetadataScope(request.scope)});
  }
  function nativeMetadataResponse(value) {
    const response=metadataFields(value,['scope','metadata']);
    const metadata=metadataFields(response.metadata,['format','authority','owner_uid','sec_uid','platform_conversation_key']);
    if(!['native-store-metadata-1','native-bound-conversation-metadata-1','native-bound-context-data-1','native-panel-context-data-1'].includes(metadata.format)||metadata.authority!=='unverified'||
      typeof metadata.owner_uid!=='string'||!/^[0-9]{1,256}(?![\s\S])/.test(metadata.owner_uid)||
      typeof metadata.sec_uid!=='string'||!/^[A-Za-z0-9_-]{1,256}(?![\s\S])/.test(metadata.sec_uid)||metadata.sec_uid==='self'||
      typeof metadata.platform_conversation_key!=='string'||metadata.platform_conversation_key.length>256||
      !/^(?:[A-Za-z0-9_-]+|[0-9]+(?::[0-9]+)+)(?![\s\S])/.test(metadata.platform_conversation_key))fail();
    return Object.freeze({scope:nativeMetadataScope(response.scope),metadata:Object.freeze(metadata)});
  }
  function candidateRequest(value,tokenKey) {
    const keys=['context_id','generation','head_token','header_bound','sec_uid',tokenKey];
    if(!plain(value))fail();const fields=Object.getOwnPropertyDescriptors(value);
    if(Reflect.ownKeys(fields).length!==keys.length||keys.some(k=>!fields[k]||!Object.hasOwn(fields[k],'value')))fail();
    if(typeof value.context_id!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(value.context_id)||!Number.isSafeInteger(value.generation)||value.generation<0||
      typeof value.header_bound!=='boolean'||typeof value.head_token!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(value.head_token)||
      typeof value.sec_uid!=='string'||!/^[A-Za-z0-9_-]{1,256}$/.test(value.sec_uid)||value.sec_uid==='self'||
      typeof value[tokenKey]!=='string'||!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value[tokenKey]))fail();
    return {...value};
  }
  function url(path, query = {}) {
    if (!['/api/v1/profile/editor','/api/v1/profile/field','/api/v1/groups/membership','/api/v1/health','/api/v1/capabilities','/api/v1/auth/startup','/api/v1/auth/logout','/api/v1/auth/pairing/status','/api/v1/auth/pairing/redeem','/api/v1/auth/pairing/challenge','/api/v1/auth/pairing/session','/api/v1/observations','/api/v1/history','/api/v1/sidebar','/api/v1/native/profile','/api/v1/native/registration-status','/api/v1/native/runtime-resolve','/api/v1/identity-pilot/status','/api/v1/identity-pilot/lookup','/api/v1/identity-pilot/pair','/api/v1/identity-pilot/device-challenge','/api/v1/identity-pilot/device-session'].includes(path)) fail();
    const u = new URL(BASE + path);
    for (const [key,value] of Object.entries(query)) if (value !== undefined && value !== null && value !== '') u.searchParams.set(key, String(value));
    return u.href;
  }
  return { BASE, senderKind, chatURL, message, history, observation, selectionRequest, resumeRequest, nativeProfileRequest, nativeRegistrationRequest, nativeRegistrationResponse, nativeMetadataScope, nativeMetadataRequest, nativeMetadataResponse, url };
});
