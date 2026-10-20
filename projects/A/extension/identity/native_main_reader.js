(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else{
    // Idempotence only, never identity evidence. V2 events avoid collisions
    // with old page listeners left behind by an extension upgrade.
    if(root.__DSH_NATIVE_READER_V2__)return;
    Object.defineProperty(root,'__DSH_NATIVE_READER_V2__',{value:true});
    api.install(root.document);
    // Register only: no SDK read until an explicit, still-untrusted event.
    api.installStoreMetadata(root.document,root.document.defaultView);
  }
})(globalThis,function(){
  'use strict';
  const REQUEST='dsh-native-read-v2',REPLY='dsh-native-reply-v2';
  const HEADER='.componentsRightPanelwrapper .RightPanelHeaderconvHeader';
  const ROW='[data-e2e="conversation-item"].conversationConversationItemcurConversation';
  function data(o,k){if(!o||typeof o!=='object')return undefined;const d=Object.getOwnPropertyDescriptor(o,k);return d&&Object.prototype.hasOwnProperty.call(d,'value')?d.value:undefined;}
  function unique(scope,s){const a=scope.querySelectorAll(s);return a.length===1&&a[0].isConnected?a[0]:null;}
  function rowSec(row){
    const keys=Object.getOwnPropertyNames(row).filter(k=>k.startsWith('__reactProps$'));if(keys.length!==1)return null;
    const children=data(data(row,keys[0]),'children');if(!Array.isArray(children))return null;
    const sec=data(data(data(data(children,'2'),'props'),'conversation'),'_toParticipantSecUserId');
    return typeof sec==='string'&&/^[A-Za-z0-9_-]{1,256}$/.test(sec)&&sec!=='self'?sec:null;
  }
  function readRow(doc,index){try{
    if(!Number.isSafeInteger(index)||index<0||index>=1000)return null;
    const rows=doc.querySelectorAll('[data-e2e="conversation-item"]');
    if(rows.length>1000||index>=rows.length||!rows[index].isConnected)return null;
    return rowSec(rows[index]);
  }catch{return null;}}
  function boundHead(doc){try{
    const head=unique(doc,HEADER),row=unique(doc,ROW);if(!head||!row)return null;
    const title=unique(head,'.RightPanelHeadertitle');if(!title)return null;
    const user=unique(head,'.RightPanelHeaderuser');if(!user)return null;
    const keys=Object.getOwnPropertyNames(user).filter(k=>k.startsWith('__reactProps$'));if(keys.length!==1)return null;
    const props=data(user,keys[0]),children=data(props,'children');if(!Array.isArray(children))return null;
    const avatar=data(children,'0'),conversation=data(data(avatar,'props'),'conversation');
    const rowKeys=Object.getOwnPropertyNames(row).filter(k=>k.startsWith('__reactProps$'));if(rowKeys.length!==1)return null;
    const rowProps=data(row,rowKeys[0]),rowChildren=data(rowProps,'children');if(!Array.isArray(rowChildren))return null;
    const rowConversation=data(data(data(rowChildren,'2'),'props'),'conversation');
    const sec=data(conversation,'_toParticipantSecUserId');if(sec!==data(rowConversation,'_toParticipantSecUserId'))return null;
    if(typeof sec!=='string'||!(/^[A-Za-z0-9_-]{1,256}$/).test(sec)||sec==='self')return null;
    return {sec,conversation,rowConversation,refs:[head,row,title,user,props,children,conversation,rowProps,rowChildren,rowConversation]};
  }catch{return null;}}
  function read(doc){const bound=boundHead(doc);return bound?bound.sec:null;}
  function storeSample(doc,view){
    if(!doc||!view||doc.defaultView!==view||view.document!==doc||view.top!==view||doc.visibilityState!=='visible')return null;
    const {origin,pathname}=doc.location;
    if(origin!=='https://www.douyin.com'||!/^\/chat(?:\/|$)/.test(pathname))return null;
    const bound=boundHead(doc);if(!bound)return null;
    const store=data(view,'conversationStore'),conversation=data(store,'curConversation');
    const ctx=data(conversation,'ctx'),option=data(ctx,'option');
    const owner=data(option,'userId'),id=data(conversation,'id'),currentId=data(store,'curConversationId');
    // Reuse the existing DOM/React own-data secUid path; do not invoke public
    // peer getters, infer a peer UID from id, or use shortId as a replacement.
    const sec=bound.sec;
    if(typeof owner!=='string'||!(/^[0-9]{1,256}(?![\s\S])/).test(owner)||
      typeof id!=='string'||id.length>256||!(/^(?:[A-Za-z0-9_-]+|[0-9]+(?::[0-9]+)+)(?![\s\S])/).test(id)||
      !(/^[A-Za-z0-9_-]{1,256}(?![\s\S])/).test(sec)||id!==currentId||
      id!==data(bound.conversation,'id')||id!==data(bound.rowConversation,'id')||
      sec!==data(conversation,'_toParticipantSecUserId'))return null;
    // Recheck the document boundary after descriptor reads as well.
    if(doc.defaultView!==view||view.document!==doc||view.top!==view||doc.visibilityState!=='visible'||
      doc.location.origin!==origin||doc.location.pathname!==pathname||
      bound.refs.slice(0,4).some(node=>!node.isConnected))return null;
    return {origin,pathname,owner,sec,id,refs:[store,conversation,ctx,option,...bound.refs]};
  }
  // Explicit-call preparation only: the default install() never invokes this.
  // Accessors are not evaluated; descriptor reads can still trigger Proxy traps.
  // Two equal reads are not an atomic snapshot or continuing page authority.
  function readCurrentStoreMetadata(doc,view){try{
    const a=storeSample(doc,view);if(!a)return null;
    const b=storeSample(doc,view);
    if(!b||!['origin','pathname','owner','sec','id'].every(k=>a[k]===b[k])||
      a.refs.length!==b.refs.length||!a.refs.every((ref,n)=>ref===b.refs[n]))return null;
    return Object.freeze({format:'native-store-metadata-1',authority:'unverified',
      owner_uid:a.owner,sec_uid:a.sec,platform_conversation_key:a.id});
  }catch{return null;}}
  function boundConversationSample(doc,view){
    if(!doc||!view||doc.defaultView!==view||view.document!==doc||view.top!==view||doc.visibilityState!=='visible')return null;
    const {origin,pathname}=doc.location;
    if(origin!=='https://www.douyin.com'||!/^\/chat(?:\/|$)/.test(pathname))return null;
    const bound=boundHead(doc);
    if(!bound||bound.conversation!==bound.rowConversation||
      !bound.refs.slice(0,2).every(node=>node.getClientRects().length>0))return null;
    const conversation=bound.conversation,ctx=data(conversation,'ctx'),option=data(ctx,'option');
    const owner=data(option,'userId'),id=data(conversation,'id');
    if(typeof owner!=='string'||!/^[0-9]{1,256}(?![\s\S])/.test(owner)||
      typeof id!=='string'||id.length>256||!/^(?:[A-Za-z0-9_-]+|[0-9]+(?::[0-9]+)+)(?![\s\S])/.test(id))return null;
    if(doc.defaultView!==view||view.document!==doc||view.top!==view||doc.visibilityState!=='visible'||
      doc.location.origin!==origin||doc.location.pathname!==pathname||
      bound.refs.slice(0,4).some(node=>!node.isConnected))return null;
    return {origin,pathname,owner,sec:bound.sec,id,refs:[ctx,option,...bound.refs]};
  }
  // Separate candidate source contract. No store/getter fallback, inference or
  // page authority; the old reader's grant cannot approve this format.
  function readBoundConversationMetadata(doc,view){try{
    const a=boundConversationSample(doc,view);if(!a)return null;
    const b=boundConversationSample(doc,view);
    if(!b||!['origin','pathname','owner','sec','id'].every(k=>a[k]===b[k])||
      a.refs.length!==b.refs.length||!a.refs.every((ref,n)=>ref===b.refs[n]))return null;
    return Object.freeze({format:'native-bound-conversation-metadata-1',authority:'unverified',
      owner_uid:a.owner,sec_uid:a.sec,platform_conversation_key:a.id});
  }catch{return null;}}
  // Fixed projection reviewed against SDK module 752145 in public asset
  // async/15314.1abe9892.js. Neither ctx nor getContext is invoked. Source
  // equality is compatibility only, never proof of page authenticity.
  const functionSource=Function.prototype.toString;
  // Classic right-panel projection, reviewed in public pc-im asset
  // __federation_expose_default_export.4da7f841.js. Virtualized list rows
  // are not required, but an existing selected row must never be bypassed.
  function panelHead(doc){try{
    if(doc.querySelectorAll(ROW).length!==0)return null;
    const head=unique(doc,HEADER),panel=unique(doc,'.componentsRightPanelwrapper');
    if(!head||!panel||head.parentElement!==panel)return null;
    const title=unique(head,'.RightPanelHeadertitle'),user=unique(head,'.RightPanelHeaderuser');
    if(!title||!user)return null;
    const props=node=>{const keys=Object.getOwnPropertyNames(node).filter(k=>k.startsWith('__reactProps$'));return keys.length===1?data(node,keys[0]):undefined;};
    const hp=props(user),hc=data(hp,'children'),avatar=data(hc,'0'),ap=data(avatar,'props'),conversation=data(ap,'conversation');
    const pp=props(panel),pc=data(pp,'children'),parts=data(pc,'0'),header=data(parts,'0'),headerProps=data(header,'props');
    if(!Array.isArray(hc)||!Array.isArray(pc)||!Array.isArray(parts)||!conversation||
      conversation!==data(headerProps,'curConversation'))return null;
    const sec=data(conversation,'_toParticipantSecUserId');
    if(typeof sec!=='string'||!(/^[A-Za-z0-9_-]{1,256}(?![\s\S])/).test(sec)||sec==='self')return null;
    return {sec,conversation,rowConversation:conversation,refs:[head,panel,title,user,hp,hc,avatar,ap,conversation,pp,pc,parts,header,headerProps]};
  }catch{return null;}}
  function boundContextDataSample(doc,view,binder=boundHead){
    if(!doc||!view||doc.defaultView!==view||view.document!==doc||view.top!==view||doc.visibilityState!=='visible')return null;
    const {origin,pathname}=doc.location;
    if(origin!=='https://www.douyin.com'||!/^\/chat(?:\/|$)/.test(pathname))return null;
    const bound=binder(doc);
    if(!bound||bound.conversation!==bound.rowConversation||
      !bound.refs.slice(0,2).every(node=>node.getClientRects().length>0))return null;
    const wrapper=bound.conversation,p1=Object.getPrototypeOf(wrapper),p2=p1&&Object.getPrototypeOf(p1),p3=p2&&Object.getPrototypeOf(p2);
    if(!p1||!p2||!p3||new Set([wrapper,p1,p2,p3]).size!==4)return null;
    const descriptor=(object,key)=>Object.getOwnPropertyDescriptor(object,key);
    if(['id','__internal_ctx'].some(key=>descriptor(wrapper,key))||
      [wrapper,p1,p2].some(object=>['ctx','getContext'].some(key=>descriptor(object,key)))||
      [p2,p3].some(object=>descriptor(object,'id')))return null;
    const idField=descriptor(p1,'id'),contextField=descriptor(p1,'__internal_ctx'),
      getter=descriptor(p3,'ctx'),method=descriptor(p3,'getContext');
    if(!idField||!contextField||!Object.hasOwn(idField,'value')||!Object.hasOwn(contextField,'value')||
      !getter||Object.hasOwn(getter,'value')||typeof getter.get!=='function'||getter.set!==undefined||
      !method||!Object.hasOwn(method,'value')||typeof method.value!=='function'||
      functionSource.call(getter.get)!=='get ctx(){return this.getContext()}'||
      functionSource.call(method.value)!=='getContext(){return this.__internal_ctx}')return null;
    const ctx=contextField.value,option=data(ctx,'option'),owner=data(option,'userId'),id=idField.value;
    if(typeof owner!=='string'||!/^[0-9]{1,256}(?![\s\S])/.test(owner)||
      typeof id!=='string'||id.length>256||!/^(?:[A-Za-z0-9_-]+|[0-9]+(?::[0-9]+)+)(?![\s\S])/.test(id))return null;
    if(doc.defaultView!==view||view.document!==doc||view.top!==view||doc.visibilityState!=='visible'||
      doc.location.origin!==origin||doc.location.pathname!==pathname||bound.refs.slice(0,4).some(node=>!node.isConnected))return null;
    if(binder===panelHead&&(doc.querySelectorAll(ROW).length!==0||bound.refs[0].parentElement!==bound.refs[1]||
      unique(doc,'.componentsRightPanelwrapper')!==bound.refs[1]))return null;
    return {origin,pathname,owner,sec:bound.sec,id,
      refs:[p1,p2,p3,ctx,option,getter.get,method.value,...bound.refs]};
  }
  function readBoundContextDataMetadata(doc,view){try{
    const a=boundContextDataSample(doc,view);if(!a)return null;
    const b=boundContextDataSample(doc,view);
    if(!b||!['origin','pathname','owner','sec','id'].every(k=>a[k]===b[k])||
      a.refs.length!==b.refs.length||!a.refs.every((ref,n)=>ref===b.refs[n]))return null;
    return Object.freeze({format:'native-bound-context-data-1',authority:'unverified',
      owner_uid:a.owner,sec_uid:a.sec,platform_conversation_key:a.id});
  }catch{return null;}}
  function readPanelContextDataMetadata(doc,view){try{
    const a=boundContextDataSample(doc,view,panelHead);if(!a)return null;
    const b=boundContextDataSample(doc,view,panelHead);
    if(!b||!['origin','pathname','owner','sec','id'].every(k=>a[k]===b[k])||
      a.refs.length!==b.refs.length||!a.refs.every((ref,n)=>ref===b.refs[n]))return null;
    return Object.freeze({format:'native-panel-context-data-1',authority:'unverified',
      owner_uid:a.owner,sec_uid:a.sec,platform_conversation_key:a.id});
  }catch{return null;}}
  function prototypeShape(object){try{
    const seen=new Set([object]);let cursor=object,result='';
    for(let depth=0;depth<3;depth++){
      cursor=cursor===null?null:Object.getPrototypeOf(cursor);
      if(cursor!==null){if(seen.has(cursor))return 'unavailable';seen.add(cursor);}
      for(const key of ['id','ctx']){
        const d=cursor===null?undefined:Object.getOwnPropertyDescriptor(cursor,key);
        result+=!d?'m':Object.hasOwn(d,'value')?'d':'a';
      }
    }
    return result;
  }catch{return 'unavailable';}}
  function boundShape(doc){try{
    const b=boundHead(doc);if(!b)return 'b1:unbound';
    const describe=(o,k)=>{
      if(!o||typeof o!=='object')return {kind:'m',value:undefined};
      const d=Object.getOwnPropertyDescriptor(o,k);
      return !d?{kind:'m',value:undefined}:Object.hasOwn(d,'value')?{kind:'d',value:d.value}:{kind:'a',value:undefined};
    };
    const hi=describe(b.conversation,'id'),ri=describe(b.rowConversation,'id');
    const hc=describe(b.conversation,'ctx'),rc=describe(b.rowConversation,'ctx');
    const ho=describe(hc.value,'option'),ro=describe(rc.value,'option');
    const hu=describe(ho.value,'userId'),ru=describe(ro.value,'userId');
    const key=v=>typeof v==='string'&&v.length<=256&&/^(?:[A-Za-z0-9_-]+|[0-9]+(?::[0-9]+)+)(?![\s\S])/.test(v);
    const uid=v=>typeof v==='string'&&/^[0-9]{1,256}(?![\s\S])/.test(v);
    const bits=[key(hi.value),key(ri.value),uid(hu.value),uid(ru.value),
      b.refs[0].getClientRects().length>0,b.refs[1].getClientRects().length>0,
      key(hi.value)&&key(ri.value)&&hi.value===ri.value,uid(hu.value)&&uid(ru.value)&&hu.value===ru.value];
    // Fixed-order descriptor enums and booleans only. No values, snippets,
    // object enumeration, getter execution or diagnostic-based authorization.
    return 'b1:'+(b.conversation===b.rowConversation?'y':'n')+
      [hi,ri,hc,rc,ho,ro,hu,ru].map(d=>d.kind).join('')+bits.map(v=>v?'1':'0').join('')+
      (b.conversation===b.rowConversation&&hi.kind==='m'&&hc.kind==='m'?':p'+prototypeShape(b.conversation):'');
  }catch{return 'b1:unavailable';}}
  function install(doc){doc.addEventListener(REQUEST,event=>{try{
    const raw=event.detail;if(typeof raw!=='string'||raw.length>128)return;
    const req=JSON.parse(raw);if(!req||![1,2].includes(Object.keys(req).length)||Object.keys(req).some(k=>!['nonce','index'].includes(k))||(Object.hasOwn(req,'index')&&(!Number.isSafeInteger(req.index)||req.index<0||req.index>=1000))||typeof req.nonce!=='string'||!/^[-a-zA-Z0-9_]{16,80}$/.test(req.nonce))return;
    doc.dispatchEvent(new CustomEvent(REPLY,{detail:JSON.stringify({nonce:req.nonce,sec:Object.hasOwn(req,'index')?readRow(doc,req.index):read(doc)})}));
  }catch{}});}
  const METADATA_REQUEST='dsh-native-metadata-read-v2',METADATA_REPLY='dsh-native-metadata-reply-v2';
  const METADATA_DIAGNOSTIC='dsh-native-metadata-diagnostic-v2';
  function unavailableReason(view){
    // Diagnostic only: inspect the two documented entry descriptors without
    // invoking getters. This never supplies metadata or authorizes a match.
    try{
      const store=Object.getOwnPropertyDescriptor(view,'conversationStore');
      if(!store)return 'store-missing';
      if(!Object.hasOwn(store,'value'))return 'store-accessor';
      if(!store.value||typeof store.value!=='object')return 'store-missing';
      const conversation=Object.getOwnPropertyDescriptor(store.value,'curConversation');
      if(!conversation)return 'conversation-missing';
      if(!Object.hasOwn(conversation,'value'))return 'conversation-accessor';
    }catch{}
    return 'metadata-unavailable';
  }
  const metadataInstallations=new WeakMap();
  // The browser entry installs once per declared script load. Page scripts can
  // also send these events; their nonce is correlation, never authentication.
  function installStoreMetadata(doc,view){
    if(metadataInstallations.has(doc))throw Error('metadata_already_installed');
    let enabled=true,busy=false,collided=false;
    function receive(event){try{
      if(!enabled)return;
      const raw=event.detail;if(typeof raw!=='string'||raw.length>128)return;
      const req=JSON.parse(raw);
      if(!req||Array.isArray(req)||Object.keys(req).length!==1||!Object.hasOwn(req,'nonce')||typeof req.nonce!=='string'||
        !/^[-a-zA-Z0-9_]{16,80}(?![\s\S])/.test(req.nonce))return;
      if(busy){collided=true;return;}
      busy=true;collided=false;
      try{
        let metadata=readCurrentStoreMetadata(doc,view);
        // A known accessor-shaped store may use the separately labelled DOM
        // candidate reader. Missing/mismatching ordinary store data never does.
        if(!metadata&&unavailableReason(view)==='conversation-accessor'){
          metadata=readBoundConversationMetadata(doc,view)||readBoundContextDataMetadata(doc,view)||readPanelContextDataMetadata(doc,view);
        }
        if(enabled&&!collided){
          if(!metadata){
            const reason=unavailableReason(view),diagnostic={nonce:req.nonce,reason};
            if(reason==='conversation-accessor')diagnostic.shape=boundShape(doc);
            doc.dispatchEvent(new CustomEvent(METADATA_DIAGNOSTIC,{detail:JSON.stringify(diagnostic)}));
          }
          if(enabled&&!collided)doc.dispatchEvent(new CustomEvent(METADATA_REPLY,{detail:JSON.stringify({nonce:req.nonce,metadata})}));
        }
      }finally{busy=false;}
    }catch{/* Missing or incompatible metadata remains unknown. */}}
    function dispose(){
      if(!enabled)return;
      enabled=false;metadataInstallations.delete(doc);doc.removeEventListener(METADATA_REQUEST,receive);
    }
    doc.addEventListener(METADATA_REQUEST,receive);metadataInstallations.set(doc,dispose);
    return dispose;
  }
  return {read,readRow,readCurrentStoreMetadata,readBoundConversationMetadata,readBoundContextDataMetadata,readPanelContextDataMetadata,installStoreMetadata,install,REQUEST,REPLY,METADATA_REQUEST,METADATA_REPLY};
});
