import {uuid,scalar,raw,observation,capability,dateDays,segments,legacyRecord,parseLossless,localDate,sessionSkipReasons} from './core.js';
import {sourceRead} from './page-read.js';
import {SOURCE_PAGES} from './sources.js';
import {estimateProgress} from './estimate.js';
const sourceChanged=detail=>Object.assign(Error('SOURCE_CHANGED'),{detail});
const pageBoundary=url=>{const u=new URL(url);return u.origin+u.pathname;};
const emptyCounts=()=>({data:0,account:0,diagnostic:0,empty:0});
const sameScalar=(a,b)=>!!a&&!!b&&a.type===b.type&&a.lossless===true&&b.lossless===true&&a.value===b.value;
const cleanDiagnostic=d=>{
 if(!d||typeof d!=='object')return null;
 return {operation:['account','history','base','summary','rank','content','trend','profile','unknown'].includes(d.operation)?d.operation:'unknown',
 stage:['account','data'].includes(d.stage)?d.stage:null,phase:['fetch','redirect','status','body','decode','projection'].includes(d.phase)?d.phase:null,
 http_status:Number.isInteger(d.http_status)&&d.http_status>=0&&d.http_status<=599?d.http_status:null,
 exception:['TypeError','TimeoutError','AbortError','SyntaxError','Error','other'].includes(d.exception)?d.exception:'other'};
};

export class NativeClient {
  constructor(api=chrome) {this.api=api;this.pending=new Map();this.port=null;this.onDisconnect=()=>{};}
  async connect() {
    if(this.port)return;
    this.port=this.api.runtime.connectNative('com.bline.collector');
    this.port.onMessage.addListener(m=>{const pending=this.pending.get(m.id);if(pending){clearTimeout(pending.timer);this.pending.delete(m.id);m.ok?pending.resolve(m.result):pending.reject(Error(m.error||'NATIVE_FAILED'));}});
    this.port.onDisconnect.addListener(()=>{void this.api.runtime.lastError;this.port=null;for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(Error('NATIVE_DISCONNECTED'));}this.pending.clear();this.onDisconnect();});
    return this.call('hello',{});
  }
  call(op,args) {
    if(!this.port)return Promise.reject(Error('NATIVE_DISCONNECTED'));
    return new Promise((resolve,reject)=>{const id=uuid(),timer=setTimeout(()=>{this.pending.delete(id);reject(Error('NATIVE_TIMEOUT'));this.port?.disconnect();},60000);this.pending.set(id,{resolve,reject,timer});this.port.postMessage({protocol:'b-native/1',id,op,args});});
  }
}

export class BrowserSource {
  constructor(api=chrome){this.api=api;this.bindings=new Map();}
  async read(tabId,task) {
    const tab=await this.api.tabs.get(tabId);const url=new URL(tab.url||'about:blank');
    const expected=task.op.startsWith('chat_')?'https://www.douyin.com':'https://anchor.douyin.com';
    if(url.origin!==expected)throw Error('SOURCE_ORIGIN');
    const fixed=SOURCE_PAGES[task.op.startsWith('chat_')?'chat':'anchor'];
    if(pageBoundary(tab.url)!==pageBoundary(fixed))throw sourceChanged('PAGE_ROUTE_CHANGED');
    const bound=this.bindings.get(tabId);
    if(bound&&bound.boundary!==pageBoundary(tab.url))throw sourceChanged('PAGE_ROUTE_CHANGED');
    const target=bound?{tabId,documentIds:[bound.documentId]}:{tabId,frameIds:[0]};
    const results=await this.api.scripting.executeScript({target,world:'MAIN',func:sourceRead,args:[task]});
    const result=results[0];const after=await this.api.tabs.get(tabId);
    if(results.length!==1||result.frameId!==0||!result.documentId||(bound&&result.documentId!==bound.documentId))throw sourceChanged('PAGE_DOCUMENT_CHANGED');
    if(pageBoundary(after.url)!==pageBoundary(tab.url))throw sourceChanged('PAGE_ROUTE_CHANGED');
    this.bindings.set(tabId,{boundary:pageBoundary(tab.url),documentId:result.documentId});
    const data=result.result;
    if(!data?.ok)throw Object.assign(Error(data?.error||'SOURCE_READ_FAILED'),{detail:['ACCOUNT_CHANGED','CONVERSATION_CHANGED','HTTP_401','HTTP_403'].includes(data?.detail)?data.detail:null,requestStage:['account','data'].includes(data?.requestStage)?data.requestStage:null,diagnostic:cleanDiagnostic(data?.diagnostic)});
    if(!Array.isArray(data.rows)||data.rows.length>50000)throw Error('SOURCE_SHAPE');
    return data;
  }
}

export class Collector {
  constructor({host,source,notify=()=>{},delay=ms=>new Promise(r=>setTimeout(r,ms)),clock=()=>performance.now()}) {
    Object.assign(this,{host,source,notify,delay,clock});this.run=null;this.epoch=0;this.total=0;this.calls=0;this.missing=0;this.counts=emptyCounts();this.stopping=false;this.started=0;this.ended=null;this.phase=null;
  }
  phaseStart(op,total=null,label=op){this.phase={op,label,total,done:0,started:this.clock(),initialData:this.counts.data};}
  progressEstimate(){return estimateProgress({started:this.started,now:this.ended??this.clock(),calls:this.calls,data:this.counts.data,phase:this.phase,running:!!this.run&&!this.stopping});}
  check(epoch){if(!this.run||epoch!==this.epoch||this.stopping)throw Error('CANCELLED');}
  async start(scope,options) {
    if(this.run)throw Error('TASK_ALREADY_RUNNING');
    dateDays(scope.start_date,scope.end_date);
    if(options.readAll!==true&&(!Number.isInteger(options.maxCalls)||options.maxCalls<1||options.maxCalls>10000))throw Error('REQUEST_BUDGET');
    this.scope=scope;this.options=options;this.total=0;this.calls=0;this.missing=0;this.counts=emptyCounts();this.stopping=false;
    this.started=this.clock();this.ended=null;this.phase=null;
    const started=await this.host.call('start',{scope});this.run={run_id:started.run_id,generation:started.generation};this.epoch++;return this.epoch;
  }
  async startBaseRepair(plan_id) {
    if(this.run)throw Error('TASK_ALREADY_RUNNING');
    const started=await this.host.call('repair_start',{plan_id});
    this.scope=started.scope;this.options={readAll:true};this.repair=started;
    this.total=0;this.calls=0;this.missing=0;this.counts=emptyCounts();this.stopping=false;
    this.started=this.clock();this.ended=null;this.phase=null;
    this.run={run_id:started.run_id,generation:started.generation};this.epoch++;return this.epoch;
  }
  async captureBaseRepair(epoch,tab) {
    let reason='completed_requested_scope';
    try {
      this.notify({kind:'stage',stage:'base_repair'});
      const account=await this.read(tab,{op:'account'},epoch);
      if(!sameScalar(account.owner,this.repair.owner)||!sameScalar(account.app,this.repair.app))throw sourceChanged('REPAIR_ACCOUNT_CHANGED');
      await this.rows(account,'account',{},epoch,['ACCOUNT_APP_SCOPE_ONLY','BASE_REPAIR_ONLY']);
      this.phaseStart('base',this.repair.target_count);
      let offset=0;
      do {
        this.check(epoch);
        const page=await this.host.call('repair_targets',{...this.run,offset});this.check(epoch);
        if(!Array.isArray(page.rooms)||page.total!==this.repair.target_count||page.next_offset!==offset+page.rooms.length||page.next_offset>page.total||(!page.rooms.length&&offset<page.total))throw Error('REPAIR_CURSOR');
        for(const room of page.rooms) {
          if(typeof room!=='string'||!/^\d{1,40}$/.test(room))throw Error('REPAIR_CURSOR');
          const context={session:scalar(room)};
          await this.attempt('session',async()=>{
            const base=await this.read(tab,{op:'base',owner:account.owner,room},epoch);
            const fields=base.rows[0];
            if(base.rows.length!==1||!sameScalar(base.owner,account.owner)||!sameScalar(base.app,account.app)||!sameScalar(fields?.room_id,context.session)||!sameScalar(fields?.anchor_id,account.owner))throw Error('SESSION_BINDING_UNVERIFIED');
            await this.rows(base,'session',context,epoch,['BASE_REPAIR_ONLY','SOURCE_TIMEZONE_UNKNOWN']);
          },epoch,false,context);
        }
        offset=page.next_offset;
      }while(offset<this.repair.target_count);
      this.check(epoch);if(this.missing)reason='failed';
    }catch(e){
      if(e.message==='CANCELLED')return;
      reason=e.message==='SOURCE_CHANGED'?'source_changed':'failed';
      this.notify({kind:'error',code:/^[A-Z_]{1,80}$/.test(e.message)?e.message:'COLLECTOR_FAILED',detail:e.detail,reason});
    }finally{if(this.run&&epoch===this.epoch&&!this.stopping)await this.finish(reason);}
  }
  async save(items,epoch) {
    this.check(epoch);
    let batch=[],bytes=0;
    const flush=async()=>{if(!batch.length)return;this.check(epoch);const result=await this.host.call('append',{...this.run,batch_id:uuid(),items:batch});this.check(epoch);this.total=result.total;for(const item of batch){const group=item.domain==='capability'?'diagnostic':item.domain==='account'?'account':item.issues.includes('EMPTY_RESPONSE_NOT_COMPLETE')||item.issues.includes('NO_ALLOWLISTED_FIELDS')?'empty':'data';this.counts[group]++;}this.notify({kind:'progress',total:this.total,calls:this.calls,missing:this.missing,counts:{...this.counts}});batch=[];bytes=0;};
    for(const item of items){const size=new TextEncoder().encode(JSON.stringify(item)).length;if(size>250000)throw Error('OBSERVATION_SIZE');if(batch.length>=50||bytes+size>650000)await flush();batch.push(item);bytes+=size;}await flush();
  }
  async read(tab,task,epoch) {
    this.check(epoch);if(this.options.readAll!==true&&this.calls>=this.options.maxCalls)throw Error('BUDGET');
    await this.delay(600);this.check(epoch);
    if(this.phase?.op!==task.op)this.phaseStart(task.op);
    const phase=this.phase;this.calls++;
    try{const data=await this.source.read(tab,{...task,includeBody:this.scope.include_body});this.check(epoch);return data;}
    finally{if(this.run&&epoch===this.epoch&&this.phase===phase)phase.done++;}
  }
  sourceInfo(data,chat=false){return {origin:chat?'https://www.douyin.com':'https://anchor.douyin.com',path:data.path,observed_at:data.observed_at,transport:chat?'native_sdk':'site_get'};}
  async rows(data,domain,context,epoch,issues=[]) {
    const base={owner:data.owner,app_scope:data.app,...context};
    const received=data.received??data.rows.length;
    const at=new Date(data.observed_at||Date.now());
    const observer={'b_observer.local_date':scalar(localDate(at)),'b_observer.timezone':scalar(Intl.DateTimeFormat().resolvedOptions().timeZone),'b_observer.utc_offset_minutes':scalar(-at.getTimezoneOffset())};
    const rows=data.rows.map((fields,i)=>observation(domain,{...this.sourceInfo(data,domain==='chat'),ref:`/projection/rows/${i}`},{...fields,...observer},{...base,...(data.op==='chat_index'?{conversation:fields.id||null}:{})},
      {received,limit:data.limit??null,terminal:data.terminal||'unknown'},[...issues,...(!Object.keys(fields).some(k=>!k.startsWith('b_projection.'))?['NO_ALLOWLISTED_FIELDS']:[]),...(fields['b_projection.omitted_fields']?['FIELDS_NOT_SCALAR_OR_TOO_LARGE']:[])]));
    // Empty success is an observation, not proof of zero history.
    if(!rows.length)rows.push(observation(domain,this.sourceInfo(data,domain==='chat'),{},base,{received:0,terminal:data.terminal||'unknown'},['EMPTY_RESPONSE_NOT_COMPLETE']));
    await this.save(rows,epoch);
  }
  async unavailable(domain,error,epoch,chat=false,context={}) {
    this.check(epoch);
    if(['CANCELLED','BUDGET','SOURCE_CHANGED','NATIVE_DISCONNECTED','NATIVE_TIMEOUT'].includes(error.message))throw error;
    const code=/^[A-Z_]{1,80}$/.test(error.message)?error.message:'SOURCE_READ_FAILED';this.missing++;
    const item=capability(domain,code,{origin:chat?'https://www.douyin.com':'https://anchor.douyin.com',path:'/adapter-capability',transport:'capability'},context);
    const detail=['HTTP_401','HTTP_403'].includes(error.detail)?error.detail:null;
    if(detail){item.issues.push(detail);item.fields['b_diagnostic.http_status']=scalar(Number(detail.slice(5)));}
    if(['account','data'].includes(error.requestStage))item.fields['b_diagnostic.request_stage']=scalar(error.requestStage);
    const diagnostic=cleanDiagnostic(error.diagnostic);
    if(diagnostic)for(const [name,value]of Object.entries(diagnostic))if(value!==null)item.fields['b_diagnostic.'+name]=scalar(value);
    if(!chat&&code==='SOURCE_PERMISSION')item.issues.push('BACKEND_STOPPED_AFTER_PERMISSION_FAILURE');
    await this.save([item],epoch);
    this.notify({kind:'missing',domain,code,detail,diagnostic});
    if(!chat&&code==='SOURCE_PERMISSION')throw Object.assign(Error('BACKEND_ACCESS_BLOCKED'),{detail});
  }
  async attempt(domain,fn,epoch,chat=false,context={}) {try{return await fn();}catch(e){await this.unavailable(domain,e,epoch,chat,context);return null;}}
  async capture(epoch) {
    let reason='completed_requested_scope';
    try {
      if(this.options.anchorTab){try{await this.backend(epoch);}catch(e){if(e.message!=='BACKEND_ACCESS_BLOCKED')throw e;this.check(epoch);this.notify({kind:'blocked',domain:'backend',code:e.message,detail:e.detail});}}
      else if(this.scope.domains.some(x=>!['chat','legacy_chat','capability','account'].includes(x)))await this.unavailable('backend',Error('BACKEND_TAB_UNAVAILABLE'),epoch);
      if(this.scope.domains.includes('chat')) {
        if(this.options.chatTab)await this.attempt('chat',()=>this.chats(epoch),epoch,true);
        else await this.unavailable('chat',Error('CHAT_TAB_UNAVAILABLE'),epoch,true);
      }
      this.check(epoch);
      if(this.missing)reason='failed';
    } catch(e) {
      if(e.message==='CANCELLED')return;
      reason=e.message==='BUDGET'?'budget':e.message==='SOURCE_CHANGED'?'source_changed':'failed';this.notify({kind:'error',code:/^[A-Z_]{1,80}$/.test(e.message)?e.message:'COLLECTOR_FAILED',detail:e.detail,reason});
    } finally {if(this.run&&epoch===this.epoch&&!this.stopping)await this.finish(reason);}
  }
  async backend(epoch) {
    const tab=this.options.anchorTab;
    this.notify({kind:'stage',stage:'main'});
    const account=await this.attempt('account',()=>this.read(tab,{op:'account'},epoch),epoch);if(!account)return;
    await this.rows(account,'account',{},epoch,['ACCOUNT_APP_SCOPE_ONLY']);const owner=account.owner;
    if(this.scope.domains.includes('rank'))for(const kind of ['score','comment_cnt','watch_duration'])for(const period of (kind==='comment_cnt'?['1d','7d','30d']:['last_live','today','7d','30d'])) {
      await this.attempt('rank',async()=>{const d=await this.read(tab,{op:'rank',owner,rankType:kind,period},epoch);await this.rows(d,'rank',{period_label:`${kind}:${period}`},epoch,['EXACT_WINDOW_UNKNOWN','PLATFORM_RETENTION_UNKNOWN']);},epoch);
    }
    const sessionDomains=['session','summary','profile','comment','gift','speech','trend'];
    if(!this.scope.domains.some(x=>sessionDomains.includes(x))){this.notify({kind:'main_ready',sessions:0,missing:this.missing});return;}
    const sessions=new Map();
    const details=[];
    this.phaseStart('history',dateDays(this.scope.start_date,this.scope.end_date).length);
    for(const day of dateDays(this.scope.start_date,this.scope.end_date)) {
      const d=await this.attempt('session_list',()=>this.read(tab,{op:'history',owner,day},epoch),epoch);if(!d)continue;
      await this.rows(d,'session_list',{period_label:`requested_calendar_day:${day}`},epoch,['SOURCE_TIMEZONE_UNKNOWN']);
      for(const row of d.rows){const key=raw(row.roomID);if(typeof key==='string'&&/^\d{1,40}$/.test(key))sessions.set(key,row);}
    }
    this.notify({kind:'sessions',count:sessions.size});
    for(const [room,history] of sessions) {
      const context={session:history.roomID};
      const base=await this.attempt('session',()=>this.read(tab,{op:'base',owner,room},epoch),epoch,false,context);if(!base)continue;
      const f=base.rows[0]||{};
      if(f.anchor_id&&String(raw(f.anchor_id))!==String(raw(owner)))throw sourceChanged('SESSION_OWNER_CHANGED');
      if(f.room_id&&String(raw(f.room_id))!==room)throw sourceChanged('SESSION_CHANGED');
      if(!f.room_id||!f.anchor_id){await this.rows(base,'session',{session:null},epoch,['SESSION_BINDING_UNVERIFIED']);await this.unavailable('session',Error('SESSION_BINDING_UNVERIFIED'),epoch,false,context);continue;}
      const start=raw(f.start_time),end=raw(f.end_time);
      const skipReasons=this.options.skipTests?sessionSkipReasons(start,end,history.serverWatchUcntTdDirect):[];
      const test=skipReasons.length>0;
      await this.rows(base,'session',context,epoch,test?['SHORT_TEST_BROADCAST_SKIPPED',...skipReasons,'SOURCE_TIMEZONE_UNKNOWN']:['SOURCE_TIMEZONE_UNKNOWN']);
      if(test){this.notify({kind:'skipped'});continue;}
      if(this.scope.domains.includes('summary'))await this.attempt('summary',async()=>{const d=await this.read(tab,{op:'summary',owner,room},epoch);await this.rows(d,'summary',context,epoch,['METRIC_UNITS_UNVERIFIED']);},epoch,false,context);
      const users=new Set();
      if(this.scope.domains.includes('rank')||this.scope.domains.includes('profile'))for(const kind of ['score','comment_cnt_realtime','watch_duration','like'])await this.attempt('rank',async()=>{const d=await this.read(tab,{op:'rank',owner,room,rankType:kind,period:'room'},epoch);if(this.scope.domains.includes('rank'))await this.rows(d,'rank',{...context,period_label:`${kind}:room`},epoch,['ROW_IDENTITY_NOT_REVIEWED']);for(const row of d.rows){const user=raw(row['user.id_str']);if(typeof user==='string'&&/^\d{1,40}$/.test(user))users.add(user);}},epoch,false,context);
      if(this.scope.domains.includes('trend'))await this.attempt('trend',async()=>{const d=await this.read(tab,{op:'trend',owner,room},epoch);await this.rows(d,'trend',context,epoch,['TREND_UNIT_AND_INTERVAL_UNVERIFIED']);},epoch,false,context);
      details.push({room,context,start,end,users});
    }
    // Every selected main-data request finishes before any slow detail request.
    // Queue entries keep exact session/user values; no cross-session ID merging.
    this.notify({kind:'main_ready',sessions:details.length,missing:this.missing});
    if(!this.scope.domains.some(x=>['profile','comment','gift','speech'].includes(x)))return;
    this.notify({kind:'stage',stage:'details'});
    for(const {room,context,start,end,users} of details){
      this.check(epoch);
      if(this.scope.domains.includes('profile'))this.phaseStart('profile',users.size);
      if(this.scope.domains.includes('profile'))for(const user of users) {
        const success=await this.attempt('profile',async()=>{const d=await this.read(tab,{op:'profile',owner,room,user},epoch);await this.rows({...d,rows:d.rows.map(row=>({...row,'b_request.user_id':scalar(user)}))},'profile',context,epoch,['REQUEST_USER_ID_NOT_INDEPENDENT_IDENTITY','PROFILE_PERIODS_IN_FIELD_NAMES','EXACT_WINDOW_UNKNOWN']);return true;},epoch,false,context);
        if(!success)break;
      }
      for(const [domain,contentType,step] of [['comment','2',60000],['gift','1',60000],['speech','4',1800000]])if(this.scope.domains.includes(domain)) {
        const ranges=segments(start,end,step);
        this.phaseStart('content',ranges.length,domain);
        if(!ranges.length){await this.unavailable(domain,Error('SESSION_TIME_UNAVAILABLE'),epoch,false,context);continue;}
        for(const [a,b] of ranges) {
          const requestContext={...context,period_label:'requested_source_labels',period_start:a,period_end:b};
          const success=await this.attempt(domain,async()=>{const d=await this.read(tab,{op:'content',owner,room,contentType,start:a,end:b},epoch);await this.rows(d,domain,requestContext,epoch,['REQUEST_BOUNDARIES_NOT_COVERAGE','EVENT_DEDUP_UNVERIFIED']);return true;},epoch,false,requestContext);
          // Permission/schema failures won't be repeated for every minute of the same session.
          if(!success)break;
        }
      }
    }
  }
  async chats(epoch) {
    this.notify({kind:'stage',stage:'chat'});
    this.phaseStart('chat_index');
    const tab=this.options.chatTab;let offset=0,owner=null;const convs=new Map();
    do {
      const d=await this.read(tab,{op:'chat_index',owner,offset},epoch);owner=d.owner;
      await this.rows(d,'chat',{},epoch,['CACHED_CONVERSATION_LIST_PARTIAL','NATIVE_ENUM_SCOPE_UNVERIFIED']);
      for(const row of d.rows)if(row.id?.type==='string')convs.set(row.id.value,row);
      offset=d.next;
    }while(offset!==null);
    for(const [conversation,meta] of convs)await this.attempt('chat',async()=>{
      const seen=new Set();let at=0;
      const save=async d=>{const fresh=[],unbound=[];for(const row of d.rows){
        if(row.conversationId&&raw(row.conversationId)!==conversation)throw sourceChanged('MESSAGE_CONVERSATION_CHANGED');
        if(row.conversationShortId&&meta.shortId&&raw(row.conversationShortId)!==raw(meta.shortId))throw sourceChanged('MESSAGE_CONVERSATION_CHANGED');
        const serialized=JSON.stringify(row);if(!seen.has(serialized)){seen.add(serialized);const target=row.conversationId||row.conversationShortId&&meta.shortId?fresh:unbound;target.push({...row,'b_request.conversation_id':meta.id,...Object.fromEntries(Object.entries(meta).map(([k,v])=>['conversation.'+k,v]))});}
      }
      const issues=['NATIVE_ENUM_SCOPE_UNVERIFIED','RAW_TYPE_NOT_TEXT_LABEL','CHAT_DATE_RANGE_NOT_FILTERED'];
      if(fresh.length)await this.rows({...d,rows:fresh},'chat',{conversation:meta.id},epoch,issues);
      if(unbound.length)await this.rows({...d,rows:unbound},'chat',{conversation:null},epoch,[...issues,'MESSAGE_CONVERSATION_BINDING_UNVERIFIED']);
      if(!d.rows.length)await this.rows(d,'chat',{conversation:meta.id},epoch,issues);
      return fresh.length+unbound.length;};
      do {const d=await this.read(tab,{op:'chat_page',owner,conversation,offset:at,more:false},epoch);await save(d);at=d.next;}while(at!==null);
      for(let page=0;this.options.readAll===true||page<this.options.chatPages;page++) {
        const d=await this.read(tab,{op:'chat_page',owner,conversation,more:true},epoch);const fresh=await save(d);
        if(d.has_more===false)break;
        if(!fresh){await this.unavailable('chat',Error('CHAT_CURSOR_NOT_ADVANCING'),epoch,true,{owner,conversation:meta.id});break;}
        if(this.options.readAll!==true&&page+1===this.options.chatPages)await this.save([observation('capability',{origin:'https://www.douyin.com',path:'/native-sdk/history-budget',transport:'capability'}, {domain:scalar('chat')},{owner,conversation:meta.id},{basis:'history_request_budget',terminal:'budget'},['CHAT_HISTORY_BUDGET_REACHED'])],epoch);
      }
    },epoch,true,{owner,conversation:meta.id});
  }
  async importFiles(files,epoch) {
    try {
      for(const file of files) {
        if(file.size>512*1024*1024)throw Error('IMPORT_SIZE');
        const key=uuid(),reader=file.stream().getReader(),decoder=new TextDecoder('utf-8',{fatal:true});let pending='',line=0,batch=[];
        await this.save([observation('legacy_chat',{origin:'local:user-selected-jsonl',path:'/selected-jsonl',transport:'selected_jsonl',ref:`/files/${key}/selection`},
          {file_key:scalar(key),'b_file.name':scalar(file.name||'unnamed-selected-file'),'b_file.size':scalar(file.size),'b_file.last_modified':scalar(Number.isFinite(file.lastModified)?file.lastModified:null)},
          {},{basis:'user_selected_file'},['FILENAME_NOT_IDENTITY','FULL_FILE_HASH_NOT_SAVED'])],epoch);
        const consume=async last=>{let cut;while((cut=pending.indexOf('\n'))>=0 || (last&&pending.length)){const str=cut>=0?pending.slice(0,cut):pending;pending=cut>=0?pending.slice(cut+1):'';line++;if(str.trim()){batch.push(legacyRecord(parseLossless(str.replace(/^\uFEFF/,'')),key,line,this.scope.include_body));if(batch.length===20){await this.save(batch,epoch);batch=[];}}}};
        try{for(;;){this.check(epoch);const part=await reader.read();if(part.done)break;pending+=decoder.decode(part.value,{stream:true});if(pending.length>2*1024*1024)throw Error('IMPORT_LINE_SIZE');await consume(false);}pending+=decoder.decode();await consume(true);await this.save(batch,epoch);
          await this.save([observation('legacy_chat',{origin:'local:user-selected-jsonl',path:'/selected-jsonl',transport:'selected_jsonl',ref:`/files/${key}/end`},{file_key:scalar(key),line_count:scalar(line)}, {},{basis:'selected_file_rows',terminal:'selected_file_end'},['FILE_END_NOT_PLATFORM_COMPLETENESS'])],epoch);
        }finally{await reader.cancel().catch(()=>{});}
      }
      if(this.run&&epoch===this.epoch)await this.finish('completed_requested_scope');
    }catch(e){if(e.message!=='CANCELLED'&&this.run&&epoch===this.epoch){this.notify({kind:'error',code:/^[A-Z_]{1,80}$/.test(e.message)?e.message:'IMPORT_FAILED'});await this.finish('failed');}}
  }
  async finish(reason) {const run=this.run;this.ended=this.clock();this.run=null;this.epoch++;if(run){const result=await this.host.call('finish',{run_id:run.run_id,generation:run.generation,reason});this.notify({kind:'finished',reason,total:result.total,missing:this.missing,counts:{...this.counts}});}}
  async stop() {if(!this.run)return;this.stopping=true;await this.finish('cancelled');}
  disconnected(){this.ended=this.clock();this.run=null;this.epoch++;this.stopping=true;this.notify({kind:'error',code:'NATIVE_DISCONNECTED'});}
}
