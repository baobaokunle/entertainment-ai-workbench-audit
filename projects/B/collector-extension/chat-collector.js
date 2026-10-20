import {Collector} from './engine.js';
import {scalar,raw,observation} from './core.js';
import {annotateChatFields} from './chat-media.js';
export {ChatBrowserSource} from './chat-source.js';

export const CHAT_ADAPTER='b-chat-sources/3';
const same=(a,b)=>a?.type===b?.type&&a?.lossless===true&&b?.lossless===true&&a.value===b.value;
const changed=()=>Error('SOURCE_CHANGED');
const stableId=v=>v?.type==='string'&&v.lossless===true&&v.value.length>0;

export class EnhancedCollector extends Collector {
 boundFailures=new WeakSet();
 async unavailable(domain,error,epoch,chat=false,context={}){
  if(chat&&error&&typeof error==='object'&&this.boundFailures.has(error)){this.check(epoch);return;}
  return super.unavailable(domain,error,epoch,chat,context);
 }
 async chatFailure(error,owner,conversation,epoch,{operation,phase,historyRequests}){
  const code=/^[A-Z_]{1,80}$/.test(error?.message)?error.message:'SOURCE_READ_FAILED';
  const fields={'b_chat.failure_operation':scalar(operation),'b_chat.failure_phase':scalar(phase),'b_chat.history_read_calls':scalar(historyRequests)};
  const d=error?.chatDiagnostic;
  if(code==='CHAT_HISTORY_EMPTY_END_UNVERIFIED'&&d?.operation==='chat_history'&&d.phase==='adapter_return'
    &&d.returned_messages===0&&d.has_more===false&&d.upstream_status_available===false){
   fields['b_chat.adapter_returned_messages']=scalar(0);fields['b_chat.adapter_has_more']=scalar(false);
   fields['b_chat.upstream_status_available']=scalar(false);
  }
  if(['HTTP_401','HTTP_403'].includes(error?.detail))fields['b_diagnostic.http_status']=scalar(Number(error.detail.slice(5)));
  await this.chatNote(code,owner,epoch,{conversation,fields});
  this.missing++;if(error&&typeof error==='object')this.boundFailures.add(error);
  this.notify({kind:'missing',domain:'chat',code});
 }
 check(epoch){
  super.check(epoch);
  if(this.chatResume&&(!Number.isFinite(Date.parse(this.chatResume.expires_at))||Date.parse(this.chatResume.expires_at)<=Date.now()))throw Error('RETENTION_EXPIRED');
 }
 async start(scope,options){this.chatResume=null;return super.start(scope,options);}
 async startChatResume(plan_id,owner,chatTab){
  if(this.run)throw Error('TASK_ALREADY_RUNNING');
  const started=await this.host.call('chat_resume_open',{plan_id,owner});
  this.scope=started.scope;this.options={chatTab,anchorTab:null,readAll:true};this.chatResume=started;
  this.total=started.total;this.calls=0;this.missing=started.unresolved_media_count+(started.unresolved_history_count??0);
  this.counts={data:0,account:0,diagnostic:0,empty:0};this.stopping=false;
  this.started=this.clock();this.ended=null;this.phase=null;
  this.run={run_id:started.run_id,generation:started.generation};this.epoch++;return this.epoch;
 }
 async save(items,epoch){return super.save(items.map(item=>item.domain==='chat'||raw(item.fields?.domain)==='chat'?{...item,source:{...item.source,adapter:CHAT_ADAPTER}}:item),epoch);}
 async rows(data,domain,context,epoch,issues=[]){
  if(domain!=='chat')return super.rows(data,domain,context,epoch,issues);
  return super.rows({...data,rows:data.rows.map(row=>({...row,'b_chat.adapter':scalar(CHAT_ADAPTER)}))},domain,context,epoch,issues);
 }
 async chatNote(code,owner,epoch,{conversation=null,terminal='unknown',fields={}}={}){
  await this.save([observation('capability',{origin:'https://www.douyin.com',path:'/native-sdk/chat-coverage',transport:'capability'},
   {domain:scalar('chat'),status:scalar(code),'b_chat.adapter':scalar(CHAT_ADAPTER),...fields},
   {owner,conversation},{basis:'chat_source_evidence',terminal},[code])],epoch);
 }
 async chats(epoch){
  this.notify({kind:'stage',stage:'chat'});
  const tab=this.options.chatTab,conversations=new Map();let owner=this.chatResume?.owner??null,processed=0,sourceEnded=0,messageObservations=0;
  const progress=stage=>this.notify({kind:'chat_progress',stage,discovered:this.chatResume?.directory_count??conversations.size,processed,sourceEnded,messageObservations,listComplete:false,
   resumed:this.chatResume!==null&&this.chatResume!==undefined,completedSkipped:this.chatResume?.completed_count??0});
  const read=async task=>{
   const d=await this.read(tab,{...task,owner},epoch);
   if(!stableId(d.owner)||(owner&&!same(owner,d.owner)))throw changed();owner=d.owner;return d;
  };
  const discover=async d=>{
   const fresh=[];
   for(const row of d.rows){
    if(!stableId(row.id)){await this.unavailable('chat',Error('CHAT_CONVERSATION_ID_MISSING'),epoch,true,{owner});continue;}
    const id=row.id.value,previous=conversations.get(id);
    if(previous){for(const key of ['shortId','type'])if(previous[key]&&row[key]&&!same(previous[key],row[key]))throw changed();continue;}
    conversations.set(id,row);fresh.push(row);
   }
   if(fresh.length)await this.rows({...d,op:'chat_index',rows:fresh},'chat',{},epoch,['CACHED_CONVERSATION_LIST_PARTIAL','NATIVE_ENUM_SCOPE_UNVERIFIED']);
   progress('loading');
  };
  if(this.chatResume){
   let offset=0;
   do{
    this.check(epoch);const page=await this.host.call('chat_resume_targets',{...this.run,offset});this.check(epoch);
    if(!Array.isArray(page.rows)||page.total!==this.chatResume.target_count||page.next_offset!==offset+page.rows.length
      ||page.next_offset>page.total||(!page.rows.length&&offset<page.total))throw Error('CHAT_RESUME_CURSOR');
    for(const meta of page.rows){
     if(!stableId(meta.id)||conversations.has(meta.id.value))throw Error('CHAT_RESUME_RECORD_MISMATCH');
     conversations.set(meta.id.value,meta);
    }
    offset=page.next_offset;
   }while(offset<this.chatResume.target_count);
  }else{
  // Paginate the initial cache, then capture every automatic-scroll snapshot.
  // Counts alone cannot detect replacement of rows in a virtualized list.
  let offset=0;
  do{const d=await read({op:'chat_index',offset});await discover(d);if(d.next!==null&&(!Number.isSafeInteger(d.next)||d.next<=offset))throw Error('CHAT_CURSOR');offset=d.next;}while(offset!==null);
  let stable=0,previous=null,top=true,listStop='CHAT_LIST_END_UNCONFIRMED';
  try{
   while(stable<12){
    const before=conversations.size,d=await read({op:'chat_load',top});top=false;await discover(d);
    const s=d.scroll;if(!s||![s.after,s.height,s.viewport].every(Number.isFinite)||typeof s.bottom!=='boolean')throw Error('CHAT_SHAPE');
    const key=JSON.stringify([s.after,s.height,s.viewport,s.bottom]);
    stable=previous===key&&conversations.size===before?stable+1:0;previous=key;
    listStop=s.bottom?'CHAT_LIST_END_UNCONFIRMED':'CHAT_LIST_LOAD_STALLED';
   }
  }catch(e){
   if(e.message!=='CHAT_SCROLL_CONTAINER_UNAVAILABLE')throw e;
   await this.unavailable('chat',e,epoch,true,{owner});listStop=e.message;
  }
  await this.chatNote(listStop,owner,epoch,{terminal:'cache_only',fields:{'b_chat.discovered_conversations':scalar(conversations.size),'b_chat.list_complete':scalar(false)}});
  }
  progress('history');
  for(const [conversation,meta]of conversations){
   const directoryRecovery=['b-chat-directory-repair/1','b-chat-reviewed-recovery/1','b-chat-dual-reviewed/1'].includes(this.chatResume?.client);
   // Commit one attempt before any source read. A lost response or process crash
   // must not silently retry this target on a later automatic resume.
   if(directoryRecovery)await this.chatNote('CHAT_DIRECTORY_RECOVERY_ATTEMPT_STARTED',owner,epoch,{conversation:meta.id});
   const resumeRequest=this.chatResume?{resumeConversation:meta}:{};
   const seen=new Map();let at=0,cursor=null,pages=0,stagnant=0,ended=false,incrementalBoundary=false;
   let requestOperation='chat_cache',failurePhase='source_read',historyRequests=0;
   const cursors=new Set();
   const save=async d=>{
    if(!same(d.conversation?.id,meta.id))throw changed();
    for(const key of ['shortId','type'])if(meta[key]&&d.conversation[key]&&!same(meta[key],d.conversation[key]))throw changed();
    let bound=[],unbound=[];let uniqueSourceRows=0;
    for(const row of d.rows){
     if(row.conversationId&&!same(row.conversationId,meta.id))throw changed();
     if(row.conversationShortId&&meta.shortId&&!same(row.conversationShortId,meta.shortId))throw changed();
     const exact=JSON.stringify(row),idKey=['serverId','clientId','id'].find(k=>stableId(row[k]));
     const key=idKey?JSON.stringify([idKey,row[idKey].value]):exact;
     if(seen.get(key)===exact)continue;seen.set(key,exact);uniqueSourceRows++;
     const projected=annotateChatFields(row,this.scope.include_body),fields={...projected,'b_request.conversation_id':meta.id,
      ...Object.fromEntries(Object.entries(meta).map(([k,v])=>['conversation.'+k,v]))};
     if(!idKey)fields['b_chat.message_key_missing']=scalar(true);
     (row.conversationId||row.conversationShortId&&meta.shortId?bound:unbound).push(fields);
    }
    if(this.filterChatRows){
     // Check every history page for the timestamp boundary, including repeated cache rows.
     const processed=await this.filterChatRows({owner,conversation:meta,rows:[...bound,...unbound],sourceRows:d.rows,op:d.op,epoch});
     this.check(epoch);incrementalBoundary=d.op==='chat_history'&&processed.beforeBoundary===true;
     bound=processed.rows.filter(f=>f.conversationId||f.conversationShortId&&meta.shortId);
     unbound=processed.rows.filter(f=>!f.conversationId&&!(f.conversationShortId&&meta.shortId));
    }
    const issues=['NATIVE_ENUM_SCOPE_UNVERIFIED','RAW_TYPE_NOT_TEXT_LABEL','CHAT_DATE_RANGE_NOT_FILTERED','MEDIA_LABEL_IS_CANDIDATE'];
    if(bound.length)await this.rows({...d,rows:bound},'chat',{conversation:meta.id},epoch,issues);
    if(unbound.length)await this.rows({...d,rows:unbound},'chat',{conversation:null},epoch,[...issues,'MESSAGE_CONVERSATION_BINDING_UNVERIFIED']);
    if(!d.rows.length)await this.rows(d,'chat',{conversation:meta.id},epoch,issues);
    messageObservations+=bound.length+unbound.length;progress('history');return uniqueSourceRows;
   };
   try{
    do{requestOperation='chat_cache';failurePhase='source_read';const d=await read({op:'chat_cache',conversation,offset:at,...resumeRequest});failurePhase='save_response';await save(d);if(d.next!==null&&(!Number.isSafeInteger(d.next)||d.next<=at))throw Error('CHAT_CURSOR');at=d.next;}while(at!==null);
    while(this.options.readAll===true||pages<this.options.chatPages){
     requestOperation='chat_history';failurePhase='source_read';historyRequests++;
     const d=await read({op:'chat_history',conversation,cursor,...resumeRequest});pages++;failurePhase='save_response';const fresh=await save(d);
     if(incrementalBoundary)break;
     if(d.has_more===false){ended=true;sourceEnded++;break;}
     if(d.has_more!==true)throw Error('CHAT_HISTORY_END_UNKNOWN');
     if(typeof d.cursor!=='string'||!/^[0-9]{1,40}$/.test(d.cursor)||d.cursor===cursor||cursors.has(d.cursor))throw Error('CHAT_CURSOR_NOT_ADVANCING');
     cursors.add(d.cursor);cursor=d.cursor;
     // Empty or repeated pages may still advance. Stop only sustained lack of progress,
     // record the gap, and never turn a safety stop into a source end assertion.
     stagnant=fresh?0:stagnant+1;if(stagnant>=20)throw Error('CHAT_HISTORY_LOAD_STALLED');
    }
    failurePhase='completion';
    if(incrementalBoundary)await this.chatNote('CHAT_INCREMENTAL_BOUNDARY_REACHED',owner,epoch,{conversation:meta.id,terminal:'unknown',fields:{'b_chat.boundary_basis':scalar('per_conversation_old_latest_local_day'),'b_chat.old_coverage_complete':scalar(false)}});
    else if(!ended)await this.chatNote('CHAT_HISTORY_BUDGET_REACHED',owner,epoch,{conversation:meta.id,terminal:'budget'});
    else await this.chatNote('CHAT_HISTORY_SOURCE_NO_MORE',owner,epoch,{conversation:meta.id,terminal:'source_no_more',fields:{'b_chat.history_pages':scalar(pages)}});
   }catch(e){
    if(['CANCELLED','BUDGET','SOURCE_CHANGED','NATIVE_DISCONNECTED','NATIVE_TIMEOUT'].includes(e.message))throw e;
    await this.chatFailure(e,owner,meta.id,epoch,{operation:requestOperation,phase:failurePhase,historyRequests});
    // Unknown SDK/permission/timeout failures stop this chat task, avoiding repeated
    // requests after an unresolved request or account boundary failure.
    if(!['CHAT_CURSOR_NOT_ADVANCING','CHAT_HISTORY_END_UNKNOWN','CHAT_HISTORY_LOAD_STALLED','CONVERSATION_UNAVAILABLE','CHAT_CURSOR'].includes(e.message))throw e;
   }
   processed++;progress('history');
  }
  await this.chatNote('CHAT_COVERAGE_PARTIAL',owner,epoch,{terminal:'cache_only',fields:{'b_chat.discovered_conversations':scalar(conversations.size),
   'b_chat.processed_conversations':scalar(processed),'b_chat.source_ended_conversations':scalar(sourceEnded),'b_chat.message_observations':scalar(messageObservations),'b_chat.list_complete':scalar(false)}});
  progress('finished');
 }
}
