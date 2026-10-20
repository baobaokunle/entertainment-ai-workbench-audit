// Existing encrypted queue only. Reuse the frozen source reader and projection.
import {Collector} from './engine.js';
import {scalar,capability} from './core.js';
export const GAP_CLIENT='b-gap-client/1';
const same=(a,b)=>a&&b&&a.type===b.type&&a.lossless===true&&b.lossless===true&&a.value===b.value;
const fixed=e=>/^[A-Z_]{1,80}$/.test(e?.message)?e.message:'SOURCE_READ_FAILED';
export class GapCollector {
 constructor({host,source,notify=()=>{},delay=ms=>new Promise(r=>setTimeout(r,ms))}){
  Object.assign(this,{host,source,notify,delay});this.run=null;this.epoch=0;this.busy=false;this.calls=0;
 }
 check(epoch){if(epoch!==this.epoch||!this.busy)throw Error('CANCELLED');}
 disconnected(){this.epoch++;this.run=null;this.busy=false;}
 async stop(){
  this.epoch++;const run=this.run;this.run=null;
  // Revoke generation before any late response can be staged or committed.
  try{if(run)await this.host.call('gap_finish',{...run,reason:'cancelled'});}finally{this.busy=false;this.notify({kind:'stopped'});}
 }
 async capture(runId,prepare){
  if(this.busy)throw Error('TASK_ALREADY_RUNNING');
  this.busy=true;this.calls=0;const epoch=++this.epoch;let reason='completed_requested_scope';
  try{
   const active=await this.host.call('gap_open',{run_id:runId,client:GAP_CLIENT});
   const owned={run_id:active.run_id,generation:active.generation};
   if(epoch!==this.epoch){await this.host.call('gap_finish',{...owned,reason:'cancelled'});throw Error('CANCELLED');}
   this.run=owned;
   const tab=await prepare(()=>this.check(epoch));this.check(epoch);
   let phase=null;
   for(;;){
    this.check(epoch);const op=await this.host.call('gap_claim',owned);this.check(epoch);if(!op)break;
    if(phase!==op.phase){phase=op.phase;this.notify({kind:'phase',phase});}
    await this.delay(600);this.check(epoch);
    let data,items=[],error=null;
    try{data=await this.source.read(tab,op.task);}catch(e){error=e;}
    this.check(epoch);
    if(error){
     const code=fixed(error);if(code==='SOURCE_CHANGED'||code==='SOURCE_ORIGIN')throw error;
     const item=capability(op.domain,code,{origin:'https://anchor.douyin.com',path:'/adapter-capability',transport:'capability'},{owner:active.owner,app_scope:active.app,...op.context});
     for(const k of ['operation','stage','phase','http_status','exception']){const v=error.diagnostic?.[k];if(v!==null&&v!==undefined)item.fields['b_diagnostic.'+k]=scalar(v);}
     items=[item];
    }else{
     if(!same(data.owner,active.owner)||!same(data.app,active.app))throw Error('SOURCE_CHANGED');
     if(op.domain==='profile')data={...data,rows:data.rows.map(r=>({...r,'b_request.user_id':scalar(op.task.user)}))};
     const collector=new Collector({});collector.save=async rows=>{items=rows;};
     const issues=['GAP_REPAIR_NEW_OBSERVATION','SOURCE_TIMEZONE_UNKNOWN'];
     if(op.domain==='profile')issues.push('REQUEST_USER_ID_NOT_INDEPENDENT_IDENTITY','PROFILE_PERIODS_IN_FIELD_NAMES','EXACT_WINDOW_UNKNOWN');
     if(op.task.op==='content')issues.push('REQUEST_BOUNDARIES_NOT_COVERAGE','EVENT_DEDUP_UNVERIFIED');
     await collector.rows(data,op.domain,op.context,0,issues);
    }
    this.check(epoch);let offset=0;
    while(offset<items.length){
     this.check(epoch);const batch=[];let bytes=0;
     while(offset+batch.length<items.length&&batch.length<50){const item=items[offset+batch.length],n=new TextEncoder().encode(JSON.stringify(item)).length;if(bytes+n>650000)break;batch.push(item);bytes+=n;}
     if(!batch.length)throw Error('OBSERVATION_SIZE');
     await this.host.call('gap_part',{...owned,op_id:op.id,attempt:op.attempt,offset,items:batch});this.check(epoch);offset+=batch.length;
    }
    this.check(epoch);const saved=await this.host.call('gap_commit',{...owned,op_id:op.id,attempt:op.attempt,count:items.length});this.check(epoch);
    this.calls++;this.notify({kind:'progress',calls:this.calls,records:saved.total,state:saved.state});
    if(saved.must_stop||error&&(op.domain==='account'||['SOURCE_PERMISSION','HTTP_401','HTTP_403'].includes(fixed(error))||[401,403,429].includes(error.diagnostic?.http_status)))throw Error('BACKEND_ACCESS_BLOCKED');
   }
  }catch(e){
   if(e.message==='CANCELLED')return;
   reason=e.message==='SOURCE_CHANGED'?'source_changed':'failed';this.notify({kind:'error',code:fixed(e)});
  }finally{
   if(epoch===this.epoch){
    const run=this.run;this.run=null;
    try{if(run)await this.host.call('gap_finish',{...run,reason});}
    finally{this.busy=false;this.notify({kind:'finished',reason});}
   }
  }
 }
}
