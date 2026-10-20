'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {createApi}=require('../web/js/api.js');
const reply=data=>({ok:true,text:async()=>JSON.stringify({schema_version:2,data})});
const session=()=>({token:'F'.repeat(43),principal:{operator_key:'fixture-manager',device_key:'fixture-device',role:'manager',account_keys:['fixture-account'],scopes:['profile:write','capabilities:read']}});
const health={writes_enabled:false,read_only_rollback:false,local_notes_state:'explicit_local_notes'};
const capability={state:'explicit_local_notes',account_key:'fixture-account',profile_fields:['note'],reply_on_demand:false};
test('only authenticated precise note capability enables note, never other fields or other accounts',async()=>{
  let selected=capability;const api=createApi(async url=>reply(url.endsWith('web-login')?session():url.endsWith('health')?health:{local_access:selected}));
  await api.health();assert.equal(api.editingAccess('fixture-account','note').allowed,false);
  await api.loginWithCode('A'.repeat(43));await api.health();assert.equal(api.editingAccess('fixture-account','note').allowed,true);
  for(const field of [undefined,'display_name','formal_tag.clip','birthday'])assert.equal(api.editingAccess('fixture-account',field).allowed,false);
  assert.equal(api.editingAccess('other-account','note').allowed,false);
  selected={...capability,profile_fields:['note','display_name']};await api.health();assert.equal(api.editingAccess('fixture-account','note').allowed,false);
  selected={state:'unavailable',profile_fields:[],reply_on_demand:false};await api.health();assert.equal(api.editingAccess('fixture-account','note').allowed,false);
});
test('late capability cannot restore access after logout',async()=>{
  let release;const pending=new Promise(r=>release=r);
  const api=createApi(async url=>url.endsWith('capabilities')?pending:reply(url.endsWith('web-login')?session():url.endsWith('health')?health:{}));
  await api.loginWithCode('A'.repeat(43));const loading=api.health();
  await new Promise(r=>setImmediate(r));await api.logout();release(reply({local_access:capability}));
  await assert.rejects(()=>loading,/stale_session/);assert.equal(api.editingAccess('fixture-account','note').allowed,false);
});
test('readonly and missing profile scope override a valid note declaration',async()=>{
  for(const readonly of [true,false]){
    const s=session();if(!readonly)s.principal.scopes=['capabilities:read'];
    const api=createApi(async url=>reply(url.endsWith('web-login')?s:url.endsWith('health')?{...health,read_only_rollback:readonly}:{local_access:capability}));
    await api.loginWithCode('A'.repeat(43));await api.health();assert.equal(api.editingAccess('fixture-account','note').allowed,false);
  }
});
