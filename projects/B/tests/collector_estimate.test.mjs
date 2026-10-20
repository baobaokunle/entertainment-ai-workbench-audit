import test from 'node:test';
import assert from 'node:assert/strict';
import {estimateProgress} from '../collector-extension/estimate.js';
import {Collector} from '../collector-extension/engine.js';
const seed={started:1000,now:11000,calls:5,data:200,running:true,phase:{label:'gift',total:10,done:4,started:3000,initialData:120}};
test('estimate uses only current known phase; never declares whole task size or time',()=>{
 const e=estimateProgress(seed);assert.equal(e.remainingReads,6);assert.equal(e.remainingMs,12000);assert.equal(e.remainingData,120);assert.equal(e.wholeTaskRemainingMs,null);assert.equal(e.wholeTaskDataTotal,null);
});
test('unknown phase size, small sample and stopped run do not get ETA',()=>{
 for(const override of [{phase:{...seed.phase,total:null}},{phase:{...seed.phase,done:2}},{running:false}])assert.equal(estimateProgress({...seed,...override}).remainingMs,null);
});
test('new phase excludes data acquired earlier; empty phase forecasts zero with no completeness claim',()=>{
 const e=estimateProgress({...seed,data:120});assert.equal(e.remainingData,0);assert.equal(e.wholeTaskDataTotal,null);
});
test('cancelled read cannot advance old or replacement phase',async()=>{
 let reject,entered;const ready=new Promise(r=>entered=r),c=new Collector({clock:()=>1000,host:{call:async op=>op==='start'?{run_id:'a',generation:'b'}:{total:0}},source:{read:async()=>{entered();return new Promise((_r,j)=>reject=j);}},delay:async()=>{}});
 const epoch=await c.start({start_date:'2026-01-01',end_date:'2026-01-01',include_body:false},{readAll:true});c.phaseStart('history',10);const phase=c.phase;
 const result=c.read(1,{op:'history'},epoch).catch(()=>{});await ready;await c.stop();reject(Error('SOURCE_READ_FAILED'));await result;assert.equal(phase.done,0);assert.equal(c.progressEstimate().remainingMs,null);
});
