'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const H=require('../extension/identity/history_summary.js');
const {mount}=require('../extension/ui/sidebar.js');
const summary={format:'historical-sender-summary-1',state:'available',scope:'export_sender_all_contexts',coverage:'qualified_root_sender_pairs_only',message_count:8,conversation_count:2,private_message_count:null,owner_verified:false,private_context_verified:false,formal_identity_match:false};
function fixture(){const nodes=[];const create=()=>{const n={textContent:'',attrs:{},setAttribute(k,v){this.attrs[k]=v;},append(){},attachShadow(){return create();},addEventListener(){},removeEventListener(){},remove(){}};nodes.push(n);return n;};const side=mount({createElement:create,body:create()},{verified:false});return {side,nodes,text:()=>nodes.map(n=>n.textContent).join('\n'),host:nodes.find(n=>n.attrs['data-dsh-owned'])};}
const view={state:'selection_candidate',historical_identity:'sender_candidate',current_identity:'unverified',owner:'unverified',private_context:'unverified',formal_binding:false,history_summary:summary};
test('history DTO rejects getters extra private fields fake counts and identity upgrades',()=>{
 assert.deepEqual(H.normalize(summary,true),summary);assert.equal(H.normalize(summary,false),null);
 for(const patch of [{message_count:0},{message_count:true},{message_count:1,conversation_count:2},{conversation_count:-1},{private_message_count:8},{owner_verified:true},{body:'FICTIONAL_PRIVATE_BODY'},{state:'unknown'}])assert.equal(H.normalize({...summary,...patch},true),null);
 let calls=0;const getter={...summary};Object.defineProperty(getter,'message_count',{get(){calls++;throw Error('FICTIONAL_PRIVATE_ERROR');}});assert.equal(H.normalize(getter,true),null);
 const response={};Object.defineProperty(response,'history_summary',{get(){calls++;throw Error('FICTIONAL_PRIVATE_ERROR');}});assert.equal(H.fromResponse(response,true),null);assert.equal(calls,0);
});
test('sidebar shows qualified export records and clears old counts on switch unknown conflict or disconnect',()=>{
 const f=fixture();
 for(const state of ['candidate','selection_candidate','resumed_candidate','recalled_candidate']){
  f.side.showIdentityPilot({...view,state});assert.equal(f.host.attrs['data-dsh-history-summary'],'available');assert.ok(f.text().includes('有效记录：8条，涉及2个会话'));assert.ok(f.text().includes('不等于私聊次数'));assert.ok(f.text().includes('未核验完整性及去重'));
 }
 for(const patch of [{state:'pending'},{historical_identity:'zero'},{historical_identity:'conflict'},{connection:'service_unavailable'},{formal_binding:true},{owner:'verified'},{history_summary:undefined},{history_summary:{...summary,body:'FICTIONAL_PRIVATE_BODY'}}]){
  f.side.showIdentityPilot({...view,...patch});assert.equal(f.host.attrs['data-dsh-history-summary'],'unknown');assert.ok(!f.text().includes('有效记录：8条'));assert.ok(f.text().includes('不按0条计算'));assert.ok(!f.text().includes('FICTIONAL_PRIVATE_BODY'));
 }
 f.side.showIdentityPilot(view);f.side.showIdentityPilot({...view,history_summary:{...summary,message_count:3,conversation_count:1}});assert.ok(f.text().includes('有效记录：3条'));assert.ok(!f.text().includes('有效记录：8条'));
});
