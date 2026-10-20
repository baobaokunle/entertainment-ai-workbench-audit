import test from 'node:test';
import assert from 'node:assert/strict';
import {Collector} from '../collector-extension/engine.js';
import {scalar} from '../collector-extension/core.js';
import {prepareBaseRepair} from '../collector-extension/repair.js';
const owner=scalar('00000001'),app=scalar('fictional-app');
function fixture({otherAccount=false,otherApp=false,badRoom=false,hold=false,deny=false}={}){
 const items=[],reads=[];let reason,entered,release;
 const waiting=new Promise(r=>entered=r);
 const scope={domains:['account','capability','session'],start_date:'2026-01-01',end_date:'2026-01-02',include_body:false,retention_days:7};
 const host={call:async(op,args)=>{
  if(op==='repair_start')return {run_id:'a'.repeat(32),generation:'b'.repeat(32),scope,owner,app,target_count:2};
  if(op==='repair_targets')return {rooms:['001','002'],next_offset:2,total:2};
  if(op==='append'){items.push(...args.items);return {total:items.length};}
  if(op==='finish'){reason=args.reason;return {total:items.length};}
  throw Error('UNEXPECTED_OPERATION');
 }};
 const source={read:async(tab,t)=>{
  reads.push(t);
  if(t.op==='base'&&hold){entered();await new Promise(r=>release=r);}
  if(t.op==='base'&&deny)throw Object.assign(Error('SOURCE_PERMISSION'),{detail:'HTTP_403'});
  return {owner:otherAccount?scalar('00000002'):owner,app:otherApp?scalar('different-app'):app,path:t.op==='account'?'/passport/account/info/v2/':'/fixture/room_base_v2',rows:t.op==='account'?[{user_id_str:owner,app_id:app}]:[{room_id:scalar(badRoom?'999':t.room),anchor_id:owner}]};
 }};
 const c=new Collector({host,source,delay:async()=>{}});
 return {c,items,reads,waiting,release:()=>release?.(),reason:()=>reason};
}
test('repair reads only account and exact planned bases, never repeats history or details',async()=>{
 const f=fixture(),e=await f.c.startBaseRepair('p');await f.c.captureBaseRepair(e,1);
 assert.deepEqual(f.reads.map(t=>t.op),['account','base','base']);assert.deepEqual(f.reads.filter(t=>t.room).map(t=>t.room),['001','002']);
 assert.ok(f.reads.every(t=>t.includeBody===false));assert.equal(f.items.filter(x=>x.domain==='session').length,2);assert.equal(f.reason(),'completed_requested_scope');
});
test('owner or application mismatch stops before any base read',async()=>{
 for(const config of [{otherAccount:true},{otherApp:true}]){const f=fixture(config),e=await f.c.startBaseRepair('p');await f.c.captureBaseRepair(e,1);assert.equal(f.reads.length,1);assert.equal(f.items.length,0);assert.equal(f.reason(),'source_changed');}
});
test('wrong room never becomes successful base data',async()=>{
 const f=fixture({badRoom:true}),e=await f.c.startBaseRepair('p');await f.c.captureBaseRepair(e,1);
 assert.equal(f.items.filter(x=>x.domain==='session').length,0);assert.equal(f.items.filter(x=>x.domain==='capability').length,2);assert.equal(f.reason(),'failed');
});
test('cancelled in-flight base is not saved or followed by a second read',async()=>{
 const f=fixture({hold:true}),e=await f.c.startBaseRepair('p'),running=f.c.captureBaseRepair(e,1);await f.waiting;await f.c.stop();f.release();await running;
 assert.equal(f.items.length,1);assert.equal(f.reads.length,2);assert.equal(f.reason(),'cancelled');
});
test('permission rejection stops remaining planned bases',async()=>{
 const f=fixture({deny:true}),e=await f.c.startBaseRepair('p');await f.c.captureBaseRepair(e,1);assert.equal(f.reads.length,2);assert.equal(f.reason(),'failed');
});
test('cancelled scan discards the late plan and never starts a capture',async()=>{
 const calls=[];let cancelled=false;
 const host={call:async(op,args)=>{calls.push(op);if(op==='repair_scan'){cancelled=true;return {plan_id:'a'.repeat(32),next_offset:1,total:2,done:false};}return {};}};
 await assert.rejects(prepareBaseRepair(host,'p',{check:()=>{if(cancelled)throw Error('CANCELLED');}}),/CANCELLED/);assert.deepEqual(calls,['repair_scan','repair_discard']);
});
test('scan rejects stalled cursors without producing a retry loop',async()=>{
 const calls=[];const host={call:async op=>{calls.push(op);return {plan_id:'a'.repeat(32),next_offset:0,total:2,done:false};}};
 await assert.rejects(prepareBaseRepair(host,'p'),/REPAIR_CURSOR/);assert.deepEqual(calls,['repair_scan','repair_discard']);
});
