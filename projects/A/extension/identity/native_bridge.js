(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DSHNativeBridge=api;})(globalThis,function(){
  'use strict';
  // Synchronous correlation only. A page can observe/forge this nonce; it is not authentication.
  function read(doc,nonce,index){
    if(index!==undefined&&(!Number.isSafeInteger(index)||index<0||index>=1000))return null;let active=true,result=null,seen=false,duplicate=false;const start=performance.now();
    function reply(event){if(!active)return;try{const raw=event.detail;if(typeof raw!=='string'||raw.length>512)return;const v=JSON.parse(raw);
      if(!v||v.nonce!==nonce)return;
      if(seen){duplicate=true;result=null;return;}
      seen=true;if(Object.keys(v).sort().join(',')==='nonce,sec'&&typeof v.sec==='string'&&/^[A-Za-z0-9_-]{1,256}$/.test(v.sec)&&v.sec!=='self')result=v.sec;
    }catch{}}
    doc.addEventListener('dsh-native-reply-v2',reply);try{doc.dispatchEvent(new CustomEvent('dsh-native-read-v2',{detail:JSON.stringify(index===undefined?{nonce}:{nonce,index})}));}finally{active=false;doc.removeEventListener('dsh-native-reply-v2',reply);}
    return !duplicate&&performance.now()-start<=25?result:null;
  }
  const METADATA_REQUEST='dsh-native-metadata-read-v2',METADATA_REPLY='dsh-native-metadata-reply-v2';
  const METADATA_DIAGNOSTIC='dsh-native-metadata-diagnostic-v2';
  const diagnosticReasons=['store-missing','store-accessor','conversation-missing','conversation-accessor','metadata-unavailable'];
  const metadataPending=new WeakMap();
  function metadataValue(value){
    const keys=['format','authority','owner_uid','sec_uid','platform_conversation_key'];
    if(!value||Array.isArray(value)||Object.keys(value).length!==keys.length||keys.some(k=>!Object.hasOwn(value,k))||
      !['native-store-metadata-1','native-bound-conversation-metadata-1','native-bound-context-data-1','native-panel-context-data-1'].includes(value.format)||value.authority!=='unverified'||
      typeof value.owner_uid!=='string'||!/^[0-9]{1,256}(?![\s\S])/.test(value.owner_uid)||
      typeof value.sec_uid!=='string'||!/^[A-Za-z0-9_-]{1,256}(?![\s\S])/.test(value.sec_uid)||value.sec_uid==='self'||
      typeof value.platform_conversation_key!=='string'||value.platform_conversation_key.length>256||
      !/^(?:[A-Za-z0-9_-]+|[0-9]+(?::[0-9]+)+)(?![\s\S])/.test(value.platform_conversation_key))return null;
    return Object.freeze(Object.fromEntries(keys.map(k=>[k,value[k]])));
  }
  // Explicit untrusted transport only. Even a well-formed single reply can be
  // forged by the page; current() fences the caller's scope, not authenticity.
  function readCurrentStoreMetadata(doc,nonce,current,onDiagnostic=null){
    if(!doc||typeof doc!=='object')return null;
    const pending=metadataPending.get(doc);if(pending){pending.cancelled=true;return null;}
    if(typeof nonce!=='string'||!/^[-a-zA-Z0-9_]{16,80}(?![\s\S])/.test(nonce)||typeof current!=='function')return null;
    const state={cancelled:false};metadataPending.set(doc,state);
    let active=false,attached=false,seen=false,result=null,live=()=>false,reason=null,shape=null,diagnosticSeen=false;
    function diagnostic(event){
      if(!active)return;
      try{
        const raw=event.detail;if(typeof raw!=='string'||raw.length>256)return;
        const value=JSON.parse(raw);
        if(!value||value.nonce!==nonce)return;
        if(diagnosticSeen){reason=null;shape=null;return;}diagnosticSeen=true;
        const keys=Object.keys(value).sort().join(',');
        if(['nonce,reason','nonce,reason,shape'].includes(keys)&&diagnosticReasons.includes(value.reason)){
          reason=value.reason;
          if(keys==='nonce,reason,shape'&&reason==='conversation-accessor'&&typeof value.shape==='string'&&
            /^(?:b1:(?:unbound|unavailable)|b1:[yn][mda]{8}[01]{8}(?::p(?:[mda]{6}|unavailable))?)(?![\s\S])/.test(value.shape))shape=value.shape;
        }
      }catch{}
    }
    function reply(event){
      if(!active)return;
      try{
        const raw=event.detail;if(typeof raw!=='string'||raw.length>2048){state.cancelled=true;return;}
        const value=JSON.parse(raw);if(!value||value.nonce!==nonce)return;
        if(seen){state.cancelled=true;return;}seen=true;
        if(Array.isArray(value)||Object.keys(value).sort().join(',')!=='metadata,nonce'){state.cancelled=true;return;}
        result=metadataValue(value.metadata);
      }catch{state.cancelled=true;}
    }
    try{
      const start=performance.now(),view=doc.defaultView,origin=doc.location.origin,path=doc.location.pathname;
      const boundary=()=>view&&doc.defaultView===view&&view.document===doc&&view.top===view&&doc.visibilityState==='visible'&&
        origin==='https://www.douyin.com'&&/^\/chat(?:\/|$)/.test(path)&&doc.location.origin===origin&&doc.location.pathname===path;
      live=()=>{
        if(state.cancelled||!boundary()||current()!==true)return false;
        const elapsed=performance.now()-start;
        return !state.cancelled&&boundary()&&Number.isFinite(elapsed)&&elapsed>=0&&elapsed<=25;
      };
      if(live()){
        attached=true;doc.addEventListener(METADATA_REPLY,reply);doc.addEventListener(METADATA_DIAGNOSTIC,diagnostic);
        if(live()){
          active=true;doc.dispatchEvent(new CustomEvent(METADATA_REQUEST,{detail:JSON.stringify({nonce})}));active=false;
        }
      }
    }catch{state.cancelled=true;}
    finally{
      active=false;
      if(attached)for(const [name,listener]of [[METADATA_REPLY,reply],[METADATA_DIAGNOSTIC,diagnostic]]){
        try{doc.removeEventListener(name,listener);}catch{state.cancelled=true;}
      }
    }
    try{
      const valid=seen&&live();
      if(valid&&!result&&reason&&typeof onDiagnostic==='function')try{onDiagnostic(reason,shape);}catch{}
      return valid&&result?result:null;
    }
    catch{return null;}
    finally{metadataPending.delete(doc);}
  }
  return {read,readRow:(doc,nonce,index)=>read(doc,nonce,index),readCurrentStoreMetadata};
});
