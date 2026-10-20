'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {createApi,BASE}=require('../web/js/api.js');
const reply=data=>({ok:true,text:async()=>JSON.stringify({schema_version:2,data})});
const session=()=>({token:'F'.repeat(43),principal:{operator_key:'fixture-manager',device_key:'fixture-device',role:'manager',account_keys:['fixture-account'],scopes:['capabilities:read','history:read']}});
test('code login posts only code and keeps session only in this API instance',async()=>{
  const calls=[];const api=createApi(async(url,options)=>{calls.push({url,options});return reply(url.endsWith('web-login')?session():{});});
  const principal=await api.loginWithCode('A'.repeat(43));assert.equal(principal.role,'manager');
  assert.equal(calls[0].url,BASE+'/api/v1/auth/web-login');assert.deepEqual(JSON.parse(calls[0].options.body),{code:'A'.repeat(43)});
  assert.equal(calls[0].options.headers['X-DSH-Schema'],'2');assert.equal(calls[0].options.credentials,'omit');
  assert.equal(calls[0].options.redirect,'error');await api.capabilities();assert.ok(calls[1].options.headers.Authorization);
  await assert.rejects(()=>createApi().capabilities(),/unauthorized/);
  await api.logout();await assert.rejects(()=>api.capabilities(),/unauthorized/);
});
test('logout while code response is pending revokes returned token without establishing session',async()=>{
  let resolve;const pending=new Promise(r=>resolve=r);const calls=[];
  const api=createApi(async(url,options)=>{calls.push({url,options});return url.endsWith('web-login')?pending:reply({});});
  const login=api.loginWithCode('A'.repeat(43));await api.logout();resolve(reply(session()));
  await assert.rejects(()=>login,/stale_session/);assert.ok(calls[1].url.endsWith('auth/logout'));
  await assert.rejects(()=>api.capabilities(),/unauthorized/);
});
test('bad code does not send and consumed code has specific recoverable status',async()=>{
  let calls=0;const api=createApi(async()=>{calls++;return {ok:false,status:410};});
  await assert.rejects(()=>api.loginWithCode('bad'),/invalid_login_code/);assert.equal(calls,0);
  await assert.rejects(()=>api.loginWithCode('A'.repeat(43)),/web_login_expired/);assert.equal(calls,1);
});
test('invalid authority response is revoked rather than accepted',async()=>{
  const calls=[];const broken=session();broken.principal.role='unknown';
  const api=createApi(async(url)=>{calls.push(url);return reply(url.endsWith('web-login')?broken:{});});
  await assert.rejects(()=>api.loginWithCode('A'.repeat(43)),/bad_response/);
  assert.ok(calls[1].endsWith('auth/logout'));await assert.rejects(()=>api.capabilities(),/unauthorized/);
});
