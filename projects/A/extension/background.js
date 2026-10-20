'use strict';
importScripts('lib/protocol.js','lib/profile_editor.js','capture/outbox.js','lib/bridge.js','lib/chat_bootstrap.js','identity/profile_navigation.js','identity/history_summary.js','identity/pilot.js','identity/selection_budget.js','identity/context_guard.js','identity/saved_profile_context.js','identity/runtime_context.js','identity/native_host.js');

// Both storage areas are inaccessible to content scripts; token only uses session.
const storageReady = Promise.all([
  chrome.storage.session.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'}),
  chrome.storage.local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'})
]);
const sessionStorage = {
  async read() { await storageReady; return (await chrome.storage.session.get('dshSession')).dshSession; },
  async write(value) { await storageReady; if (value) await chrome.storage.session.set({dshSession:value}); else await chrome.storage.session.remove('dshSession'); }
};
const deviceStorage = {
  async read() {await storageReady; return (await chrome.storage.local.get('dshIdentityPilotDevice')).dshIdentityPilotDevice;},
  async write(value) {await storageReady; if(value)await chrome.storage.local.set({dshIdentityPilotDevice:value});else await chrome.storage.local.remove('dshIdentityPilotDevice');}
};
const workbenchDeviceStorage = {
  async read() {await storageReady;return (await chrome.storage.local.get('dshWorkbenchDevice')).dshWorkbenchDevice;},
  async write(value) {await storageReady;if(value)await chrome.storage.local.set({dshWorkbenchDevice:value});else await chrome.storage.local.remove('dshWorkbenchDevice');}
};
const workbenchResumeStorage={
  async read(){await storageReady;return (await chrome.storage.local.get('dshWorkbenchAutoConnect')).dshWorkbenchAutoConnect;},
  async write(value){await storageReady;await chrome.storage.local.set({dshWorkbenchAutoConnect:value});}
};
const workbenchRetryStorage={
  async read(){await storageReady;return (await chrome.storage.session.get('dshWorkbenchRetryAt')).dshWorkbenchRetryAt;},
  async write(value){await storageReady;await chrome.storage.session.set({dshWorkbenchRetryAt:value});}
};
const outbox = new DSHOutbox.Outbox({
  async read() { await storageReady; return (await chrome.storage.local.get('dshOutboxV2')).dshOutboxV2; },
  async write(value) { await storageReady; await chrome.storage.local.set({dshOutboxV2:value}); }
});
const resumeIntentKey='dshIdentityPilotResumeAllowed';
let resumeIntentVersion=0,resumeIntentBlocked=false,resumeIntentPersistence=Promise.resolve();
function setPilotResumeIntent(allowed){
  const stamp=++resumeIntentVersion;
  // A user stop takes effect before any storage or network await. Enabling
  // remains blocked until its exact boolean is durably written for this worker.
  resumeIntentBlocked=true;
  const task=resumeIntentPersistence.then(async()=>{
    await storageReady;
    await chrome.storage.session.set({[resumeIntentKey]:allowed===true});
    if(stamp!==resumeIntentVersion)throw Error('stale_session');
    resumeIntentBlocked=allowed!==true;
  });
  resumeIntentPersistence=task.catch(()=>{});
  return task.catch(error=>{if(stamp===resumeIntentVersion)resumeIntentBlocked=true;throw Error(error.message==='stale_session'?'stale_session':'storage_unavailable');});
}
async function allowPilotResume(){
  const stamp=resumeIntentVersion;
  if(resumeIntentBlocked)return false;
  try{
    await storageReady;await resumeIntentPersistence;
    const stored=await chrome.storage.session.get(resumeIntentKey);
    if(stamp!==resumeIntentVersion||resumeIntentBlocked)return false;
    return stored[resumeIntentKey]===true;
  }catch{if(stamp===resumeIntentVersion)resumeIntentBlocked=true;return false;}
}
async function request(path,{query,body,token} = {}) {
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(),5000);
  const headers = {'Accept':'application/json'};
  if (token) headers.Authorization = 'Bearer ' + token;
  if (body !== undefined) { headers['Content-Type'] = 'application/json'; if (path !== '/api/v1/auth/startup') headers['X-DSH-Schema'] = '2'; }
  try {
    const response = await fetch(DSHProtocol.url(path,query || {}), {method:body === undefined ? 'GET' : 'POST',headers,body:body === undefined ? undefined : JSON.stringify(body),signal:controller.signal,cache:'no-store',redirect:'error',credentials:'omit',referrerPolicy:'no-referrer'});
    const raw = await response.text(); if (raw.length > 1024 * 1024) throw new Error('bad_response');
    const envelope = JSON.parse(raw);
    if (!response.ok) {
      if (path === '/api/v1/observations' && [400,409,413,422].includes(response.status) && envelope.schema_version === 2) {
        const code = envelope.errors && envelope.errors[0] && envelope.errors[0].code;
        return {status:'rejected',committed:false,code:typeof code === 'string' && /^[a-zA-Z0-9_]{1,80}$/.test(code) ? code : 'invalid_observation'};
      }
      if(['/api/v1/profile/field','/api/v1/groups/membership'].includes(path)&&response.status===409)throw Error('conflict');
      throw new Error(response.status === 401 ? 'unauthorized' : response.status === 403 ? 'forbidden' : 'service_error');
    }
    if (envelope.schema_version !== 2 || !envelope.data || typeof envelope.data !== 'object') throw new Error('bad_response');
    return envelope.data;
  } finally { clearTimeout(timer); }
}
const bridge = DSHBridge.createBridge({id:chrome.runtime.id,request,sessionStorage,deviceStorage,workbenchDeviceStorage,workbenchResumeStorage,workbenchRetryStorage,outbox,allowPilotResume});
async function scopedPilotStatus(){
  const config=await bridge.pilotStatus(),epoch=bridge.pilotEpoch();
  identityHost.syncMemoryEpoch(epoch,config);
  return {...config,connection_epoch:epoch};
}
async function scopedNativeStatus(){
  if(await bridge.connectionKind()==='workbench')return bridge.workbenchResume();
  return scopedPilotStatus();
}
const selectionBudget=DSHSelectionBudget.createScopedBudget({storageForKey:key=>({
  async read(){await storageReady;return (await chrome.storage.local.get(key))[key];},
  async write(value){await storageReady;await chrome.storage.local.set({[key]:value});}
})});
// No policy means unknown. The protected host grants a scoped reader policy;
// page metadata and saved cards never supply that permission.
const runtimeContext=DSHNativeRuntime.create({
  readMetadata:(scope,current)=>identityHost.readCurrentMetadata(scope,current),
  resolve:(metadata,current)=>bridge.nativeRuntimeResolve(metadata,current),
  registrationStatus:(query,current)=>bridge.nativeRegistrationStatus(query,current),epoch:()=>bridge.pilotEpoch()});
const savedProfileContext=DSHSavedProfileContext.create({read:runtimeContext.read,authorize:runtimeContext.authorize,
  registrationStatus:runtimeContext.registrationStatus,
  renewable:runtimeContext.renewable,expiresAt:runtimeContext.expiresAt,
  lookup:(query,current)=>bridge.nativeProfile(query,current),editor:(binding,command,current)=>bridge.nativeEditor(binding,command,current)});
savedProfileContext.onInvalidate(lease=>{if(!lease)runtimeContext.invalidate();});
const identityHost=new DSHNativeHost.NativeHost({id:chrome.runtime.id,status:scopedNativeStatus,selectionBudget,savedProfileContext,lookup:q=>bridge.pilotLookup(q),
  readPageMetadata:(tab,documentId,value)=>chrome.tabs.sendMessage(tab,value,{documentId}),
  readConnectionEpoch:()=>bridge.pilotEpoch(), // Existing getter tracks the shared session epoch, including workbench.
  diagnostic:(e,v)=>chrome.tabs.sendMessage(e.tab,{type:'identity-pilot-diagnostic',context_id:e.context.context_id,generation:e.context.generation,...v},{documentId:e.document}).catch(()=>{}),
  getFrame:q=>chrome.webNavigation.getFrame(q),emit:(tab,documentId,value)=>{
    const sent=chrome.tabs.sendMessage(tab,value,{documentId});
    return value.type==='native-saved-profile-renew'?sent:sent.catch(()=>{});
  }});
const publicErrors = new Set(['invalid_request','unauthorized','forbidden','already_authenticated','stale_session','bad_response','storage_unavailable','outbox_full','outbox_corrupt','observation_id_conflict','service_error']);
let startupConnectAllowed=true,contextActionVersion=0;
const pendingContextRegistrations=new Map();
const startupAttemptKey='dshIdentityPilotStartupAttempted';
function suppressStartupConnect(){
  startupConnectAllowed=false;
  // Keep a later alarm-woken worker from restoring an explicitly ended session.
  storageReady.then(()=>chrome.storage.session.set({[startupAttemptKey]:true})).catch(()=>{});
}
chrome.runtime.onMessage.addListener((value,sender,respond) => {
  if(value&&value.type==='identity-profile-edit'){
    try{DSHProfileEditorProtocol.exact(value,['type','value']);}catch{respond({ok:false,error:'invalid_request'});return;}
    identityHost.editor(value.value,sender).then(data=>respond({ok:true,data})).catch(error=>respond({ok:false,error:['conflict','stale_session','forbidden','unauthorized','invalid_request'].includes(error.message)?error.message:'unavailable'}));return true;
  }
  if(value&&['workbench-pairing-info','workbench-pair','workbench-connect','workbench-forget'].includes(value.type)){
    try{
      if(DSHProtocol.senderKind(sender,chrome.runtime.id)!=='popup')throw Error('invalid_request');
      const pair=value.type==='workbench-pair',keys=pair?['type','code']:['type'];
      const fields=Object.getOwnPropertyDescriptors(value);
      if(Reflect.ownKeys(fields).length!==keys.length||keys.some(key=>!fields[key]||!Object.hasOwn(fields[key],'value'))||
         pair&&(typeof value.code!=='string'||!(/^[A-Za-z0-9_-]{43}$/).test(value.code)))throw Error('invalid_request');
    }catch{respond({ok:false,error:'invalid_request'});return;}
    if(value.type==='workbench-pairing-info'){
      bridge.workbenchStatus().then(data=>respond({ok:true,data})).catch(error=>respond({ok:false,error:publicErrors.has(error.message)?error.message:'unavailable'}));return true;
    }
    contextActionVersion++;suppressStartupConnect();identityHost.invalidateAll();
    const intent=Promise.all([setPilotResumeIntent(false),bridge.setWorkbenchResume(value.type!=='workbench-forget')]);
    const task=value.type==='workbench-pair'?()=>bridge.workbenchPair(value.code):value.type==='workbench-connect'?()=>bridge.workbenchConnect():()=>bridge.workbenchForget();
    const version=resumeIntentVersion;
    const action=value.type==='workbench-forget'?Promise.all([task(),intent]).then(values=>values[0]):intent.then(()=>{
      if(version!==resumeIntentVersion)throw Error('stale_session');return task();
    });
    action.then(data=>respond({ok:true,data})).catch(error=>respond({ok:false,error:publicErrors.has(error.message)?error.message:'unavailable'}));return true;
  }
  if(value&&['identity-pilot-pair','identity-pilot-connect','identity-pilot-forget'].includes(value.type)){
    try{
      if(DSHProtocol.senderKind(sender,chrome.runtime.id)!=='popup')throw Error('invalid_request');
      const pair=value.type==='identity-pilot-pair';
      if(!Object.hasOwn(value,'type')||Object.keys(value).length!==(pair?2:1)||Object.keys(value).some(k=>!(pair?['type','code']:['type']).includes(k))||(pair&&(!Object.hasOwn(value,'code')||typeof value.code!=='string'||!(/^[A-Za-z0-9_-]{43}$/).test(value.code))))throw Error('invalid_request');
    }catch{respond({ok:false,error:'invalid_request'});return;}
    contextActionVersion++;suppressStartupConnect();
    const task=value.type==='identity-pilot-pair'?()=>bridge.pilotPair(value.code):value.type==='identity-pilot-connect'?()=>bridge.pilotConnect():()=>bridge.pilotForget();
    identityHost.invalidateAll();
    const intent=Promise.all([setPilotResumeIntent(value.type!=='identity-pilot-forget'),bridge.setWorkbenchResume(false)]);
    const intentVersion=resumeIntentVersion;
    // Forget invalidates the bridge epoch immediately, even if persistence is
    // slow. A connect may start only after its intent survives that same await.
    const action=value.type==='identity-pilot-forget'?Promise.all([task(),intent]).then(values=>values[0]):intent.then(()=>{
      if(intentVersion!==resumeIntentVersion||resumeIntentBlocked)throw Error('stale_session');
      return task();
    });
    action.then(data=>respond({ok:true,data})).catch(error=>respond({ok:false,error:publicErrors.has(error.message)?error.message:'unavailable'}));return true;
  }
  if(value&&value.type==='identity-pilot-connection'){
    try{if(Object.keys(value).length!==1||DSHProtocol.senderKind(sender,chrome.runtime.id)!=='popup')throw Error('invalid_request');}
    catch{respond({ok:false,error:'invalid_request'});return;}
    bridge.pilotConnection().then(data=>respond({ok:true,data})).catch(()=>respond({ok:false,error:'unavailable'}));return true;
  }
  if(value&&value.type==='identity-pilot-info'){
    try{if(Object.keys(value).length!==1||DSHProtocol.senderKind(sender,chrome.runtime.id)!=='popup')throw Error('invalid_request');}
    catch{respond({ok:false,error:'invalid_request'});return;}
    scopedPilotStatus().then(({connection_epoch,...data})=>respond({ok:true,data})).catch(()=>respond({ok:false,error:'unavailable'}));return true;
  }
  if(value&&['identity-pilot-context','identity-pilot-arm','identity-pilot-recall','identity-pilot-selection','identity-pilot-resume','identity-saved-profile-suspend','identity-saved-profile-heartbeat'].includes(value.type)){
    if(Object.keys(value).length!==2||!Object.hasOwn(value,'value')){respond({ok:false,error:'invalid_request'});return;}
    // Registration waits for this worker's one existing startup attempt.
    // This adds no connect/lookup retry and leaves explicit stop cancellation
    // in the original startup and persisted-intent guards.
    let action;
    if(value.type==='identity-pilot-context'){
      try{
        identityHost.sender(sender);
        const c=value.value,keys=['context_id','generation','head_token','header_bound'];
        if(!c||Object.getPrototypeOf(c)!==Object.prototype||Object.keys(c).length!==4||Object.keys(c).some(k=>!keys.includes(k))||
          typeof c.context_id!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(c.context_id)||!Number.isSafeInteger(c.generation)||c.generation<0||
          typeof c.head_token!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(c.head_token)||typeof c.header_bound!=='boolean')throw Error('invalid_request');
        if(pendingContextRegistrations.size>=32&&!pendingContextRegistrations.has(sender.tab.id))throw Error('unavailable');
      }catch(error){respond({ok:false,error:error.message==='invalid_request'?'invalid_request':'unavailable'});return;}
      const tab=sender.tab.id,marker={},version=contextActionVersion;
      pendingContextRegistrations.set(tab,marker);
      const current=()=>contextActionVersion===version&&pendingContextRegistrations.get(tab)===marker;
      action=initialPilotStartup.then(async()=>{
        if(!current())throw Error('stale_session');
        const data=await identityHost.control(value.type,value.value,sender);
        if(!current())throw Error('stale_session');
        return data;
      }).finally(()=>{if(pendingContextRegistrations.get(tab)===marker)pendingContextRegistrations.delete(tab);});
    }else action=identityHost.control(value.type,value.value,sender);
    action.then(data=>{
      respond({ok:true,data});
      if(value.type==='identity-pilot-arm'){
        const e=identityHost.entries.get(sender.tab.id);
        if(e&&e.document===sender.documentId&&e.context.context_id===value.value.context_id&&e.context.generation===value.value.generation){
          identityHost.diagnose(e,'navigation',navigationAttached?'listener-attached':'listener-missing',navigationAttached);
          chrome.permissions.contains({permissions:['webNavigation']}).then(granted=>identityHost.diagnose(e,'navigation',granted?'permission-granted':'permission-missing',granted===true)).catch(()=>identityHost.diagnose(e,'navigation','permission-unavailable'));
        }
      }
    }).catch(()=>respond({ok:false,error:'unavailable'}));return true;
  }
  if(value&&value.type==='logout'){
    let trusted=false;
    try{trusted=DSHProtocol.senderKind(sender,chrome.runtime.id)==='popup';if(trusted)DSHProtocol.message(value,'popup');}
    catch{trusted=false;/* An untrusted message cannot cancel startup. */}
    if(trusted){
      contextActionVersion++;suppressStartupConnect();identityHost.invalidateAll();
      const intent=Promise.all([setPilotResumeIntent(false),bridge.setWorkbenchResume(false)]);
      Promise.all([bridge.handle(value,sender),intent]).then(values=>respond({ok:true,data:values[0]}))
        .catch(error=>respond({ok:false,error:publicErrors.has(error.message)?error.message:'unavailable'}));return true;
    }
  }
  if(value&&value.type==='login'){try{if(DSHProtocol.senderKind(sender,chrome.runtime.id)==='popup'){DSHProtocol.message(value,'popup');contextActionVersion++;}}catch{}}
  const readyForStatus=value&&value.type==='status'?initialPilotStartup:Promise.resolve();
  readyForStatus.then(()=>bridge.handle(value,sender)).then(data => {if(['login','logout'].includes(value.type))identityHost.invalidateAll();respond({ok:true,data});}).catch(error => respond({ok:false,error:publicErrors.has(error.message) ? error.message : 'unavailable'}));
  return true;
});
// No automatic outbox delivery in this identity-only development increment.
chrome.alarms.clear('dsh-retry');
chrome.alarms.create('dsh-identity-expiry',{periodInMinutes:1});
chrome.alarms.onAlarm.addListener(alarm=>{
  if(alarm.name!=='dsh-identity-expiry')return;
  identityHost.sweep();
  bridge.connectionKind().then(kind=>{if(kind==='workbench')return bridge.workbenchResume();}).catch(()=>{});
  chatBootstrap.scan().catch(()=>{});
  // Recheck explicit resume intent on this existing expiry alarm. Status uses
  // the bridge's shared, epoch-checked recovery; never restore identity proofs.
  allowPilotResume().then(allowed=>{
    if(allowed!==true)return;
    const snapshot=Array.from(identityHost.entries.values(),entry=>({entry,proof:entry.proof}));
    return scopedPilotStatus().catch(()=>identityHost.revokeProofs(snapshot));
  }).catch(()=>{});
});
let navigationAttached=false;
async function attachNavigation(){
  if(navigationAttached||!await chrome.permissions.contains({permissions:['webNavigation']}))return;
  navigationAttached=true;
  chrome.webNavigation.onCreatedNavigationTarget.addListener(d=>identityHost.created(d).catch(()=>{}));
  chrome.webNavigation.onCommitted.addListener(d=>identityHost.committed(d).catch(()=>{}));
  chrome.webNavigation.onHistoryStateUpdated.addListener(d=>identityHost.history(d));
  chrome.tabs.onRemoved.addListener(id=>identityHost.removed(id));
}
chrome.permissions.onAdded.addListener(()=>attachNavigation().catch(()=>{}));attachNavigation().catch(()=>{});
chrome.permissions.onRemoved.addListener(change=>{if((change.permissions||[]).includes('webNavigation'))identityHost.invalidateAll();});

// One startup attempt per loaded extension session. The existing expiry alarm
// may later demand safe status recovery only with explicit persisted intent.
// The trusted session marker contains only a boolean, never a secret.
// Popup diagnosis remains available if storage, service or host proof fails.
async function connectRememberedDeviceOnce(){
  if(await bridge.connectionKind()==='workbench')return bridge.workbenchResume();
  const device=await deviceStorage.read();
  if(!startupConnectAllowed||!device||Object.getPrototypeOf(device)!==Object.prototype)return;
  const fields=Object.getOwnPropertyDescriptors(device);
  if(Reflect.ownKeys(fields).length!==3||!['format','extension_id','credential'].every(key=>fields[key]&&Object.hasOwn(fields[key],'value'))||
    device.format!=='identity-pilot-device-1'||device.extension_id!==chrome.runtime.id||
    typeof device.credential!=='string'||!(/^[A-Za-z0-9_-]{43}$/).test(device.credential))return;
  const marker=await chrome.storage.session.get(startupAttemptKey);
  if(!startupConnectAllowed||marker[startupAttemptKey]!==undefined)return;
  const intent=await chrome.storage.session.get(resumeIntentKey);
  if(!startupConnectAllowed||(intent[resumeIntentKey]!==undefined&&intent[resumeIntentKey]!==true))return;
  await setPilotResumeIntent(true);
  if(!startupConnectAllowed)return;
  await chrome.storage.session.set({[startupAttemptKey]:true});
  if(!startupConnectAllowed)return;
  await bridge.pilotConnect();
}
const initialPilotStartup=connectRememberedDeviceOnce().catch(()=>{});
// No tab creation, navigation, refresh, or message sending. Optional injection
// only mounts this extension into an already open, exact /chat top document.
const chatBootstrap=DSHChatBootstrap.create(chrome);
chrome.runtime.onInstalled?.addListener(()=>chatBootstrap.scan().catch(()=>{}));
chrome.runtime.onStartup?.addListener(()=>chatBootstrap.scan().catch(()=>{}));
chrome.tabs.onUpdated?.addListener((id,change,tab)=>{
  if(change.status==='complete')chatBootstrap.mount({...tab,id}).catch(()=>{});
});
chrome.permissions.onAdded.addListener(()=>chatBootstrap.scan().catch(()=>{}));
chatBootstrap.scan().catch(()=>{});
