import {NativeClient} from './engine.js';
import {uuid,raw} from './core.js';
import {chatMediaUrls} from './chat-media.js';

export async function waitForChatReady(source,tabId,check,delay=ms=>new Promise(r=>setTimeout(r,ms))){
 for(let attempt=0;attempt<30;attempt++){
  check();
  try{const result=await source.read(tabId,{op:'chat_index',offset:0,includeBody:false});check();return result;}
  catch(e){if(!['CHAT_SDK_UNAVAILABLE','CHAT_SDK_SIGNATURE_NOT_FOUND','OWNER_UNAVAILABLE'].includes(e.message))throw e;}
  await delay(1000);
 }
 check();throw Error('CHAT_LOGIN_OR_SOURCE_NOT_READY');
}

export class ChatNativeClient extends NativeClient {
 constructor(api=chrome){super(api);}
 async connect(){
  if(this.port)return;
  this.port=this.api.runtime.connectNative('com.bline.chatcollector');
  this.port.onMessage.addListener(m=>{const p=this.pending.get(m.id);if(p){clearTimeout(p.timer);this.pending.delete(m.id);m.ok?p.resolve(m.result):p.reject(Error(m.error||'CHAT_HOST_FAILED'));}});
  this.port.onDisconnect.addListener(()=>{void this.api.runtime.lastError;this.port=null;for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(Error('NATIVE_DISCONNECTED'));}this.pending.clear();this.onDisconnect();});
  return this.call('hello',{});
 }
 call(op,args){
  if(!this.port)return Promise.reject(Error('NATIVE_DISCONNECTED'));
  return new Promise((resolve,reject)=>{const id=uuid(),timer=setTimeout(()=>{this.pending.delete(id);reject(Error('NATIVE_TIMEOUT'));this.port?.disconnect();},op==='chat_prepare'?180000:60000);this.pending.set(id,{resolve,reject,timer});this.port.postMessage({protocol:'b-native/1',id,op,args});});
 }
}

export function bindIncremental(collector,host,onStats=()=>{}){
 let mediaMissing=0,trackedRun=null;
 collector.filterChatRows=async({owner,conversation,rows,sourceRows,epoch})=>{
  if(trackedRun!==collector.run?.run_id){trackedRun=collector.run?.run_id;mediaMissing=0;}
  const kept=[];
  const update=r=>{const next=r.stats.media_failed+r.stats.media_missing_url;collector.missing+=next-mediaMissing;mediaMissing=next;onStats(r.stats);};
  for(const row of rows){collector.check(epoch);const result=await host.call('chat_prepare',{...collector.run,owner,conversation,rows:[row],urls:[chatMediaUrls(row)],source_times:[]});collector.check(epoch);kept.push(...result.rows);update(result);}
  const result=await host.call('chat_prepare',{...collector.run,owner,conversation,rows:[],urls:[],source_times:sourceRows.map(r=>{const t=raw(r['b_chat.created_at_iso']);return typeof t==='string'&&Number.isFinite(Date.parse(t))?Date.parse(t)/1000:null;})});
  collector.check(epoch);update(result);return {...result,rows:kept};
 };
}
