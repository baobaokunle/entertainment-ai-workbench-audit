// Generated from local core.js + page-body.js.txt; no runtime eval.
export async function sourceRead(task) {
class LosslessNumber { constructor(token){this.token=token;} }
function parseLossless(text) {
  let p = 0, depth = 0;
  const ws = () => { while (/\s/.test(text[p] || "x")) p++; };
  function str() {
    const start = p++;
    while (p < text.length) { if (text[p] === "\\") { p += 2; continue; } if (text[p++] === '"') return JSON.parse(text.slice(start, p)); }
    throw Error("SOURCE_JSON");
  }
  function value() {
    ws(); if (++depth > 64) throw Error("SOURCE_DEPTH");
    let out, c = text[p];
    if (c === '"') out = str();
    else if (c === "{" || c === "[") {
      const obj = c === "{"; p++; out = obj ? Object.create(null) : []; ws();
      const end = obj ? "}" : "]";
      if (text[p] !== end) for (;;) {
        ws(); let key;
        if (obj) { if (text[p] !== '"') throw Error("SOURCE_JSON"); key = str(); ws(); if (text[p++] !== ":" || Object.hasOwn(out,key)) throw Error("SOURCE_JSON"); }
        const item = value(); if (obj) out[key] = item; else out.push(item);
        ws(); if (text[p] !== ",") break; p++;
      }
      if (text[p++] !== end) throw Error("SOURCE_JSON");
    } else {
      const match = /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(p));
      if (!match) throw Error("SOURCE_JSON"); p += match[0].length;
      out = /^[tfn]/.test(match[0]) ? JSON.parse(match[0]) : new LosslessNumber(match[0]);
    }
    depth--; return out;
  }
  const out = value(); ws(); if (p !== text.length) throw Error("SOURCE_JSON"); return out;
}
function scalar(value) {
  if (value === null) return {type:"null",value:null,lossless:true};
  if (value instanceof LosslessNumber)
    return {type:"number_token",value:value.token,lossless:true};
  if (typeof value === "string" && value.length <= 32768) return {type:"string",value,lossless:true};
  if (typeof value === "boolean") return {type:"boolean",value,lossless:true};
  if (typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER)
    return {type:"number",value,lossless:true};
  return null;
}
// Build input, wrapped with the same tested lossless parser/scalar by tools/build_collector.mjs.
const fail=code=>{throw Error(code);};
const changed=detail=>{const error=Error("SOURCE_CHANGED");error.detail=detail;throw error;};
const value=x=>x instanceof LosslessNumber ? x.token : x;
const get=(obj,path)=>path.split(".").reduce((x,k)=>x?.[k],obj);
const fields=(obj,paths)=>{ const out={};let omitted=0; for(const path of paths) {const original=get(obj,path); if(original===undefined)continue;const v=scalar(original);if(v)out[path]=v;else omitted++;}if(omitted)out["b_projection.omitted_fields"]=scalar(omitted); return out; };
const timeout=AbortSignal.timeout(20000);
const origin=location.origin;
const MAX_BYTES=8*1024*1024;
// Fixed diagnostic metadata only. Never preserve exception messages or URLs.
let requestStage=null,requestPhase=null,httpStatus=null;
async function read(path,params={}) {
  const url=new URL(path,origin); for(const [k,v] of Object.entries(params)) if(v!==undefined&&v!==null)url.searchParams.set(k,String(v));
  // Fixed public application routing configuration from the saved site client.
  // Never copy credential/signature headers; passport keeps its separate request.
  const routing=path.startsWith("/anchor_pc_tinker_proxy/lego/")?{headers:{"x-appid":"477650","x-sub-web-id":"1116","x-use-bpsc":"1","client-id":"anchor_pc_tinker_proxy"}}:{};
  requestStage=path===paths.account?'account':'data';requestPhase='fetch';httpStatus=null;
  const response=await fetch(url,{method:"GET",credentials:"same-origin",cache:"no-store",redirect:"manual",signal:timeout,...routing});
  httpStatus=response.status;requestPhase='status';
  if(response.type==='opaqueredirect'){requestPhase='redirect';fail('SOURCE_READ_FAILED');}
  if(!response.ok){const denied=response.status===401||response.status===403;throw Object.assign(Error(denied?"SOURCE_PERMISSION":"SOURCE_HTTP"),denied?{detail:`HTTP_${response.status}`,requestStage:path==="/passport/account/info/v2/"?"account":"data"}:{});}
  if(!/json/.test(response.headers.get("content-type")||"")) fail("SOURCE_CONTENT_TYPE");
  requestPhase='body';
  const reader=response.body.getReader(),decoder=new TextDecoder("utf-8",{fatal:true}); let size=0,text="";
  try { for(;;) {const part=await reader.read();if(part.done)break;size+=part.value.length;if(size>MAX_BYTES)fail("SOURCE_SIZE");text+=decoder.decode(part.value,{stream:true});}text+=decoder.decode();}
  finally{await reader.cancel().catch(()=>{});}
  requestPhase='decode';let data=parseLossless(text);
  if(!data||typeof data!=='object'||Array.isArray(data)||data instanceof LosslessNumber)fail('SOURCE_SHAPE');
  for(const k of ["code","status_code"]) if(Object.hasOwn(data,k)&&String(value(data[k]))!=="0")fail("SOURCE_REJECTED");
  if(typeof data.data==="string" && /^\s*[{[]/.test(data.data)) {
    data=parseLossless(data.data);
    if(!data||typeof data!=='object'||Array.isArray(data)||data instanceof LosslessNumber)fail('SOURCE_SHAPE');
    for(const k of ["code","status_code"])if(Object.hasOwn(data,k)&&String(value(data[k]))!=="0")fail("SOURCE_REJECTED");
  }
  return data;
}
const proxy="/anchor_pc_tinker_proxy";
const paths={
 account:"/passport/account/info/v2/",
 history:"/lego/native/webcast_api/room/replay/history_list",
 base:"/lego/native/simple/room/replay/room_base_v2",
 summary:"/lego/native/webcast_api/room/replay/overview_v3",
 rank:"/lego/native/webcast_api/anchor/public/rank",
 content:"/lego/native/webcast_api/room/detail/room_stats_content_list",
 trend:"/lego/native/webcast_api/room/replay/minute_trend",
 profile:"/lego/native/webcast_api/anchor/audience/info_card"
};
const account=async()=>{const d=await read(paths.account);const f=fields(d.data||{},["user_id_str","sec_user_id","app_id"]);if(!f.user_id_str||f.user_id_str.type!=="string")fail("OWNER_UNAVAILABLE");return f;};
// Chrome transports may reorder dictionary keys. Compare exact values/types,
// never insertion order, coercion, nicknames or normalized identifier strings.
const equal=(a,b)=>{
 if(a===b)return true;
 if(!a||!b||typeof a!=="object"||typeof b!=="object"||Array.isArray(a)!==Array.isArray(b))return false;
 const keys=Object.keys(a);return keys.length===Object.keys(b).length&&keys.every(k=>Object.hasOwn(b,k)&&equal(a[k],b[k]));
};
try {
 if(!task || !/^[a-z_]+$/.test(task.op||""))fail("OP_NOT_ALLOWED");
 if(task.op.startsWith("chat_")) {
  if(origin!=="https://www.douyin.com")fail("SOURCE_ORIGIN");
  // Capture only this site's already loaded webpack runtime; no downloaded script/eval.
  const chunks=globalThis.webpackChunkdouyin_web;if(!Array.isArray(chunks)||chunks.push===Array.prototype.push)fail("CHAT_SDK_UNAVAILABLE");
  let req;const key="b_collector_"+crypto.randomUUID();chunks.push([[key],{},r=>{req=r;}]);
  // Prefer cache. Only the known client module may be requested on an already-open IM UI.
  // Do not enumerate exports, clear caches, create a conversation, send or mark read.
  let module=req?.c?.[767375]?.exports;
  const chatListReady=location.pathname==="/chat"&&typeof document!=="undefined"&&!!document.querySelector('[class*="conversationConversationListwrapper"], [data-e2e="conversation-list"]');
  if(!module&&(globalThis.conversationStore?.curConversation||chatListReady)&&typeof req?.m?.[767375]==="function") {
   const source=Function.prototype.toString.call(req.m[767375]);
   if(source.includes("bytedIMInstance")&&source.includes("ImSdk"))module=req(767375);
  }
  const sdk=module?.ImSdk?.bytedIMInstance;
  if(!sdk?.getConversationList)fail("CHAT_SDK_UNAVAILABLE");
  const owner=scalar(sdk.option?.userId);if(!owner||owner.type!=="string")fail("OWNER_UNAVAILABLE");
  if(task.owner&&!equal(owner,task.owner))changed("ACCOUNT_CHANGED");
  const list=sdk.getConversationList();if(!Array.isArray(list))fail("CHAT_SHAPE");
  const convPaths=["id","shortId","type","bizType","participantCount","toParticipantUserId","toParticipantSecUserId","coreInfo.owner"];
  const convProject=c=>fields(c,convPaths);
  if(task.op==="chat_index") {
   const start=Number.isInteger(task.offset)&&task.offset>=0?task.offset:0;
   return {ok:true,op:task.op,path:"/native-sdk/getConversationList",owner,app:null,observed_at:new Date().toISOString(),
    rows:list.slice(start,start+50).map(convProject),total:list.length,next:start+50<list.length?start+50:null,terminal:"cache_only",enum_basis:"unverified_current_build"};
  }
  if(task.op!=="chat_page" || typeof task.conversation!=="string")fail("OP_NOT_ALLOWED");
  const conv=list.find(c=>c.id===task.conversation);if(!conv||typeof conv.getMessageList!=="function")fail("CONVERSATION_UNAVAILABLE");
  const before=convProject(conv);let messages=conv.getMessageList();if(!Array.isArray(messages))fail("CHAT_SHAPE");
  let more=null;
  if(task.more===true) {
   if(typeof sdk.getMessagesByConversation!=="function"||!messages[0])fail("CHAT_HISTORY_UNAVAILABLE");
   // Only documented-in-current-client read method, existing cached conversation and cursor object.
   let timer;
   const result=await Promise.race([sdk.getMessagesByConversation({conversation:conv,cursor:messages[0],limit:50}),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error("SOURCE_TIMEOUT")),20000);})]).finally(()=>clearTimeout(timer));
   if(!Array.isArray(result?.messages))fail("CHAT_SHAPE");messages=result.messages;more=typeof result.hasMore==="boolean"?result.hasMore:null;
  }
  if(!equal(scalar(sdk.option?.userId),owner))changed("ACCOUNT_CHANGED");
  if(!equal(convProject(conv),before))changed("CONVERSATION_CHANGED");
  const keys=["serverId","clientId","id","conversationId","conversationShortId","sender","senderUserId","secSender","type","createdAt","createTime","indexInConversation","orderInConversation","isRecalled"];
  if(task.includeBody===true)keys.push("content");
  const offset=task.more===true?0:Math.max(0,task.offset||0);
  return {ok:true,op:task.op,path:"/native-sdk/getMessagesByConversation",owner,app:null,observed_at:new Date().toISOString(),conversation:before,
   rows:messages.slice(offset,offset+100).map(m=>fields(m,keys)),next:task.more===true?null:(offset+100<messages.length?offset+100:null),
   has_more:more,terminal:task.more===true&&more===false?"source_no_more":task.more===true?"unknown":"cache_only"};
 }
 if(origin!=="https://anchor.douyin.com"||!Object.hasOwn(paths,task.op))fail("SOURCE_ORIGIN");
 const before=await account();
 if(task.owner&&!equal(before.user_id_str,task.owner))changed("ACCOUNT_CHANGED");
 if(task.op==="account")return {ok:true,op:task.op,path:paths.account,owner:before.user_id_str,app:before.app_id||null,observed_at:new Date().toISOString(),rows:[before]};
 const p={};let path=proxy+paths[task.op];
 if(task.op==="history") {
  if(!/^\d{4}-\d\d-\d\d$/.test(task.day||""))fail("DATE_FORMAT");
  Object.assign(p,{anchorID:before.user_id_str.value,startDate:task.day,endDate:task.day,orderField:"startTimeUnix",sortType:"desc",needStats:"1",limit:400});
  // Current saved replay SDK uses the same configured proxy client (Nt).
  // Keep its camelCase parameters and raw field names; no identifier conversion.
 } else if(task.op==="rank") {
  if(!["score","comment_cnt","comment_cnt_realtime","watch_duration","like"].includes(task.rankType)||!["30d","7d","today","1d","last_live","room"].includes(task.period))fail("RANK_SELECTOR");
  if(task.period==='room'){
   if(typeof task.room!=='string'||!/^\d{1,40}$/.test(task.room))fail('ROOM_KEY_UNAVAILABLE');
   if(task.rankType==='comment_cnt')fail('RANK_SELECTOR');
   Object.assign(p,{anchorID:before.user_id_str.value,roomID:task.room,rankType:task.rankType,rankDuration:'room',limit:200});
  }else{
   if(task.room||task.rankType==='comment_cnt_realtime')fail('RANK_SELECTOR');
   Object.assign(p,{anchorID:before.user_id_str.value,rank_type:task.rankType,rank_duration:task.period,limit:200});
  }
 } else {
  if(typeof task.room!=="string"||!/^\d{1,40}$/.test(task.room))fail("ROOM_KEY_UNAVAILABLE");
  if(task.op==="base")Object.assign(p,{room_id:task.room,anchor_id:before.user_id_str.value});
  else Object.assign(p,{roomID:task.room,anchorID:before.user_id_str.value});
  if(task.op==="trend")p.cid="d2nc8ubc77u8uot7jfe0";
  if(task.op==="content") {
   if(!["1","2","4"].includes(task.contentType)||!/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(task.start||"")||!/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(task.end||""))fail("CONTENT_SELECTOR");
   Object.assign(p,{roomStatsContentType:task.contentType,startTime:task.start,endTime:task.end});
  }
  if(task.op==="profile") {if(typeof task.user!=="string"||!/^\d{1,40}$/.test(task.user))fail("USER_KEY_UNAVAILABLE");delete p.roomID;Object.assign(p,{room_id:task.room,user_id:task.user,is_real_time:0});}
 }
 const response=await read(path,p),dataStatus=httpStatus,after=await account();if(!equal(before,after))changed("ACCOUNT_CHANGED");
 requestStage='data';requestPhase='projection';httpStatus=dataStatus;
 let d=response.data??response,rows;
 if(!d||typeof d!=='object'||Array.isArray(d)||d instanceof LosslessNumber)fail('SOURCE_SHAPE');
 if(task.op==="rank")rows=d.ranks;
 else if(task.op==="profile")rows=[d];
 else rows=d.series??response.series;
 if(!Array.isArray(rows))fail("SOURCE_SHAPE");
 if(rows.length>50000)fail("SOURCE_ROW_LIMIT");
 const lists={
 history:["roomID","roomTitle","startTime","endTime","startTimeUnix","endTimeUnix","duration","liveDuration","serverWatchUcntTdDirect","roomLiveEarnScore","liveNewFollowUcnt","isNormalGroupLive"],
 base:["room_id","anchor_id","start_time","end_time","duration","title","room_title","status"],
 summary:["earn_score","consume_ucnt","consume_rate","show_ucnt","watch_ucnt","watch_rate","acu","pcu","avg_watch_duration_mins_rate","comment_ucnt","like_cnt","follow_ucnt","share_cnt","join_fansclub_ucnt","fans_like_cnt","non_fans_like_cnt","like_rate","room_id","anchor_id"],
 rank:["rank","value","watch_time","like_cnt","comment_cnt","contribute_value","accompany_day_cnt","user.id_str","user.id","user.sec_uid","user.nickname","user.display_id"],
 content:["userID","secUid","nickname","contentTime","time","contentCount","contentPrice","giftToAnchorID","giftToAnchor","message_id","msg_id","record_id","gift_id"],
 profile:["interact_index.watch_duration","interact_index.watch_duration_7d","interact_index.watch_cnt_7d","interact_index.consume_cnt_7d","interact_index.comment_cnt_7d"]
 };
 if(task.includeBody===true)lists.content.push("content");
 let projected;
 if(task.op==="trend") {
  let metadata=response.meta??d.meta;
  if(typeof metadata==="string") {try{metadata=parseLossless(metadata);}catch{metadata=null;}}
  // Dynamic chart scalar columns only. No whole metadata/response/credential payload.
  projected=rows.map(row=>fields(row,Object.keys(row||{}).filter(k=>/^[A-Za-z0-9_]{1,100}$/.test(k)&&!/(cookie|token|secret|auth|signature|password|content|body|text)/i.test(k)).slice(0,100)));
  // Retain metadata facts as separately labelled scalar observations, never infer units from names.
  if(metadata&&typeof metadata==="object") {
   for(const k of ["unit","title"])if(scalar(metadata[k]))projected.unshift({["meta."+k]:scalar(metadata[k])});
   for(const key of ["field","packages","categoryAxis","valueAxis"])if(Array.isArray(metadata[key]))for(let i=0;i<Math.min(200,metadata[key].length);i++) {
    const item=metadata[key][i],row={};const v=scalar(item);
    if(v)row[`meta.${key}[${i}]`]=v;
    else if(Array.isArray(item))item.slice(0,100).forEach((x,j)=>{const s=scalar(x);if(s)row[`meta.${key}[${i}][${j}]`]=s;});
    else for(const name of ["key","title","unit","field","name"]) {const s=scalar(item?.[name]);if(s)row[`meta.${key}[${i}].${name}`]=s;}
    if(Object.keys(row).length)projected.unshift(row);
   }
  }
 }else projected=rows.map(row=>fields(row,lists[task.op]));
 return {ok:true,op:task.op,path,owner:before.user_id_str,app:before.app_id||null,observed_at:new Date().toISOString(),rows:projected,
  received:rows.length,limit:task.op==="rank"?200:task.op==="history"?400:null,terminal:"unknown"};
} catch(error) {
 const codes=["OP_NOT_ALLOWED","SOURCE_ORIGIN","SOURCE_CHANGED","OWNER_UNAVAILABLE","SOURCE_HTTP","SOURCE_PERMISSION","SOURCE_CONTENT_TYPE","SOURCE_REJECTED","SOURCE_SHAPE","SOURCE_SIZE","SOURCE_JSON","SOURCE_DEPTH","SOURCE_ROW_LIMIT","CHAT_SDK_UNAVAILABLE","CHAT_SHAPE","CONVERSATION_UNAVAILABLE","CHAT_HISTORY_UNAVAILABLE","SOURCE_TIMEOUT","ROOM_KEY_UNAVAILABLE","USER_KEY_UNAVAILABLE","DATE_FORMAT","RANK_SELECTOR","CONTENT_SELECTOR"];
 const code=error?.name==='TimeoutError'?'SOURCE_TIMEOUT':codes.includes(error?.message)?error.message:'SOURCE_READ_FAILED';
 const diagnostic=['SOURCE_READ_FAILED','SOURCE_TIMEOUT','SOURCE_SHAPE','SOURCE_JSON','SOURCE_HTTP','SOURCE_REJECTED','SOURCE_CONTENT_TYPE','SOURCE_SIZE'].includes(code)?{operation:Object.hasOwn(paths,task?.op)?task.op:'unknown',stage:['account','data'].includes(requestStage)?requestStage:null,phase:['fetch','redirect','status','body','decode','projection'].includes(requestPhase)?requestPhase:null,http_status:Number.isInteger(httpStatus)&&httpStatus>=0&&httpStatus<=599?httpStatus:null,exception:['TypeError','TimeoutError','AbortError','SyntaxError','Error'].includes(error?.name)?error.name:'other'}:null;
 return {ok:false,error:code,...(diagnostic?{diagnostic}:{}),...(["ACCOUNT_CHANGED","CONVERSATION_CHANGED","HTTP_401","HTTP_403"].includes(error?.detail)?{detail:error.detail}:{}),...(["account","data"].includes(error?.requestStage)?{requestStage:error.requestStage}:{})};
}

}
