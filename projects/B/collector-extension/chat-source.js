import {BrowserSource} from './engine.js';

// Self-contained MAIN-world function. No downloaded/evaluated source, DOM text,
// contact clicks, send/read-mark methods, or broad browser-storage access.
export async function chatSourceRead(task) {
 let failureDiagnostic=null;
 const fail=code=>{throw Error(code);};
 const own=(o,k)=>{if(!o||(typeof o!=='object'&&typeof o!=='function'))return undefined;const d=Object.getOwnPropertyDescriptor(o,k);return d&&Object.hasOwn(d,'value')?d.value:undefined;};
 const scalar=v=>typeof v==='string'&&v.length<=32768?{type:'string',value:v,lossless:true}:typeof v==='number'&&Number.isSafeInteger(v)?{type:'number',value:v,lossless:true}:typeof v==='boolean'?{type:'boolean',value:v,lossless:true}:v===null?{type:'null',value:null,lossless:true}:null;
 const fields=(o,keys)=>{const out={};let omitted=0;for(const k of keys){const v=o?.[k];if(v===undefined)continue;const s=scalar(v);if(s)out[k]=s;else omitted++;}if(omitted)out['b_projection.omitted_fields']=scalar(omitted);return out;};
 const cursorText=v=>{
  if(typeof v==='string'&&/^[0-9]{1,40}$/.test(v))return v;
  if(typeof v==='number'&&Number.isSafeInteger(v)&&v>=0)return String(v);
  const low=own(v,'low'),high=own(v,'high'),unsigned=own(v,'unsigned');
  if(!Number.isInteger(low)||!Number.isInteger(high)||low<-(2**31)||low>2**31-1||high<-(2**31)||high>2**31-1||typeof unsigned!=='boolean')return null;
  const n=(BigInt(high>>>0)<<32n)|BigInt(low>>>0);if(!unsigned&&high<0)return null;return n.toString();
 };
 try{
  if(location.origin!=='https://www.douyin.com'||location.pathname!=='/chat')fail('SOURCE_ORIGIN');
  if(!['chat_owner','chat_index','chat_load','chat_cache','chat_history'].includes(task?.op))fail('OP_NOT_ALLOWED');
  const runtimeNames=Object.getOwnPropertyNames(globalThis).filter(k=>/^webpackChunk[A-Za-z0-9_]{0,100}$/.test(k));
  if(runtimeNames.length>12)fail('CHAT_SDK_RUNTIME_AMBIGUOUS');
  const runtimes=[...new Set(runtimeNames.map(k=>own(globalThis,k)).filter(c=>Array.isArray(c)&&c.push!==Array.prototype.push))];
  const matches=[];
  // Current remote is a separate, versioned chunk runtime. This fixed getter was
  // checked in the public 4da7f841 bundle: it returns an existing SDK instance only.
  const remoteName='@pc-im/im:1.0.0.1016',remote=own(globalThis,remoteName);
  if(Array.isArray(remote)&&remote.push!==Array.prototype.push){
   let req;remote.push([['b_chat_'+crypto.randomUUID()],{},r=>{req=r;}]);
   const f=req?.m?.[5245],code=typeof f==='function'?Function.prototype.toString.call(f):'';
   if(!code.includes('ImSdkInstance:null')||!code.includes('getImSdkInstance')||typeof req!=='function')fail('CHAT_SDK_REMOTE_VERSION_UNVERIFIED');
   const module=req(5245);if(typeof module?.o!=='function')fail('CHAT_SDK_REMOTE_UNAVAILABLE');
   matches.push({req,moduleId:'5245',module,readSdk:()=>module.o(true),runtime:remoteName});
  }
  if(!matches.length&&!runtimes.length)fail('CHAT_SDK_UNAVAILABLE');
  for(const chunks of matches.length?[]:runtimes){
   let req;chunks.push([['b_chat_'+crypto.randomUUID()],{},r=>{req=r;}]);
   const cached=req?.c?.[767375]?.exports;
   if(cached?.ImSdk?.bytedIMInstance){matches.push({req,moduleId:'767375',module:cached});continue;}
   if(!document.querySelector('[class*="conversationConversationListwrapper"], [data-e2e="conversation-list"]'))continue;
   // Build numbers change. Inspect only already-loaded public module functions,
   // never traverse application values or fetch/evaluate new scripts.
   if(!req?.m)continue;
   const entries=Object.entries(req.m);if(entries.length>30000)fail('CHAT_SDK_MODULE_TABLE_TOO_LARGE');
   const candidates=entries.filter(([id,f])=>{
    if(!/^[0-9]{1,12}$/.test(id)||typeof f!=='function')return false;
    const source=Function.prototype.toString.call(f);
    return source.length<2000000&&source.includes('bytedIMInstance')&&source.includes('ImSdk');
   });
   for(const [moduleId]of candidates)matches.push({req,moduleId,module:null});
  }
  if(!matches.length)fail('CHAT_SDK_SIGNATURE_NOT_FOUND');
  if(matches.length!==1)fail('CHAT_SDK_SIGNATURE_AMBIGUOUS');
  const {req,moduleId}=matches[0],module=matches[0].module??req(moduleId);
  const readSdk=matches[0].readSdk??(()=>module?.ImSdk?.bytedIMInstance);
  const sdk=readSdk();
  if(typeof sdk?.getConversationList!=='function')fail('CHAT_SDK_UNAVAILABLE');
  const verifyNative=async()=>{
   const c=task.nativeContract;
   if(!c)return false;
   if(c.version!=='native-history/1'||matches[0].runtime!=='@pc-im/im:1.0.0.1016'
     ||own(sdk,'oldOption')!==undefined)fail('CHAT_NATIVE_CONTRACT_CHANGED');
   const option=own(own(sdk,'ctx'),'option');
   if(!option||['aspectBefore','aspectAfter','aspectError'].some(k=>k in option))fail('CHAT_NATIVE_CONTRACT_CHANGED');
   const collected={};
   const collect=(label,object,names)=>{for(const name of names){let p=object;for(let depth=0;p&&depth<8;depth++,p=Object.getPrototypeOf(p)){
    const d=Object.getOwnPropertyDescriptor(p,name),f=d?.value??d?.get;
    if(typeof f==='function')collected[label+':'+depth+':'+name]=Function.prototype.toString.call(f);
   }}};
   collect('sdk',sdk,['getMessagesByConversation','getConversationList','getConversationOnline','api']);
   collect('api',sdk.api,['GetMessagesByConversation','sendWithRawBody']);
   collect('messages',sdk.resolve('messageManager'),['processNewMessagesFromPull']);
   let count=0;for(const p of sdk.ctx.plugin)collect('plugin'+count++,p,['receiveApiResponse']);
   if(count!==c.plugin_count||JSON.stringify(Object.keys(collected).sort())!==JSON.stringify(Object.keys(c.methods).sort()))fail('CHAT_NATIVE_CONTRACT_CHANGED');
   for(const [name,code]of Object.entries(collected)){
    const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(code)))).map(b=>b.toString(16).padStart(2,'0')).join('');
    if(digest!==c.methods[name])fail('CHAT_NATIVE_CONTRACT_CHANGED');
   }
   return true;
  };
  const readOwner=()=>{
   const contextOwner=own(own(own(sdk,'__internal_ctx'),'option'),'userId');
   const adapterOwner=matches[0].runtime?own(own(sdk,'oldOption'),'userId'):undefined;
   // Current native SDK's public option getter returns ctx.option directly.
   // Read only this already initialized instance's own data field, never credentials.
   const nativeOwner=matches[0].runtime?own(own(own(sdk,'ctx'),'option'),'userId'):undefined;
   const owners=[contextOwner,adapterOwner,nativeOwner].filter(v=>v!==undefined);
   if(owners.some(v=>v!==owners[0]))fail('SOURCE_CHANGED');
   return owners[0];
  };
  const ownerValue=readOwner(),owner=scalar(ownerValue);
  if(!owner||owner.type!=='string'||!ownerValue)fail('OWNER_UNAVAILABLE');
  if(task.owner&&(task.owner.type!=='string'||task.owner.lossless!==true||task.owner.value!==ownerValue))fail('SOURCE_CHANGED');
  const assertCurrent=()=>{
   const currentModule=req?.c?.[moduleId]?.exports??(typeof req==='function'?req(moduleId):module);
   if(currentModule!==module||readSdk()!==sdk||readOwner()!==ownerValue)fail('SOURCE_CHANGED');
  };
  if(task.nativeContract){await verifyNative();assertCurrent();}
  const list=task.op==='chat_owner'?[]:sdk.getConversationList();if(!Array.isArray(list))fail('CHAT_SHAPE');
  const convFields=c=>({...fields(c,['id','shortId','type','bizType','participantCount','toParticipantUserId','toParticipantSecUserId']),
   'b_chat.sdk_runtime':scalar(matches[0].runtime??'legacy_webpack'),
   'b_chat.sdk_access':scalar(matches[0].runtime?'existing_remote_provider_5245_o':'existing_ImSdk')});
  const result={ok:true,op:task.op,path:'/native-sdk/'+task.op,owner,app:null,rows:[],observed_at:new Date().toISOString(),terminal:'cache_only'};
  if(task.op==='chat_owner'){
   result.contract_verified=await verifyNative();assertCurrent();
  }else if(task.op==='chat_index'){
   const offset=task.offset??0;if(!Number.isSafeInteger(offset)||offset<0)fail('CHAT_CURSOR');
   result.rows=list.slice(offset,offset+200).map(convFields);result.total=list.length;result.next=offset+200<list.length?offset+200:null;
  }else if(task.op==='chat_load'){
   const roots=[...new Set(document.querySelectorAll('[class*="conversationConversationListwrapper"], [data-e2e="conversation-list"]'))];
   if(roots.length!==1)fail('CHAT_SCROLL_CONTAINER_UNAVAILABLE');
   const root=roots[0],nodes=[root,...root.querySelectorAll('*')];
   if(nodes.length>4000)fail('CHAT_SCROLL_CONTAINER_UNAVAILABLE');
   const candidates=nodes.filter(e=>e.clientHeight>0&&e.scrollHeight>e.clientHeight+1&&/^(auto|scroll)$/.test(getComputedStyle(e).overflowY));
   const outer=candidates.filter(e=>!candidates.some(other=>other!==e&&other.contains(e)));
   let el;
   if(outer.length===1)el=outer[0];
   else if(outer.length===0&&root.clientHeight>0&&root.scrollHeight<=root.clientHeight+1)el=root;
   else fail('CHAT_SCROLL_CONTAINER_UNAVAILABLE');
   const before=el.scrollTop;if(task.top===true)el.scrollTop=0;else el.scrollTop=Math.min(el.scrollHeight,el.scrollTop+Math.max(160,el.clientHeight*0.8));
   if(list.length>50000)fail('CHAT_SHAPE');
   // Capture each cache snapshot, including same-size replacement by a virtual list.
   result.rows=list.map(convFields);result.total=list.length;result.scroll={before,after:el.scrollTop,height:el.scrollHeight,viewport:el.clientHeight,bottom:el.scrollTop+el.clientHeight>=el.scrollHeight-2};
  }else{
   if(typeof task.conversation!=='string'||!task.conversation)fail('CONVERSATION_UNAVAILABLE');
   let conv=list.find(c=>c.id===task.conversation);
   if(!conv&&task.resumeConversation){
    // Fixed public pc-im adapter API (remote-expose.4da7f841): read the
    // existing conversation by its original id/shortId/type, not a nickname,
    // synthetic conversation object, or get-or-create operation.
    const saved=task.resumeConversation;
    if(saved.id?.type!=='string'||saved.id.lossless!==true||saved.id.value!==task.conversation
      ||saved.shortId?.type!=='string'||saved.shortId.lossless!==true||!saved.shortId.value
      ||saved.type?.type!=='number'||saved.type.lossless!==true||!Number.isSafeInteger(saved.type.value)
      ||!matches[0].runtime||typeof sdk.getConversationOnline!=='function')fail('CHAT_RESUME_CONVERSATION_UNAVAILABLE');
    let timer;
    conv=await Promise.race([sdk.getConversationOnline({conversationId:saved.id.value,shortId:saved.shortId.value,type:saved.type.value}),
     new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('SOURCE_TIMEOUT')),20000);})]).finally(()=>clearTimeout(timer));
    assertCurrent(); // No subsequent cache/history read after an awaited account change.
    if(!conv)fail('CHAT_RESUME_CONVERSATION_UNAVAILABLE');
   }
   if(!conv||typeof conv.getMessageList!=='function')fail('CONVERSATION_UNAVAILABLE');
   if(task.resumeConversation){
    const current=convFields(conv);
    for(const key of ['id','shortId','type']){const expected=task.resumeConversation[key];if(expected&&JSON.stringify(current[key])!==JSON.stringify(expected))fail('SOURCE_CHANGED');}
   }
   const before=convFields(conv);let messages;
   if(task.op==='chat_cache'){
    const nativeVerified=await verifyNative();assertCurrent();
    const offset=task.offset??0;if(!Number.isSafeInteger(offset)||offset<0)fail('CHAT_CURSOR');
    const cached=conv.getMessageList();if(!Array.isArray(cached))fail('CHAT_SHAPE');
    messages=cached.slice(offset,offset+100);result.next=offset+100<cached.length?offset+100:null;
    if(nativeVerified){await verifyNative();assertCurrent();result.cache_contract='native-cache/1';}
    result.cache_offset=offset;result.cache_total=cached.length;
   }else{
    if(typeof sdk.getMessagesByConversation!=='function')fail('CHAT_HISTORY_UNAVAILABLE');
    const nativeVerified=await verifyNative();assertCurrent();
    const args={conversation:conv,limit:50};
    if(task.cursor!==null&&task.cursor!==undefined){if(typeof task.cursor!=='string'||!/^[0-9]{1,40}$/.test(task.cursor))fail('CHAT_CURSOR');args.cursor=task.cursor;}
    const nativeHistory=async()=>{
     // The native implementation is pinned by the contract. Use a per-call
     // receiver to inspect its API result without changing any live SDK method.
     // Non-SEND InvalidTicket can pass sendWithRawBody, so hashes alone are not
     // sufficient: require the actual response status before processing messages.
     const api=sdk.api,original=api.GetMessagesByConversation;
     let calls=0,accepted=false;
     const facade=Object.freeze({GetMessagesByConversation:async input=>{
      if(++calls!==1)fail('CHAT_NATIVE_CONTRACT_CHANGED');
      const response=await Reflect.apply(original,api,[input]);
      assertCurrent();
      if(response?.status_code!==0)fail('CHAT_NATIVE_RESPONSE_NOT_SUCCESS');
      accepted=true;return response;
     }});
     const receiver=Object.create(sdk);
     Object.defineProperties(receiver,{api:{value:facade},resolve:{value:sdk.resolve.bind(sdk)}});
     let p=Object.getPrototypeOf(sdk),method;
     for(let depth=1;p&&depth<8;depth++,p=Object.getPrototypeOf(p)){
      const d=Object.getOwnPropertyDescriptor(p,'getMessagesByConversation');
      if(typeof d?.value==='function'){method=d.value;break;}
     }
     if(!method)fail('CHAT_NATIVE_CONTRACT_CHANGED');
     const page=await Reflect.apply(method,receiver,[args]);
     if(calls!==1||!accepted)fail('CHAT_NATIVE_CONTRACT_CHANGED');
     return page;
    };
    let timer;const page=await Promise.race([nativeVerified?nativeHistory():sdk.getMessagesByConversation(args),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('SOURCE_TIMEOUT')),20000);})]).finally(()=>clearTimeout(timer));
    assertCurrent(); // Account/runtime changes take precedence even for empty/error-shaped results.
    if(nativeVerified){await verifyNative();assertCurrent();}
    if(!Array.isArray(page?.messages))fail('CHAT_SHAPE');messages=page.messages;
    // The reviewed remote adapter can turn an internal request failure into
    // {messages:[],hasMore:false}. This shape cannot prove a history endpoint.
    // Stop the task instead of persisting a completion checkpoint or retrying.
    if(matches[0].runtime&&!nativeVerified&&messages.length===0&&page.hasMore===false){
     failureDiagnostic={operation:'chat_history',phase:'adapter_return',returned_messages:0,has_more:false,upstream_status_available:false};
     fail('CHAT_HISTORY_EMPTY_END_UNVERIFIED');
    }
    result.has_more=typeof page.hasMore==='boolean'?page.hasMore:null;result.cursor=cursorText(page.cursor);
    if(nativeVerified&&(result.has_more===null||result.cursor===null))fail('CHAT_SHAPE');
    if(nativeVerified)result.history_contract='native-history/1';
    result.terminal=result.has_more===false?'source_no_more':'unknown';
   }
   if(JSON.stringify(convFields(conv))!==JSON.stringify(before))fail('SOURCE_CHANGED');
   const keys=['serverId','clientId','id','conversationId','conversationShortId','sender','senderUserId','secSender','type','createTime','indexInConversation','orderInConversation','isRecalled'];
   if(task.includeBody===true)keys.push('content');
   result.rows=messages.map(m=>{
    const out=fields(m,keys);
    // Preserve a Date-valued SDK timestamp as an explicitly derived UTC label.
    try{const milliseconds=Date.prototype.getTime.call(m.createdAt);if(Number.isFinite(milliseconds)){out['b_chat.created_at_iso']=scalar(new Date(milliseconds).toISOString());out['b_chat.created_at_basis']=scalar('native_Date_utc_projection');}}catch{}
    if(task.includeBody===true){const text=own(own(m,'ext'),'s:file_ext_key_audio_asr_text');if(typeof text==='string'&&text.length<=32768)out['b_chat.source_transcript_text']=scalar(text);}
    return out;
   });result.conversation=before;
  }
  assertCurrent();
  return result;
 }catch(e){return {ok:false,error:/^[A-Z_]{1,80}$/.test(e?.message)?e.message:'SOURCE_READ_FAILED',...(failureDiagnostic?{chatDiagnostic:failureDiagnostic}:{})};}
}

// Closed diagnostic projection: never propagate response bodies, URLs or error text.
export function chatSourceError(data){
 const code=/^[A-Z_]{1,80}$/.test(data?.error)?data.error:'SOURCE_READ_FAILED';
 const d=data?.chatDiagnostic;
 const diagnostic=code==='CHAT_HISTORY_EMPTY_END_UNVERIFIED'&&d?.operation==='chat_history'&&d.phase==='adapter_return'
  &&d.returned_messages===0&&d.has_more===false&&d.upstream_status_available===false
  ?{operation:'chat_history',phase:'adapter_return',returned_messages:0,has_more:false,upstream_status_available:false}:null;
 return Object.assign(Error(code),{chatDiagnostic:diagnostic});
}

export class ChatBrowserSource extends BrowserSource {
 async read(tabId,task){
  if(!task.op.startsWith('chat_'))return super.read(tabId,task);
  const boundary='https://www.douyin.com/chat',page=url=>{const u=new URL(url||'about:blank');return u.origin+u.pathname;};
  const tab=await this.api.tabs.get(tabId);if(page(tab.url)!==boundary)throw Error('SOURCE_CHANGED');
  const bound=this.bindings.get(tabId);if(bound&&bound.boundary!==boundary)throw Error('SOURCE_CHANGED');
  const target=bound?{tabId,documentIds:[bound.documentId]}:{tabId,frameIds:[0]};
  const results=await this.api.scripting.executeScript({target,world:'MAIN',func:chatSourceRead,args:[task]});
  const r=results[0],after=await this.api.tabs.get(tabId);
  if(results.length!==1||r.frameId!==0||!r.documentId||(bound&&r.documentId!==bound.documentId)||page(after.url)!==boundary)throw Error('SOURCE_CHANGED');
  this.bindings.set(tabId,{boundary,documentId:r.documentId});
  const data=r.result;if(!data?.ok)throw chatSourceError(data);
  if(!Array.isArray(data.rows)||data.rows.length>50000)throw Error('CHAT_SHAPE');return data;
 }
}
