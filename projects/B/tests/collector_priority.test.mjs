import test from 'node:test';
import assert from 'node:assert/strict';
import {Collector} from '../collector-extension/engine.js';
import {scalar} from '../collector-extension/core.js';

const scope={domains:['account','capability','session_list','session','summary','rank','profile','trend','comment'],start_date:'2026-01-01',end_date:'2026-01-01',include_body:false,retention_days:7};
const options={anchorTab:1,readAll:true,skipTests:true};
function fixture({failBase=false,blockDetail=false,differentUsers=false}={}){
 const items=[],calls=[],events=[];let finishReason,release,entered;const detailEntered=new Promise(r=>entered=r);
 const host={call:async(op,a)=>{
  if(op==='start')return {run_id:'a'.repeat(32),generation:'b'.repeat(32)};
  if(op==='append'){items.push(...a.items);return {total:items.length};}
  if(op==='finish'){finishReason=a.reason;return {total:items.length};}
 }};
 const source={read:async(_tab,t)=>{
  calls.push(t);
  if(failBase&&t.op==='base'&&t.room==='002')throw Error('SOURCE_TIMEOUT');
  if(blockDetail&&t.op==='profile'){entered();await new Promise(r=>release=r);}
  const rows=t.op==='account'?[{user_id_str:scalar('0001')}]:t.op==='history'?['001','002'].map(id=>({roomID:scalar(id),serverWatchUcntTdDirect:scalar(20)})):
   t.op==='base'?[{room_id:scalar(t.room),anchor_id:scalar('0001'),start_time:scalar('2026-01-01 10:00:00'),end_time:scalar('2026-01-01 10:12:00')}]:
   t.op==='rank'?(differentUsers&&t.room==='002'?['0008','0009']:['0009']).map(id=>({'user.id_str':scalar(id),value:scalar('10')})):[{value:scalar('fictional')}];
  return {ok:true,op:t.op,owner:scalar('0001'),path:'/fixture',rows};
 }};
 const c=new Collector({host,source,delay:async()=>{},notify:e=>events.push(e)});
 return {c,items,calls,events,detailEntered,release:()=>release?.(),reason:()=>finishReason};
}
test('all sessions main records are saved before first slow detail; IDs remain per session',async()=>{
 const f=fixture();const epoch=await f.c.start(scope,options);await f.c.capture(epoch);
 const first=f.calls.findIndex(t=>t.op==='profile');
 for(const room of ['001','002'])for(const op of ['base','summary','trend'])assert.ok(f.calls.findIndex(t=>t.room===room&&t.op===op)<first);
 assert.deepEqual(f.calls.filter(t=>t.op==='profile').map(t=>[t.room,t.user]),[['001','0009'],['002','0009']]);
 assert.equal(f.calls.filter(t=>t.op==='content').length,26); // Existing inclusive end-label segmentation retained.
 assert.equal(f.reason(),'completed_requested_scope');assert.equal(f.events.filter(e=>e.kind==='main_ready').length,1);
});
test('slow first detail cannot hold second session main data hostage; cancellation rejects late detail',async()=>{
 const f=fixture({blockDetail:true});const epoch=await f.c.start(scope,options);const pending=f.c.capture(epoch);await f.detailEntered;
 assert.equal(f.items.filter(i=>i.domain==='trend').length,2);assert.ok(f.c.run);assert.equal(f.items.filter(i=>i.domain==='profile').length,0);
 await f.c.stop();f.release();await pending;assert.equal(f.reason(),'cancelled');assert.equal(f.items.filter(i=>i.domain==='profile').length,0);
});
test('failed main base remains missing, never queues unbound details or claims main complete',async()=>{
 const f=fixture({failBase:true});const epoch=await f.c.start(scope,options);await f.c.capture(epoch);
 assert.equal(f.events.find(e=>e.kind==='main_ready').missing,1);assert.equal(f.reason(),'failed');
 assert.equal(f.calls.some(t=>t.room==='002'&&['profile','content'].includes(t.op)),false);
 assert.ok(f.items.every(i=>i.coverage.status!=='complete'));
});
test('main-only selection never starts detail stage or new unselected source requests',async()=>{
 const f=fixture();const epoch=await f.c.start({...scope,domains:scope.domains.filter(d=>!['profile','comment'].includes(d))},options);await f.c.capture(epoch);
 assert.equal(f.calls.some(t=>['profile','content'].includes(t.op)),false);assert.equal(f.events.some(e=>e.stage==='details'),false);
});
test('profile-only details reset each session estimate even for consecutive profiles',async()=>{
 const f=fixture({differentUsers:true});const phaseSizes=[];const original=f.c.phaseStart.bind(f.c);
 f.c.phaseStart=(op,total,label)=>{if(op==='profile')phaseSizes.push(total);original(op,total,label);};
 const epoch=await f.c.start({...scope,domains:scope.domains.filter(d=>d!=='comment')},options);await f.c.capture(epoch);
 assert.deepEqual(phaseSizes,[1,2]);assert.equal(f.c.phase.total,2);assert.equal(f.c.phase.done,2);
});
test('small offline order comparison uses identical request inventory, no speed or coverage assumptions',async t=>{
 const f=fixture();const epoch=await f.c.start(scope,options);await f.c.capture(epoch);
 // One equal unit per source operation, excluding storage/network variability.
 const isMain=x=>!['content','profile'].includes(x.op);
 const lead=f.calls.filter(x=>!x.room),perRoom=id=>f.calls.filter(x=>x.room===id);
 const oldOrder=[...lead,...perRoom('001'),...perRoom('002')];
 const lastMain=xs=>xs.findLastIndex(isMain)+1;
 assert.equal(oldOrder.length,f.calls.length);assert.ok(lastMain(f.calls)<lastMain(oldOrder));
 t.diagnostic(JSON.stringify({fictional:true,sourceReads:0,totalOperations:f.calls.length,previousMainReadyStep:lastMain(oldOrder),newMainReadyStep:lastMain(f.calls),allDetailsRetained:true}));
});
