'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/js/api.js'),'utf8');
const envelope=data=>({ok:true,text:async()=>JSON.stringify({schema_version:2,data})});

test('protected AI requests wait within the explicit budget and abort never retries a POST',async()=>{
  const timers=[],calls=[];
  const sandbox={module:{exports:{}},URL,AbortController,TextEncoder,
    setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout:()=>{}};
  vm.runInNewContext(source,sandbox);
  const api=sandbox.module.exports.createApi(async(url,options)=>{
    calls.push({url,options});
    if(url.endsWith('auth/startup'))return envelope({token:'fixture',principal:{operator_key:'o',device_key:'d',account_keys:['A']}});
    return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(Error('aborted'),{name:'AbortError'}))));
  });
  await api.login('o','d','fixture');assert.equal(timers.at(-1).delay,8000);
  for(const [method,data] of [['aiStatus',{account_key:'A',person_key:'P'}],['aiJob',{account_key:'A',job_key:'J'}],
    ['aiRequest',{account_key:'A',person_key:'P',purpose:'reply',on_demand:true}]]){
    const waiting=api[method](data);assert.equal(timers.at(-1).delay,120000);
    const rejected=assert.rejects(waiting,/ai_wait_timeout/);timers.at(-1).fn();await rejected;
  }
  assert.equal(calls.length,4);assert.equal(calls.filter(x=>x.url.endsWith('/ai/request')).length,1);
  const waiting=api.profileField({account_key:'A',person_key:'P',field:'note',value:'fixture',expected_revision:[],clear:false});
  assert.equal(timers.at(-1).delay,30000);const rejected=assert.rejects(waiting,/aborted/);timers.at(-1).fn();await rejected;
});

test('reply availability does not expand note editing or general writes',async()=>{
  const sandbox={module:{exports:{}},URL,AbortController,TextEncoder,setTimeout,clearTimeout};vm.runInNewContext(source,sandbox);
  const api=sandbox.module.exports.createApi(async url=>envelope(url.endsWith('auth/startup')?
    {token:'fixture',principal:{operator_key:'o',device_key:'d',account_keys:['A'],scopes:['profile:write']}}:
    url.endsWith('health')?{writes_enabled:false,local_notes_state:'explicit_local_notes'}:
    {local_access:{state:'explicit_local_notes',reply_on_demand:true,profile_fields:['note'],account_key:'A'}}));
  await api.login('o','d','fixture');await api.health();assert.equal(api.editingAccess('A','note').allowed,true);
  assert.equal(api.editingAccess('A','display_name').allowed,false);assert.equal(api.editingAccess('other','note').allowed,false);
});
