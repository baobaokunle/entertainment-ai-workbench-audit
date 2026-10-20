(function(){
  'use strict';
  if(!DSHProtocol.chatURL(location.href)||!document.body||window.top!==window)return;
  if(globalThis.__DSH_CHAT_LIFECYCLE_V2__?.alive())return;
  const sidebar=DSHSidebar.mount(document,new DSHChat.UnverifiedChatAdapter().capability(),{editor:editProfile});
  const binding=DSHNativeBinding;
  const divMode=binding.mode==='native-div-v1';
  let snapshot=null,recallSnapshot=null,recallReady=false,recallSent=false,recallEligible=false,recallAt=0;
  let selectionIntent=null;
  let resumeCycle=null,resumeRequest=null,wasHidden=document.visibilityState!=='visible';
  function allowResume(){
    const now=Date.now();
    resumeCycle={started:now,deadline:now+15000,snapshot:null,stableAt:0};resumeRequest=null;
  }
  function cancelResume(){resumeCycle=null;resumeRequest=null;}
  function maybeResume(){
    if(resumeRequest&&Date.now()>=resumeRequest.displayUntil)resumeRequest=null;
    if(!resumeCycle||disposed||selectionIntent||!divMode||document.visibilityState!=='visible'||armUntil>Date.now())return;
    const now=Date.now(),cycle=resumeCycle;
    if(now<cycle.started||now>=cycle.deadline){resumeCycle=null;return;}
    const live=capture();
    if(!live||!live.row||!same(snapshot,live)||!metadata().header_bound){cycle.snapshot=null;cycle.stableAt=0;return;}
    if(!same(cycle.snapshot,live)){cycle.snapshot=live;cycle.stableAt=now;return;}
    if(!recallReady||now-cycle.stableAt<500)return;
    // One opportunity belongs to this page/foreground lifecycle, not to each
    // context. DOM changes, expiry, heartbeat and auth retries cannot renew it.
    resumeCycle=null;
    resumeRequest={snapshot:live,context,generation,headToken,token:crypto.randomUUID(),started:now,displayUntil:now+30000,displayed:false};
    recallEligible=false;recallSnapshot=null;
    send('identity-pilot-resume',{...metadata(),sec_uid:live.sec,resume_token:resumeRequest.token});
  }
  const ALL_ROWS='[data-e2e="conversation-item"]';
  function intentRowKey(intent){
    if(!intent||!intent.row.isConnected||intent.route!==location.href)return null;
    const rows=document.querySelectorAll(ALL_ROWS),index=Array.prototype.indexOf.call(rows,intent.row);
    if(rows.length>1000||index<0)return null;
    return DSHNativeBridge.readRow(document,crypto.randomUUID(),index);
  }
  function validIntent(intent){
    const now=Date.now();
    return !!intent&&document.visibilityState==='visible'&&now>=intent.started&&now<(intent.sent?intent.displayUntil:intent.deadline)&&intentRowKey(intent)===intent.sec;
  }
  function maybeSelection(){
    const intent=selectionIntent;if(!intent)return;
    if(!validIntent(intent)){reset('selection-invalid');return;}
    const live=capture();
    if(!live||live.row!==intent.row||live.sec!==intent.sec){intent.stableAt=0;return;}
    if(!same(snapshot,live)){reset('native-identity-changed');return;}
    if(!intent.stableAt){intent.stableAt=Date.now();return;}
    if(intent.sent||!recallReady||Date.now()-intent.stableAt<100)return;
    intent.sent=true;intent.context=context;intent.generation=generation;intent.headToken=headToken;intent.displayUntil=Date.now()+30000;
    recallEligible=false;recallSnapshot=null;
    send('identity-pilot-selection',{...metadata(),sec_uid:intent.sec,selection_token:intent.token});
  }
  const navTrace=[];
  const tracePattern=/^[1-9][0-9]{0,2}:(native|replay):(entry|resume):(blank|expected|other):(fresh|same|replaced):(newer|same|older):(idle|pending_navigation|pending_profile|candidate|failed)(?::(link|typed|auto_bookmark|auto_subframe|manual_subframe|generated|auto_toplevel|form_submit|reload|keyword|keyword_generated|unknown):([01]{4}|unknown))?$/;
  function capture(){
    if(!divMode)return null;
    const h=currentHead(),rows=document.querySelectorAll(binding.conversationSelector);
    const titles=h?h.querySelectorAll(binding.titleSelector):[];
    if(h&&rows.length===0&&titles.length===1&&titles[0].isConnected&&!own(titles[0])){
      const panels=document.querySelectorAll('.componentsRightPanelwrapper'),panel=panels.length===1?panels[0]:null;
      if(!panel||!panel.isConnected||h.parentElement!==panel||own(panel))return null;
      const route=location.href,metadata=DSHNativeBridge.readCurrentStoreMetadata(document,crypto.randomUUID(),()=>true);
      if(!metadata||metadata.format!=='native-panel-context-data-1'||route!==location.href||h!==currentHead()||
        !panel.isConnected||h.parentElement!==panel||document.querySelectorAll(binding.conversationSelector).length!==0||
        document.querySelectorAll('.componentsRightPanelwrapper').length!==1||
        h.querySelectorAll(binding.titleSelector).length!==1||h.querySelectorAll(binding.titleSelector)[0]!==titles[0])return null;
      return {head:h,row:null,panel,title:titles[0],route,sec:metadata.sec_uid,owner:metadata.owner_uid,key:metadata.platform_conversation_key};
    }
    if(!h||rows.length!==1||!rows[0].isConnected||titles.length!==1||!titles[0].isConnected||own(rows[0])||own(titles[0]))return null;
    const route=location.href,sec=DSHNativeBridge.read(document,crypto.randomUUID());
    if(!sec||route!==location.href||h!==currentHead())return null;
    const rows2=document.querySelectorAll(binding.conversationSelector),titles2=h.querySelectorAll(binding.titleSelector);
    if(rows2.length!==1||rows2[0]!==rows[0]||titles2.length!==1||titles2[0]!==titles[0])return null;
    return {head:h,row:rows[0],title:titles[0],route,sec};
  }
  function same(a,b){return !!a&&!!b&&['head','row','panel','title','route','sec','owner','key'].every(k=>a[k]===b[k]);}
  function stable(a,b){return (a===null&&b===null)||same(a,b);}
  let generation=0,context=crypto.randomUUID(),headToken=crypto.randomUUID(),head=null,disposed=false,expires=0,heartbeatAt=0,armUntil=0;
  let savedRevoked=false,savedShown=false,savedQualified=false;
  function currentHead(){
    if(!binding.enabled)return null;
    const nodes=document.querySelectorAll(binding.headerSelector);
    return nodes.length===1&&nodes[0].isConnected&&!nodes[0].closest('[data-dsh-owned]')?nodes[0]:null;
  }
  function metadata(){return {context_id:context,generation,head_token:headToken,header_bound:!!head&&head===currentHead()};}
  async function editProfile(command){
    const scope=metadata();
    const live=()=>receiptCurrent(scope)&&savedShown&&savedQualified&&!savedRevoked&&Date.now()<expires&&document.visibilityState==='visible'&&
      DSHProtocol.chatURL(location.href)&&scope.header_bound&&(divMode?same(snapshot,capture()):head===currentHead());
    if(!live())throw Error('stale_session');
    const result=await chrome.runtime.sendMessage({type:'identity-profile-edit',value:{...scope,command}});
    if(!live())throw Error('stale_session');
    if(!result||result.ok!==true)throw Error(result?.error||'unavailable');
    return result.data;
  }
  function diagnostic(key,value){if(typeof sidebar.diagnostic==='function')sidebar.diagnostic(key,value);}
  function receiptCurrent(value){return !disposed&&value.context_id===context&&value.generation===generation&&value.head_token===headToken;}
  function savedHeartbeatReceipt(value,result){
    if(!receiptCurrent(value)||!savedShown||!savedQualified||savedRevoked)return;
    const data=(object,key)=>{const d=object&&Object.getOwnPropertyDescriptor(object,key);return d&&Object.hasOwn(d,'value')?d.value:undefined;};
    let current=false;
    try{current=data(result,'ok')===true&&data(data(result,'data'),'state')==='saved_current'&&Date.now()<expires;}catch{}
    // A receipt can only retain the existing deadline or clear this display.
    // It cannot grant a binding, retry a lookup or revive a consumed context.
    if(!current){savedRevoked=true;expires=0;sidebar.showIdentityPilot({state:'pending',historical_identity:'not_checked'});}
  }
  function receipt(type,value,result){
    const key=type==='identity-pilot-selection'?'selection-ack':type==='identity-pilot-resume'?'resume-ack':null;
    if(!key||!receiptCurrent(value))return;
    // Read only data properties: transport errors and arbitrary values stay local.
    const ownValue=(object,name)=>{if(!object||typeof object!=='object')return undefined;const descriptor=Object.getOwnPropertyDescriptor(object,name);return descriptor&&Object.hasOwn(descriptor,'value')?descriptor.value:undefined;};
    let state;try{if(ownValue(result,'ok')===true)state=ownValue(ownValue(result,'data'),'state');}catch{}
    const allowed=key==='selection-ack'?['unbound','budget_exhausted','stale','pending','selection_candidate']:['unbound','stale','pending','resumed_candidate'];
    diagnostic(key,allowed.includes(state)?state:'rejected');
  }
  function transportLost(){
    if(disposed)return;
    expires=0;
    sidebar.showIdentityPilot({state:'pending',historical_identity:'not_checked',connection:'service_unavailable'});
    dispose(false);
  }
  function maybeRecall(){
    if(resumeRequest||selectionIntent||disposed||!divMode||!recallEligible||!recallReady||recallSent||!snapshot||document.visibilityState!=='visible'||Date.now()<recallAt)return;
    const live=capture();if(live&&!live.row)return;
    if(!same(snapshot,live)||!metadata().header_bound){reset('native-identity-changed');return;}
    recallSent=true;recallSnapshot=snapshot;
    send('identity-pilot-recall',{...metadata(),sec_uid:live.sec});
  }
  function send(type,value){
    if(disposed)return;
    const receiptKey=type==='identity-pilot-selection'?'selection-ack':type==='identity-pilot-resume'?'resume-ack':null;
    if(receiptKey)diagnostic(receiptKey,'sent');
    if(type==='identity-pilot-arm'){armUntil=Date.now()+30000;recallEligible=false;recallSnapshot=null;}
    try{Promise.resolve(chrome.runtime.sendMessage({type,value})).then(result=>{
    if(disposed)return;
    if(type==='identity-saved-profile-heartbeat'){savedHeartbeatReceipt(value,result);return;}
    receipt(type,value,result);
    if(type==='identity-pilot-context'&&value.context_id===context&&value.generation===generation&&value.head_token===headToken){
      recallReady=!!(result&&result.ok&&result.data&&result.data.state==='ready');maybeSelection();maybeResume();maybeRecall();
    }
    if(type==='identity-pilot-arm'&&value.context_id===context&&value.generation===generation){
      const state=result&&result.ok&&result.data&&result.data.state;
      diagnostic('arm-ack',['armed','unbound','stale','unavailable'].includes(state)?state:'rejected');
      if(state==='unbound')reset('native-host-unbound');
    }
  }).catch(()=>{
    if(type==='identity-saved-profile-heartbeat'){savedHeartbeatReceipt(value,null);return;}
    receipt(type,value,null);if(!receiptKey||receiptCurrent(value))transportLost();
  });}catch{
    if(type==='identity-saved-profile-heartbeat'){savedHeartbeatReceipt(value,null);return;}
    receipt(type,value,null);if(!receiptKey||receiptCurrent(value))transportLost();
  }}
  function reset(reason='context-reset'){
    if(disposed)return;
    const observed=selectionIntent?capture():null;
    const keep=selectionIntent&&(!observed||observed.row===selectionIntent.row||observed.row===selectionIntent.previousRow)&&!selectionIntent.sent&&['native-dom-changed','native-identity-changed'].includes(reason)&&validIntent(selectionIntent);
    if(!keep)selectionIntent=null;
    resumeRequest=null;
    if(reason==='candidate-expired')resumeCycle=null;
    if(resumeCycle){resumeCycle.snapshot=null;resumeCycle.stableAt=0;}
    navTrace.length=0;diagnostic('nav-trace','');
    expires=0;
    armUntil=0;heartbeatAt=Date.now()+20000;
    savedRevoked=false;savedShown=false;savedQualified=false;
    diagnostic('metadata-status','not-read');
    diagnostic('metadata-shape','not-read');
    recallReady=false;recallSent=false;recallSnapshot=null;recallAt=Date.now()+300;
    recallEligible=!['native-other-gesture','candidate-expired'].includes(reason);
    sidebar.showIdentityPilot({state:'pending',historical_identity:'not_checked',header_bound:false});
    generation++;context=crypto.randomUUID();headToken=crypto.randomUUID();head=currentHead();snapshot=capture();if(divMode&&!snapshot)head=null;
    send('identity-pilot-context',metadata());
    diagnostic('generation',generation);diagnostic('reset-reason',reason);diagnostic('arm-ack','none');diagnostic('selection-ack','none');diagnostic('resume-ack','none');diagnostic('nav-phase','none');diagnostic('nav-reason','none');diagnostic('nav-passed','false');
  }
  function own(target){return target instanceof Element&&!!target.closest('[data-dsh-owned]');}
  function nativeGesture(event){
    if(disposed)return;
    if(!event.isTrusted)return;
    if(own(event.target)){if(selectionIntent)reset('selection-other-gesture');return;}
    const hadResume=!!resumeRequest;
    cancelResume();
    if(hadResume)reset('resume-new-gesture');
    if(selectionIntent)reset('selection-new-gesture');
    if(divMode){
      const target=event.target instanceof Element?event.target:null;
      const row=target&&target.closest(ALL_ROWS);
      const primary=(event.type==='pointerdown'&&event.button===0)||(event.type==='keydown'&&event.key==='Enter'&&!event.repeat);
      if(row&&primary&&!event.ctrlKey&&!event.altKey&&!event.shiftKey&&!event.metaKey&&
        !target.closest('button,a,input,textarea,select,[contenteditable],[role="button"],[role="menuitem"],[role="menu"],[role="link"]')){
        const rows=document.querySelectorAll(ALL_ROWS),index=Array.prototype.indexOf.call(rows,row);
        const sec=rows.length<=1000&&index>=0&&row.isConnected?DSHNativeBridge.readRow(document,crypto.randomUUID(),index):null;
        const before=capture();reset('selection-gesture');
        if(sec&&document.visibilityState==='visible'&&row.isConnected){
          const now=Date.now();selectionIntent={row,sec,route:location.href,token:crypto.randomUUID(),started:now,deadline:now+3000,sent:false,stableAt:0,previousRow:before&&before.row};
          maybeSelection();
        }
        return;
      }
      const live=capture();
      if(!stable(snapshot,live)){reset('native-identity-changed');return;}
      if(!snapshot)return;
      // Rowless candidates belong only to the separately authorized workbench
      // metadata reader, never the older navigation/recall pilot.
      if(!snapshot.row){reset('native-other-gesture');return;}
      if(event.target instanceof Element&&(event.target===snapshot.title||snapshot.title.contains(event.target))&&
        ((event.type==='pointerdown'&&event.button===0)||(event.type==='keydown'&&event.key==='Enter'))){
        const url='https://www.douyin.com/user/'+snapshot.sec+'?from_tab_name=main';
        try{DSHProfileNavigation.parseProfileURL(url);}catch{reset();return;}
        send('identity-pilot-arm',{...metadata(),profile_url:url});return;
      }
      reset('native-other-gesture');return;
    }
    const anchor=event.target instanceof Element?event.target.closest('a[href]'):null;
    // No nickname, avatar, message body, input or page-wide identity scan.
    if(head&&head===currentHead()&&anchor&&head.contains(anchor)&&anchor.target==='_blank'&&
      ((event.type==='pointerdown'&&event.button===0)||(event.type==='keydown'&&event.key==='Enter'))){
      let url;try{url=anchor.href;DSHProfileNavigation.parseProfileURL(url);}catch{reset();return;}
      send('identity-pilot-arm',{...metadata(),profile_url:url});return;
    }
    reset();
  }
  function divMutationInvalidates(record){
    const target=record.target;
    // Classify text nodes through their parent without reading their value.
    if(own(target)||(!(target instanceof Element)&&own(target.parentElement)))return false;
    if(target===snapshot.title||snapshot.title.contains(target))return true;
    if(record.type==='childList'){
      for(const node of [...record.addedNodes,...record.removedNodes]){
        if([snapshot.head,snapshot.row,snapshot.panel,snapshot.title].filter(Boolean).some(ref=>node===ref||(node instanceof Element&&node.contains(ref))))return true;
      }
    }
    if(record.type==='attributes'&&target instanceof Element){
      const name=record.attributeName;
      const conversation=target===snapshot.row||target.getAttribute('data-e2e')==='conversation-item'||(name==='data-e2e'&&record.oldValue==='conversation-item');
      if(conversation&&['class','aria-selected','data-e2e'].includes(name)){
        const final=target.getAttribute(name);
        if(name==='class'){
          const selected=value=>typeof value==='string'&&value.split(/\s+/).includes('conversationConversationItemcurConversation');
          if(selected(record.oldValue)!==selected(final))return true;
        }else if(record.oldValue!==final)return true;
      }
    }
    return false;
  }
  const observer=new MutationObserver(records=>{
    if(!divMode){if(records.some(r=>!own(r.target)))reset('native-dom-changed');return;}
    if(selectionIntent){
      const intent=selectionIntent,live=capture();
      const removed=records.some(r=>r.type==='childList'&&Array.from(r.removedNodes||[]).some(n=>n===intent.row||(n instanceof Element&&n.contains(intent.row))));
      if(removed||!validIntent(intent)||(live&&live.row!==intent.row&&live.row!==intent.previousRow)){reset('selection-invalid');return;}
    }
    if(!stable(snapshot,capture())||(snapshot&&records.some(divMutationInvalidates)))reset('native-dom-changed');
    maybeSelection();
    maybeResume();
  });
  observer.observe(document.body,divMode?
    {subtree:true,childList:true,characterData:true,attributes:true,attributeOldValue:true}:
    {subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['class','aria-selected','data-e2e','href']});
  document.addEventListener('pointerdown',nativeGesture,true);document.addEventListener('keydown',nativeGesture,true);
  const message=(value,sender,respond)=>{
    if(value&&value.type==='native-metadata-read'){
      let result=null;
      try{
        // Browser-supplied sender data, not page fields. An absent worker URL
        // is tolerated; a supplied URL must name this extension's background.
        if(sender&&sender.id===chrome.runtime.id&&!sender.tab&&
          (sender.url===undefined||sender.url===`chrome-extension://${chrome.runtime.id}/background.js`)){
          const request=DSHProtocol.nativeMetadataRequest(value),expected=request.scope;
          const current=(checkIdentity=true)=>!disposed&&!savedRevoked&&divMode&&document.visibilityState==='visible'&&
            expected.context_id===context&&expected.generation===generation&&expected.head_token===headToken&&
            !!head&&head===currentHead()&&DSHProtocol.chatURL(location.href)&&
            (!checkIdentity&&snapshot&&snapshot.panel?snapshot.panel.isConnected&&head.parentElement===snapshot.panel:same(snapshot,capture()));
          if(current()){
            diagnostic('metadata-status','reading');
            diagnostic('metadata-shape','not-read');
            // Panel capture itself uses this synchronous transport. Keep its
            // full identity checks outside the pending bridge request; reentry
            // is deliberately rejected by the bridge. Recheck fully afterward.
            const metadata=DSHNativeBridge.readCurrentStoreMetadata(document,crypto.randomUUID(),()=>current(false),(reason,shape)=>{
              if(current(false)&&['store-missing','store-accessor','conversation-missing','conversation-accessor','metadata-unavailable'].includes(reason))diagnostic('metadata-status',reason);
              if(current(false)&&typeof shape==='string'&&/^(?:b1:(?:unbound|unavailable)|b1:[yn][mda]{8}[01]{8}(?::p(?:[mda]{6}|unavailable))?)(?![\s\S])/.test(shape))diagnostic('metadata-shape',shape);
            });
            if(current()&&metadata)diagnostic('metadata-status','unverified-metadata');
            if(metadata&&current())result=DSHProtocol.nativeMetadataResponse({scope:expected,metadata});
          }
        }
      }catch{/* No retry, context reset, lookup or display on a failed read. */}
      if(typeof respond==='function')respond(result);
      return;
    }
    if(sender.id!==chrome.runtime.id||!value||!['identity-pilot-view','identity-pilot-diagnostic','native-saved-profile-view','native-qualified-profile-view','native-saved-profile-clear','native-saved-profile-renew'].includes(value.type)||value.context_id!==context||value.generation!==generation||disposed)return;
    if(!DSHProtocol.chatURL(location.href)){dispose();return;}
    if(divMode?!stable(snapshot,capture()):head!==currentHead()){reset();return;}
    if(value.type==='native-saved-profile-renew'){
      const now=Date.now();
      const accepted=!sender.tab&&(sender.url===undefined||sender.url===`chrome-extension://${chrome.runtime.id}/background.js`)&&
        Object.keys(value).length===5&&savedShown&&savedQualified&&!savedRevoked&&document.visibilityState==='visible'&&
        !!head&&value.head_token===headToken&&(divMode?same(snapshot,capture()):head===currentHead())&&
        now<expires&&Number.isFinite(value.expires_at)&&value.expires_at>expires&&value.expires_at<=now+30000;
      // Refresh the existing card's lease only. No profile data or new display.
      if(accepted)expires=value.expires_at;
      if(typeof respond==='function')respond({state:accepted?'renewed':'stale',expires_at:accepted?expires:null});
      return;
    }
    if(value.type==='native-saved-profile-clear'){
      if(value.head_token!==headToken)return;
      savedRevoked=true;expires=0;sidebar.showIdentityPilot({state:'pending'});return;
    }
    if(value.type==='native-saved-profile-view'||value.type==='native-qualified-profile-view'){
      const current=()=>!disposed&&!savedRevoked&&!savedShown&&document.visibilityState==='visible'&&!!head&&
        value.context_id===context&&value.generation===generation&&value.head_token===headToken&&
        DSHProtocol.chatURL(location.href)&&(divMode?same(snapshot,capture()):head===currentHead())&&
        Number.isFinite(value.expires_at)&&value.expires_at>Date.now()&&value.expires_at<=Date.now()+30000;
      const qualified=value.type==='native-qualified-profile-view';
      if(qualified?(value.format!=='native-qualified-profile-view-1'||value.fixture!==false||!value.result||value.result.fixture!==false):
        (value.fixture!==true||!value.result||value.result.fixture!==true))return;
      if(!current())return;
      const shown=sidebar.showSavedProfile(value.result,value.binding,current,qualified);
      if(shown&&shown.summary_state==='saved_binding'&&current()){expires=value.expires_at;savedShown=true;savedQualified=qualified;}
      return;
    }
    if(document.visibilityState==='hidden'&&value.type!=='identity-pilot-diagnostic'&&
      !(value.view&&value.view.state==='candidate'&&armUntil>Date.now()&&(!divMode||same(snapshot,capture()))))return;
    if(value.type==='identity-pilot-diagnostic'){
      if(value.phase==='commit-trace'){
        if(Object.keys(value).length!==6||typeof value.reason!=='string'||!tracePattern.test(value.reason)||value.passed!==false)return;
        navTrace.push(value.reason);if(navTrace.length>16)navTrace.shift();diagnostic('nav-trace',navTrace.join(';'));return;
      }
      if(Object.keys(value).length!==6||!['created','live','commit','lookup','query','navigation','history','coordinator-created','coordinator-replaced','coordinator-navigated'].includes(value.phase)||!['source-frame','new-target','no-proof','age','time','url','url-empty','url-blocked-blank','url-newtab','url-profile-same-key','url-profile-different-key','duplicate-target','stale','accepted','frame-unavailable','frame-mismatch','initial-url','exact-url','coordinator','blank','started','returned','rejected','completed','listener-attached','listener-missing','permission-granted','permission-missing','permission-unavailable','coordinator-clock-invalid','coordinator-scope-invalid','coordinator-reentrant-callback','coordinator-expired','coordinator-scope-changed','coordinator-approval-required','coordinator-concurrent-attempt','coordinator-event-invalid','coordinator-event-unrelated','coordinator-multiple-targets','coordinator-route-or-event-invalid','coordinator-profile-redirect-conflict','coordinator-replacement-invalid','coordinator-replacement-unrelated','coordinator-cancelled','coordinator-invalidated'].includes(value.reason)||typeof value.passed!=='boolean')return;
      diagnostic('nav-phase',value.phase);diagnostic('nav-reason',value.reason);diagnostic('nav-passed',String(value.passed));return;
    }
    if(snapshot&&snapshot.panel&&value.view&&['candidate','selection_candidate','resumed_candidate','recalled_candidate'].includes(value.view.state))return;
    // A matching context cannot turn an absent or disabled native binding into
    // an identity candidate, even when a trusted message still has a valid TTL.
    if(value.view&&value.view.state==='candidate'&&(!head||!Number.isFinite(value.expires_at)||value.expires_at<=Date.now())){
      if(divMode&&!snapshot)return;
      reset();return;
    }
    if(value.view&&value.view.state==='selection_candidate'){
      const intent=selectionIntent,v=value.view,now=Date.now(),live=capture();
      if(!intent||!intent.sent||!validIntent(intent)||intent.context!==context||intent.generation!==generation||intent.headToken!==headToken||value.selection_token!==intent.token||
        !same(snapshot,live)||live.row!==intent.row||live.sec!==intent.sec||v.current_identity!=='unverified'||v.owner!=='unverified'||v.private_context!=='unverified'||v.formal_binding!==false||
        !['sender_candidate','zero','conflict'].includes(v.historical_identity)||!Number.isFinite(value.expires_at)||value.expires_at<=now||value.expires_at>intent.displayUntil||intent.displayed)return;
      intent.displayed=true;
    }
    if(value.view&&value.view.state==='resumed_candidate'){
      const request=resumeRequest,v=value.view,now=Date.now();
      if(!request||request.displayed||selectionIntent||request.context!==context||request.generation!==generation||request.headToken!==headToken||value.resume_token!==request.token||
        now<request.started||now>=request.displayUntil||!same(request.snapshot,capture())||!head||v.current_identity!=='unverified'||v.owner!=='unverified'||v.private_context!=='unverified'||v.formal_binding!==false||
        !['sender_candidate','zero','conflict'].includes(v.historical_identity)||!Number.isFinite(value.expires_at)||value.expires_at<=now||value.expires_at>request.displayUntil)return;
      request.displayed=true;
    }
    if(value.view&&value.view.state==='recalled_candidate'){
      const v=value.view,now=Date.now();
      if(!divMode||document.visibilityState!=='visible'||!head||!recallSent||!same(recallSnapshot,capture())||
        v.current_identity!=='unverified'||v.owner!=='unverified'||v.private_context!=='unverified'||v.formal_binding!==false||
        !['sender_candidate','zero','conflict'].includes(v.historical_identity)||!Number.isFinite(value.expires_at)||value.expires_at<=now||value.expires_at>now+300000)return;
      resumeCycle=null;
    }
    // Keep summaries inside the already accepted identity/context path. Never
    // expose fields added by a producer or retain counts after a context reset.
    if(value.view&&Object.hasOwn(value.view,'history_summary')){
      const summary=typeof DSHHistorySummary!=='undefined'?DSHHistorySummary.fromResponse(value.view,value.view.state!=='pending'&&value.view.historical_identity==='sender_candidate'&&value.view.owner==='unverified'&&value.view.private_context==='unverified'&&value.view.formal_binding===false):null;
      const clean={...value.view};delete clean.history_summary;
      value={...value,view:{...clean,...(summary?{history_summary:summary}:{})}};
    }
    sidebar.showIdentityPilot(value.view);
    // Idle pending views contain no identity proof. Their display deadline
    // must not revoke a fresh native gesture; only unavailable context needs
    // a timed registration retry. Candidate expiry stays fixed by the host.
    const retryPending=value.view&&value.view.state==='pending'&&['service_unavailable','login_required'].includes(value.view.connection);
    expires=value.view&&(['candidate','recalled_candidate','selection_candidate','resumed_candidate'].includes(value.view.state)||retryPending)&&Number.isFinite(value.expires_at)?value.expires_at:0;
  };
  chrome.runtime.onMessage.addListener(message);
  function visibility(event){
    if(disposed)return;
    if(event&&event.isTrusted===false)return;
    if(document.visibilityState==='visible'){
      if(!wasHidden)return;
      wasHidden=false;
      // Returning from an approved native profile must not cancel its pending
      // lookup or renew its deadline. Identity changes still revoke the scope.
      if(armUntil>Date.now()&&(!divMode?head===currentHead():same(snapshot,capture())))return;
      allowResume();
      reset('visibility-resumed');return;
    }
    wasHidden=true;cancelResume();
    savedRevoked=true;send('identity-saved-profile-suspend',metadata());
    if(selectionIntent)reset('selection-hidden');
    // Opening native profile naturally backgrounds this chat. Clear only the
    // display; retain its approved navigation scope so the host may finish it.
    recallSnapshot=null;recallEligible=false;
    if(!(armUntil>Date.now()&&(!divMode?head===currentHead():same(snapshot,capture()))))expires=0;
    sidebar.showIdentityPilot({state:'pending',historical_identity:'not_checked',header_bound:false});
  }
  document.addEventListener('visibilitychange',visibility);
  function dispose(notifyHost=true){
    if(disposed)return;
    const value=metadata();savedRevoked=true;disposed=true;
    // Mark disposed before notifying the host: a synchronous fixture or a
    // queued clear receipt must never re-enter reset/dispose on the old route.
    if(notifyHost!==false)try{Promise.resolve(chrome.runtime.sendMessage({type:'identity-saved-profile-suspend',value})).catch(()=>{});}catch{}
    observer.disconnect();document.removeEventListener('pointerdown',nativeGesture,true);document.removeEventListener('keydown',nativeGesture,true);document.removeEventListener('visibilitychange',visibility);try{chrome.runtime.onMessage.removeListener(message);}catch{}sidebar.dispose();clearInterval(timer);
  }
  const timer=setInterval(()=>{
    if(disposed)return;
    try{if(!chrome.runtime.id){dispose(false);return;}}catch{dispose(false);return;}
    if(!DSHProtocol.chatURL(location.href)){dispose();return;}
    const now=Date.now();
    if(expires&&now>=expires){
      if(savedShown){
        // Missing a successful recheck clears the display. Heartbeats cannot
        // revive this consumed context or repeat its profile lookup.
        savedRevoked=true;expires=0;sidebar.showIdentityPilot({state:'pending'});
        send('identity-saved-profile-suspend',metadata());return;
      }
      reset('candidate-expired');return;
    }
    if(divMode&&!stable(snapshot,capture())){reset('native-identity-changed');return;}
    maybeSelection();maybeResume();maybeRecall();
    if(savedShown){
      // Recheck only an existing qualified lease, including after worker loss.
      // Never re-register a consumed card, even after it has been cleared.
      if(savedQualified&&!savedRevoked&&document.visibilityState==='visible'&&now>=armUntil&&now>=heartbeatAt){
        heartbeatAt=now+20000;send('identity-saved-profile-heartbeat',metadata());
      }
      return;
    }
    // Re-register only idle context after worker eviction. Never reconstruct a
    // navigation attempt, renew candidate expiry, or interrupt an armed proof.
    if(!expires&&now>=armUntil&&now>=heartbeatAt){heartbeatAt=now+20000;send('identity-pilot-context',metadata());}
  },1000);
  globalThis.__DSH_CHAT_LIFECYCLE_V2__={alive:()=>!disposed};
  window.addEventListener('pagehide',dispose,{once:true});if(!wasHidden)allowResume();reset();
})();
