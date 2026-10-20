(function (root, factory) {
  const node=typeof module === 'object' && module.exports;
  const value = factory(node ? require('./protocol.js') : root.DSHProtocol,node ? require('./profile_editor.js') : root.DSHProfileEditorProtocol);
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHBridge = value;
})(globalThis, function (P,E) {
  'use strict';
  function createBridge({id,request,sessionStorage,outbox,deviceStorage,workbenchDeviceStorage,cryptoProvider=globalThis.crypto,allowPilotResume,workbenchResumeStorage,workbenchRetryStorage,now=Date.now}) {
    let session = null; let epoch = 0; let selectedKind='none';
    let persistence = Promise.resolve();
    let pilotWork = null;
    let statusResume = null;
    let normalResume=null,normalIntentVersion=0,normalBlocked=false,normalIntentWrites=Promise.resolve();
    const ready = sessionStorage.read().then(async value => {
      if(exactData(value,['format'])&&value.format==='workbench-session-selection-1'){
        session=null;selectedKind='workbench';
      }else{session=value||null;selectedKind=sessionKindOf(session);}
      if(value==null&&workbenchDeviceStorage){
        // Browser restart clears session storage, but not device memory.
        // Recover only the unauthenticated mode; every connect still proves
        // the host and validates the device. Corrupt/unreadable memory must not
        // select legacy pilot, or prevent explicit forget from clearing it.
        try{if(await normalMemory()!=null)selectedKind='workbench';}
        catch{selectedKind='workbench';}
      }
    });
    ready.catch(() => {}); // Fail closed when storage is unavailable; handlers still reject.
    function persistCurrent() {
      // Preserve only a non-secret UI selection after normal logout/expiry.
      // It grants no authentication and prevents automatic fallback to pilot.
      const task = persistence.then(() => sessionStorage.write(session||
        (selectedKind==='workbench'?{format:'workbench-session-selection-1'}:null)));
      persistence = task.catch(() => {}); return task;
    }
    function persistDevice(value, work) {
      const task=persistence.then(()=>{if(work)assertCurrent(work);return deviceStorage.write(value);});
      persistence=task.catch(()=>{});return task;
    }
    function publicSession() {
      return session ? {authenticated:true,session_kind:selectedKind,principal:{operator_key:session.principal.operator_key,device_key:session.principal.device_key,role:session.principal.role,account_keys:[...session.principal.account_keys]}} : {authenticated:false,session_kind:selectedKind};
    }
    async function publicPairingMemory() {
      if(!deviceStorage)return {};
      try {
        const value=await deviceStorage.read();
        const remembered=validDevice(value);
        return {pairing_remembered:remembered,pairing_memory_available:remembered||value===null||value===undefined};
      } catch {
        // A local read error does not mean first use or permission to re-pair.
        return {pairing_remembered:false,pairing_memory_available:false};
      }
    }
    async function authenticated(path, query, body) {
      if (!session) throw new Error('unauthorized');
      const stamp = epoch; const current = session;
      let result;
      try { result = await request(path,{query,body,token:current.token}); }
      catch (error) {
        if (error.message === 'unauthorized' && stamp === epoch && current === session) { session = null; epoch++; await persistCurrent(); }
        throw error;
      }
      if (stamp !== epoch || current !== session) throw new Error('stale_session');
      return result;
    }
    async function flush(force = false) {
      await ready; if (!session) return {attempted:0};
      const stamp = epoch; const current = session;
      return outbox.flush(value => authenticated('/api/v1/observations',null,value), current.principal, {force,stillAuthorized:() => stamp === epoch && current === session});
    }
    async function handle(message,sender) {
      const kind = P.senderKind(sender,id); const value = P.message(message,kind); await ready;
      switch (value.type) {
        case 'status': return {...publicSession(),...await publicPairingMemory(),...(workbenchDeviceStorage?{workbench_pairing_supported:true}:{}),...(workbenchResumeStorage?{workbench_auto_connect:await normalResumeMode()}:{}),queue:await outbox.stats(session && session.principal),source:{state:'unavailable',verified:false,coverage:'unknown'}};
        case 'login': {
          if (session) throw new Error('already_authenticated');
          const stamp = ++epoch;
          const result = await request('/api/v1/auth/startup',{body:{operator_key:value.operator_key,device_key:value.device_key,credential:value.credential,client_kind:'extension',extension_id:id}});
          if (!result || typeof result.token !== 'string' || !result.principal || !Array.isArray(result.principal.account_keys) || result.principal.operator_key !== value.operator_key || result.principal.device_key !== value.device_key) throw new Error('bad_response');
          if (stamp !== epoch) { await request('/api/v1/auth/logout',{token:result.token,body:{}}).catch(() => {}); throw new Error('stale_session'); }
          session = {token:result.token,principal:result.principal};selectedKind=sessionKindOf(session);
          try { await persistCurrent(); } catch { if (stamp === epoch) { session = null; epoch++; } await request('/api/v1/auth/logout',{token:result.token,body:{}}).catch(() => {}); throw new Error('storage_unavailable'); }
          if (stamp !== epoch) throw new Error('stale_session');
          return publicSession();
        }
        case 'logout': {
          const previous = session; session = null; epoch++; await persistCurrent();
          if (previous) await request('/api/v1/auth/logout',{token:previous.token,body:{}});
          return {authenticated:false};
        }
        case 'retry': return flush(true);
        case 'capabilities': return authenticated('/api/v1/capabilities');
        case 'history':
        case 'sidebar': {
          if (!session || !session.principal.account_keys.includes(value.query.account_key)) throw new Error('forbidden');
          return authenticated('/api/v1/' + value.type,value.query);
        }
        case 'observation': {
          if (!session || !session.principal.account_keys.includes(value.observation.account_key)) throw new Error('forbidden');
          // Durability precedes requests. Pending/rejected remain separate and visible.
          return outbox.enqueue(value.observation,session.principal);
        }
        default: throw new Error('invalid_request');
      }
    }
    // Background-only dependencies. Content messages cannot call arbitrary
    // queries or bypass the native navigation/context guard.
    async function pilotConnection() {
      // Health alone is not authentication or evidence about a fan.
      try {
        const result=await request('/api/v1/health',{body:{extension_id:id,build:'F-identity-pilot-connection-1'}});
        return {state:result&&result.service==='identity-pilot-readonly'&&result.business_enabled===false?'connected':'different_service'};
      } catch { return {state:'unavailable'}; }
    }
    async function pilotStatus() {
      await ready;
      if(selectedKind==='workbench'||session&&!validSession(session))throw new Error('forbidden');
      if(statusResume)return statusResume;
      try{return await authenticated('/api/v1/identity-pilot/status');}
      catch(error){if(error.message!=='unauthorized'||typeof allowPilotResume!=='function')throw error;}
      if(statusResume)return statusResume;
      const stamp=epoch;
      const resume=Promise.resolve().then(async()=>{
        let allowed;
        try{allowed=await allowPilotResume();}catch{throw new Error('storage_unavailable');}
        if(epoch!==stamp)throw new Error('stale_session');
        if(allowed!==true)throw new Error('unauthorized');
        requireDeviceStorage();
        const remembered=await deviceStorage.read();
        if(epoch!==stamp)throw new Error('stale_session');
        if(!validDevice(remembered))throw new Error('unauthorized');
        // Only a read-only status demand may restore a session. The existing
        // connect path proves the host, coalesces calls and rejects late login.
        const result=await pilotConnect();
        if(!result||result.state!=='connected')throw new Error('unauthorized');
        const connectedStamp=epoch;
        try{allowed=await allowPilotResume();}catch{allowed=false;}
        if(epoch!==connectedStamp)throw new Error('stale_session');
        if(allowed!==true){
          const previous=session;session=null;epoch++;
          await persistCurrent();await disposeToken(previous);
          throw new Error('stale_session');
        }
        // One retry, not a login loop; lookup and business methods never use it.
        return authenticated('/api/v1/identity-pilot/status');
      });
      const shared=resume.finally(()=>{if(statusResume===shared)statusResume=null;});
      statusResume=shared;return shared;
    }
    async function pilotLookup(query) {
      await ready;
      if(selectedKind==='workbench'||session&&!validSession(session))throw new Error('forbidden');
      if (!session || !session.principal.account_keys.includes(query.account_key)) throw new Error('forbidden');
      return authenticated('/api/v1/identity-pilot/lookup',null,query);
    }
    async function nativeProfile(value,current) {
      const query=P.nativeProfileRequest(value);
      if(typeof current!=='function')throw new Error('invalid_request');
      await ready;
      const original=session,stamp=epoch;
      // The pilot's snapshot-owner-unverified authority is not an app account.
      if(!original||!Array.isArray(original.principal.account_keys)||
         !original.principal.account_keys.some(key=>key!=='snapshot-owner-unverified')||
         !Array.isArray(original.principal.scopes)||!original.principal.scopes.includes('sidebar:read'))throw new Error('forbidden');
      async function live(){
        if(session!==original||epoch!==stamp)throw new Error('stale_session');
        if(await current()!==true||session!==original||epoch!==stamp)throw new Error('stale_session');
      }
      await live();
      const canonical=Object.fromEntries(Object.keys(query).sort().map(key=>[key,query[key]]));
      const hash=await cryptoProvider.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(canonical)));
      const expected=Array.from(new Uint8Array(hash),byte=>byte.toString(16).padStart(2,'0')).join('');
      await live();
      const result=await authenticated('/api/v1/native/profile',null,query);
      await live();
      if(!result||result.format!=='native-profile-lookup-1'||result.request_digest!==expected||
         result.native_page_verification!=='background_required'||result.historical_device_provenance!=='not_assessed'||
         result.message_history!=='not_loaded'||result.AI_reference!=='not_requested'||
         !['unknown','saved_binding_ready'].includes(result.state))throw new Error('bad_response');
      if(result.state==='unknown'){
        if(result.binding!==null||result.card!==null)throw new Error('bad_response');
      }else{
        const binding=result.binding,card=result.card;
        if(!binding||!card||result.authority_scope!=='current_independent_approval'||
           typeof binding.account_key!=='string'||!original.principal.account_keys.includes(binding.account_key)||
           typeof binding.person_key!=='string'||typeof binding.conversation_key!=='string'||
           !binding.person_key||!binding.conversation_key||card.format!=='native-profile-card-1'||
           card.account_key!==binding.account_key||card.person_key!==binding.person_key||
           card.object_kind!=='person'||card.verified!==true||card.message_history!=='not_loaded'||
           card.AI_reference!=='not_requested')throw new Error('bad_response');
      }
      // This is a saved-binding result, never a verified current Chrome view.
      return result;
    }
    // Only the background lease supplies a target. No generic content route.
    async function nativeEditor(binding,operation,current){
      const cmd=E.command(operation);await ready;
      const original=session,stamp=epoch;
      if(selectedKind!=='workbench'||!original||typeof current!=='function')throw Error('forbidden');
      const authority=normalBinding(original.principal);
      if(!authority.account_keys.includes(binding.account_key)||!authority.scopes.includes('sidebar:read'))throw Error('forbidden');
      const target={account_key:binding.account_key,person_key:binding.person_key};
      async function live(){if(session!==original||epoch!==stamp||await current()!==true||session!==original||epoch!==stamp)throw Error('stale_session');}
      await live();
      const raw=await authenticated('/api/v1/profile/editor',target);await live();
      const view=E.snapshot(raw,target);
      if(cmd.action==='read')return view;
      if(!authority.scopes.includes('profile:write')||!E.allowed(view.access,cmd))throw Error('forbidden');
      if(cmd.action==='membership'&&!view.groups.some(g=>g.group_id===cmd.group_id&&!g.deleted&&g.state!=='conflict'))throw Error('conflict');
      const {action,...change}=cmd;await live();
      const result=await authenticated(action==='field'?'/api/v1/profile/field':'/api/v1/groups/membership',null,{...target,...change});
      await live();
      if(!result||result.status!=='saved'||result.field!==undefined&&result.field!==(action==='field'?cmd.field:'group.'+cmd.group_id))throw Error('bad_response');
      const revision=E.revision(result.revision);if(!revision.length)throw Error('bad_response');
      return {status:'saved',revision};
    }
    // Trusted worker call only. This does not activate a reader/authorizer or
    // turn a saved registration snapshot into continuing page authority.
    async function nativeRegistrationStatus(value,current){
      const query=P.nativeRegistrationRequest(value);
      if(typeof current!=='function')throw Error('invalid_request');
      await ready;
      const original=session,stamp=epoch;
      if(selectedKind!=='workbench'||!original)throw Error('forbidden');
      const authority=normalBinding(original.principal);
      if(!authority.account_keys.includes(query.account_key)||!authority.scopes.includes('sidebar:read'))throw Error('forbidden');
      async function live(){
        if(session!==original||epoch!==stamp)throw Error('stale_session');
        if(await current()!==true||session!==original||epoch!==stamp)throw Error('stale_session');
      }
      await live();
      const canonical=Object.fromEntries(Object.keys(query).sort().map(key=>[key,query[key]]));
      const expected=hex(new Uint8Array(await cryptoProvider.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(canonical)))));
      await live();
      const raw=await authenticated('/api/v1/native/registration-status',null,query);
      await live();
      let result;
      try{result=P.nativeRegistrationResponse(raw,query,authority.device_key);}catch{throw Error('bad_response');}
      if(result.request_digest!==expected)throw Error('bad_response');
      await live();
      return result;
    }
    async function nativeRuntimeResolve(metadata,current){
      const fakeScope={context_id:'validation',generation:0,head_token:'validation',header_bound:true};
      const query=P.nativeMetadataResponse({scope:fakeScope,metadata}).metadata;
      if(typeof current!=='function')throw Error('invalid_request');
      await ready;const original=session,stamp=epoch;
      if(selectedKind!=='workbench'||!original)throw Error('forbidden');
      const authority=normalBinding(original.principal);
      if(!authority.scopes.includes('sidebar:read'))throw Error('forbidden');
      const live=async()=>{if(session!==original||epoch!==stamp||await current()!==true||session!==original||epoch!==stamp)throw Error('stale_session');};
      await live();
      const canonical=Object.fromEntries(Object.keys(query).sort().map(k=>[k,query[k]]));
      const expected=hex(new Uint8Array(await cryptoProvider.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(canonical)))));
      await live();const result=await authenticated('/api/v1/native/runtime-resolve',null,query);await live();
      if(!result||Object.keys(result).length!==4||result.format!=='native-runtime-result-1'||result.request_digest!==expected||
        !['unknown','permitted'].includes(result.state))throw Error('bad_response');
      if(result.state==='unknown'){if(result.grant!==null)throw Error('bad_response');}
      else if(!result.grant||result.grant.native_device_key!==authority.device_key||!result.grant.binding||
        !authority.account_keys.includes(result.grant.binding.account_key))throw Error('bad_response');
      return result;
    }
    const secretPattern=/^[A-Za-z0-9_-]{43}$/;
    const tokenPattern=/^[A-Za-z0-9_-]{32,256}$/;
    const permittedScopes=new Set(['history:read','sidebar:read','observations:write','actions:write','capabilities:read','profile:write','model:read','model:request','model:correct']);
    function exactData(value,keys) {
      if(!value||Object.getPrototypeOf(value)!==Object.prototype)return false;
      const fields=Object.getOwnPropertyDescriptors(value);
      return Reflect.ownKeys(fields).length===keys.length&&keys.every(key=>fields[key]&&Object.hasOwn(fields[key],'value'));
    }
    function plainList(value,max=10) {
      if(!Array.isArray(value)||Object.getPrototypeOf(value)!==Array.prototype||value.length>max)return false;
      const fields=Object.getOwnPropertyDescriptors(value);
      return Reflect.ownKeys(fields).length===value.length+1&&Array.from({length:value.length},(_,i)=>fields[i]&&Object.hasOwn(fields[i],'value')).every(Boolean);
    }
    function validPrincipal(value) {
      return exactData(value,['operator_key','device_key','role','account_keys','scopes','session_id'])&&
        value.operator_key==='identity-pilot'&&value.device_key==='F-identity-pilot'&&value.role==='manager'&&
        plainList(value.account_keys,1)&&value.account_keys.length===1&&value.account_keys[0]==='snapshot-owner-unverified'&&
        typeof value.session_id==='string'&&/^[0-9a-f]{32}$/.test(value.session_id)&&plainList(value.scopes)&&
        value.scopes.length<=permittedScopes.size&&new Set(value.scopes).size===value.scopes.length&&
        value.scopes.every(scope=>typeof scope==='string'&&permittedScopes.has(scope))&&
        value.scopes.includes('history:read')&&value.scopes.includes('capabilities:read');
    }
    function validSession(value,paired=false) {
      return exactData(value,paired?['token','principal','device_credential']:['token','principal'])&&
        typeof value.token==='string'&&tokenPattern.test(value.token)&&validPrincipal(value.principal)&&
        (!paired||(typeof value.device_credential==='string'&&secretPattern.test(value.device_credential)));
    }
    function sessionKindOf(value){
      if(!value)return 'none';
      if(validSession(value))return 'identity_pilot';
      if(!exactData(value,['token','principal'])||typeof value.token!=='string'||!tokenPattern.test(value.token))return 'unknown';
      const p=value.principal;
      if(!exactData(p,['operator_key','device_key','role','account_keys','scopes','session_id'])||
         typeof p.operator_key!=='string'||!p.operator_key||p.operator_key.length>256||
         typeof p.device_key!=='string'||!p.device_key||p.device_key.length>256||
         !['manager','baobao'].includes(p.role)||!plainList(p.account_keys,1000)||
         p.account_keys.some(key=>typeof key!=='string'||!key||key.length>256||key==='snapshot-owner-unverified')||
         !plainList(p.scopes,permittedScopes.size)||p.scopes.some(scope=>!permittedScopes.has(scope))||
         typeof p.session_id!=='string'||!/^[0-9a-f]{32}$/.test(p.session_id))return 'unknown';
      // Routing classification only. The normal AuthStore still validates
      // token, installation, account and scope on every normal API request.
      return 'workbench';
    }
    async function connectionKind(){await ready;return selectedKind;}
    // Local, non-secret intent is separate from authentication and from the
    // legacy selection marker (which ambiguously meant logout OR expiry).
    // The requested upgrade enables existing paired devices by default;
    // new explicit stops survive both worker and browser restarts.
    function setWorkbenchResume(allowed){
      const version=++normalIntentVersion;normalBlocked=true;
      const task=normalIntentWrites.then(async()=>{
        if(!workbenchResumeStorage)throw Error('storage_unavailable');
        await workbenchResumeStorage.write(allowed===true);
        if(version!==normalIntentVersion)throw Error('stale_session');
        normalBlocked=allowed!==true;
      });
      normalIntentWrites=task.catch(()=>{});
      return task.catch(error=>{if(version===normalIntentVersion)normalBlocked=true;throw error;});
    }
    async function normalResumeMode(){
      if(normalBlocked)return 'paused';
      if(!workbenchResumeStorage)return 'unavailable';
      const version=normalIntentVersion;
      try{
        const value=await workbenchResumeStorage.read();
        if(version!==normalIntentVersion||normalBlocked)return 'paused';
        return value===false?'paused':value===true||value===undefined?'automatic':'unavailable';
      }catch{return 'unavailable';}
    }
    async function workbenchResume(){
      await ready;
      if(selectedKind!=='workbench')throw Error('forbidden');
      if(normalResume)return normalResume;
      const task=(async()=>{
        // Reuse a healthy token. A heartbeat must never rotate it or renew an
        // identity lease just because the worker was restarted.
        try{return await workbenchNativeStatus();}
        catch(error){if(error.message!=='unauthorized')throw error;}
        const stamp=epoch,version=normalIntentVersion;
        async function current(){
          const mode=await normalResumeMode();
          if(epoch!==stamp||version!==normalIntentVersion)throw Error('stale_session');
          if(mode!=='automatic')throw Error(mode==='paused'?'unauthorized':'storage_unavailable');
        }
        await current();
        const memory=await normalMemory();await current();
        if(!normalDevice(memory))throw Error(memory==null?'unauthorized':'storage_unavailable');
        if(!workbenchRetryStorage)throw Error('storage_unavailable');
        const retryAt=await workbenchRetryStorage.read();await current();
        if(retryAt!==undefined&&(!Number.isSafeInteger(retryAt)||retryAt<0))throw Error('storage_unavailable');
        if(retryAt>now())throw Error('service_error');
        // Reserve a one-minute attempt before any network; survives worker
        // eviction. No body fetch, outbox flush or page proof is retried here.
        await workbenchRetryStorage.write(now()+60000);await current();
        try{
          const result=await workbenchConnect();
          if(result.state!=='connected')throw Error('unauthorized');
          const connectedEpoch=epoch;
          const mode=await normalResumeMode();
          if(epoch!==connectedEpoch||version!==normalIntentVersion)throw Error('stale_session');
          if(mode!=='automatic'){
            const previous=session;session=null;epoch++;await persistCurrent();await disposeToken(previous);
            throw Error('stale_session');
          }
          return await workbenchNativeStatus();
        }catch(error){
          // A denied/changed authority is not a network outage. Stop until an
          // explicit reconnect; never fall back to a different account/realm.
          if(version===normalIntentVersion&&['unauthorized','forbidden','bad_response','storage_unavailable'].includes(error.message))
            await setWorkbenchResume(false).catch(()=>{});
          throw error;
        }
      })();
      const shared=task.finally(()=>{if(normalResume===shared)normalResume=null;});normalResume=shared;return shared;
    }
    // Background-only connection check. Normal authentication says nothing
    // about the owner, selected fan, conversation or a saved-profile binding.
    async function workbenchNativeStatus(){
      await ready;
      if(selectedKind!=='workbench')throw Error('forbidden');
      if(!session)throw Error('unauthorized');
      const original=session,stamp=epoch,expected=normalBinding(original.principal);
      const result=await authenticated('/api/v1/capabilities');
      const actual=normalBinding(result&&result.principal);
      if(original!==session||stamp!==epoch)throw Error('stale_session');
      if(result.principal.session_id!==original.principal.session_id||
         JSON.stringify(actual)!==JSON.stringify(expected))throw Error('bad_response');
      return {format:'workbench-native-status-1',connection_epoch:stamp};
    }
    function normalBinding(p){
      if(sessionKindOf({token:'t'.repeat(43),principal:p})!=='workbench'||
         p.operator_key==='identity-pilot'||p.device_key==='F-identity-pilot'||
         !/^[A-Za-z0-9_:.\-]{1,128}$/.test(p.operator_key)||!/^[A-Za-z0-9_:.\-]{1,128}$/.test(p.device_key)||
         !p.scopes.includes('capabilities:read')||new Set(p.account_keys).size!==p.account_keys.length||
         new Set(p.scopes).size!==p.scopes.length)throw Error('bad_response');
      return {operator_key:p.operator_key,device_key:p.device_key,role:p.role,
        account_keys:[...p.account_keys].sort(),scopes:[...p.scopes].sort(),extension_id:id};
    }
    async function bindingDigest(binding){
      const canonical=Object.fromEntries(Object.keys(binding).sort().map(key=>[key,binding[key]]));
      return hex(new Uint8Array(await cryptoProvider.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(canonical)))));
    }
    function normalDevice(value){
      return exactData(value,['format','extension_id','credential','authority_digest'])&&
        value.format==='workbench-device-1'&&value.extension_id===id&&typeof value.credential==='string'&&
        secretPattern.test(value.credential)&&typeof value.authority_digest==='string'&&/^[0-9a-f]{64}$/.test(value.authority_digest);
    }
    function requireNormalStorage(){
      if(!workbenchDeviceStorage||typeof workbenchDeviceStorage.read!=='function'||typeof workbenchDeviceStorage.write!=='function')throw Error('storage_unavailable');
    }
    async function normalMemory(){
      requireNormalStorage();
      try{return await workbenchDeviceStorage.read();}catch{throw Error('storage_unavailable');}
    }
    function persistNormalDevice(value,work){
      const task=persistence.then(()=>{if(work)assertCurrent(work);return workbenchDeviceStorage.write(value);});
      persistence=task.catch(()=>{});return task;
    }
    async function workbenchStatus(){
      await ready;const memory=await normalMemory();
      if(memory!=null&&!normalDevice(memory))throw Error('storage_unavailable');
      const result=await request('/api/v1/auth/pairing/status',{body:{extension_id:id}});
      if(!exactData(result,['format','available','code_available','authenticated','native_page_verified'])||
         result.format!=='workbench-pairing-status-1'||result.available!==true||typeof result.code_available!=='boolean'||
         result.authenticated!==false||result.native_page_verified!==false)throw Error('bad_response');
      return {available:true,code_available:result.code_available,pairing_remembered:normalDevice(memory),native_page_verified:false};
    }
    async function normalResult(result,paired,digest,work){
      assertCurrent(work);
      if(!exactData(result,paired?['token','principal','device_credential','authority_digest']:['token','principal','authority_digest'])||
         typeof result.token!=='string'||!tokenPattern.test(result.token)||
         typeof result.authority_digest!=='string'||!/^[0-9a-f]{64}$/.test(result.authority_digest)||
         paired&&(typeof result.device_credential!=='string'||!secretPattern.test(result.device_credential)))throw Error('bad_response');
      const actual=await bindingDigest(normalBinding(result.principal));assertCurrent(work);
      if(actual!==result.authority_digest||digest&&actual!==digest)throw Error('bad_response');
    }
    async function installNormalSession(result,work){
      assertCurrent(work);session={token:result.token,principal:structuredClone(result.principal)};selectedKind='workbench';
      try{await persistCurrent();}catch{
        if(work.stamp===epoch&&pilotWork===work){session=null;epoch++;await persistCurrent().catch(()=>{});}
        throw Error('storage_unavailable');
      }
      assertCurrent(work);return {...publicSession(),state:'connected',native_page_verified:false};
    }
    async function workbenchPair(code){
      await ready;requireNormalStorage();
      if(typeof code!=='string'||!secretPattern.test(code))throw Error('invalid_request');
      if(session)throw Error('already_authenticated');
      return launchPilot('workbench-pair',async work=>{
        if(await normalMemory()!=null)throw Error('storage_unavailable');assertCurrent(work);
        let result;
        try{
          result=await request('/api/v1/auth/pairing/redeem',{body:{extension_id:id,pairing_code:code}});
          await normalResult(result,true,null,work);
          await persistNormalDevice({format:'workbench-device-1',extension_id:id,credential:result.device_credential,
            authority_digest:result.authority_digest},work);
          return await installNormalSession(result,work);
        }catch(error){await disposeToken(result);throw error;}
      });
    }
    async function workbenchConnect(){
      await ready;requireNormalStorage();
      if(session&&selectedKind!=='workbench')throw Error('forbidden');
      if(pilotWork){if(pilotWork.kind==='workbench-connect'&&pilotWork.stamp===epoch)return pilotWork.promise;throw Error('stale_session');}
      return launchPilot('workbench-connect',async work=>{
        const device=await normalMemory();assertCurrent(work);
        if(!normalDevice(device)){
          if(device!=null)throw Error('storage_unavailable');
          return {...publicSession(),state:'needs_pairing',native_page_verified:false};
        }
        let result;const previous=session;
        try{
          const client=base64url(cryptoProvider.getRandomValues(new Uint8Array(32)));
          const challenge=await request('/api/v1/auth/pairing/challenge',{body:{extension_id:id,client_challenge:client}});
          assertCurrent(work);
          if(!exactData(challenge,['extension_id','client_challenge','server_challenge','proof','authority_digest'])||
             challenge.extension_id!==id||challenge.client_challenge!==client||challenge.authority_digest!==device.authority_digest||
             typeof challenge.server_challenge!=='string'||!secretPattern.test(challenge.server_challenge)||
             typeof challenge.proof!=='string'||!/^[0-9a-f]{64}$/.test(challenge.proof))throw Error('bad_response');
          const encode=value=>new TextEncoder().encode(value),suffix=id+'\n'+device.authority_digest+'\n'+client+'\n'+challenge.server_challenge;
          const key=await cryptoProvider.subtle.importKey('raw',encode(device.credential),{name:'HMAC',hash:'SHA-256'},false,['sign','verify']);
          assertCurrent(work);
          const trusted=await cryptoProvider.subtle.verify('HMAC',key,Uint8Array.from(challenge.proof.match(/../g),part=>parseInt(part,16)),encode('workbench-host-1\n'+suffix));
          assertCurrent(work);if(!trusted)throw Error('bad_response');
          const proof=hex(new Uint8Array(await cryptoProvider.subtle.sign('HMAC',key,encode('workbench-client-1\n'+suffix))));
          assertCurrent(work);
          result=await request('/api/v1/auth/pairing/session',{body:{extension_id:id,client_challenge:client,server_challenge:challenge.server_challenge,proof}});
          await normalResult(result,false,device.authority_digest,work);
          const installed=await installNormalSession(result,work);await disposeToken(previous);assertCurrent(work);return installed;
        }catch(error){
          if(work.stamp===epoch&&pilotWork===work){session=null;selectedKind='workbench';epoch++;await persistCurrent().catch(()=>{});}
          await disposeToken(result);throw error;
        }
      });
    }
    async function workbenchForget(){
      await ready;requireNormalStorage();if(session&&selectedKind!=='workbench')throw Error('forbidden');
      const previous=session;session=null;selectedKind='workbench';const work={kind:'workbench-forget',stamp:++epoch};pilotWork=work;
      work.promise=(async()=>{let failed=false;
        try{await persistNormalDevice(null);}catch{failed=true;}
        try{await persistCurrent();}catch{failed=true;}
        await disposeToken(previous);if(failed)throw Error('storage_unavailable');
        return {authenticated:false,session_kind:'workbench',state:'needs_pairing',native_page_verified:false};
      })().finally(()=>{if(pilotWork===work)pilotWork=null;});return work.promise;
    }
    function validDevice(value) {
      return exactData(value,['format','extension_id','credential'])&&value.format==='identity-pilot-device-1'&&
        value.extension_id===id&&typeof value.credential==='string'&&secretPattern.test(value.credential);
    }
    function assertCurrent(work) {if(work.stamp!==epoch||pilotWork!==work)throw new Error('stale_session');}
    function launchPilot(kind,task) {
      if(pilotWork)throw new Error('stale_session');
      const work={kind,stamp:++epoch};pilotWork=work;
      work.promise=Promise.resolve().then(()=>task(work)).finally(()=>{if(pilotWork===work)pilotWork=null;});
      return work.promise;
    }
    function requireDeviceStorage() {
      if(!deviceStorage||typeof deviceStorage.read!=='function'||typeof deviceStorage.write!=='function')throw new Error('storage_unavailable');
    }
    async function disposeToken(value) {
      // Only a finite token goes to the fixed logout endpoint; never return it.
      if(value&&typeof value.token==='string'&&tokenPattern.test(value.token))await request('/api/v1/auth/logout',{token:value.token,body:{}}).catch(()=>{});
    }
    async function installPilotSession(result,work) {
      assertCurrent(work);
      session={token:result.token,principal:structuredClone(result.principal)};selectedKind='identity_pilot';
      try{await persistCurrent();}
      catch{
        if(work.stamp===epoch&&pilotWork===work){session=null;epoch++;await persistCurrent().catch(()=>{});}
        throw new Error('storage_unavailable');
      }
      assertCurrent(work);
      return {...publicSession(),state:'connected'};
    }
    async function clearPilotSession(work) {
      assertCurrent(work);
      session=null;work.stamp=++epoch;
      try{await persistCurrent();}catch{throw new Error('storage_unavailable');}
      assertCurrent(work);
    }
    async function pilotPair(code) {
      await ready;requireDeviceStorage();
      if(typeof code!=='string'||!secretPattern.test(code))throw new Error('invalid_request');
      if(session)throw new Error('already_authenticated');
      return launchPilot('pair',async work=>{
        const remembered=await deviceStorage.read();assertCurrent(work);
        if(remembered!==undefined&&remembered!==null)throw new Error('invalid_request');
        if((await pilotConnection()).state!=='connected')throw new Error('unavailable');assertCurrent(work);
        let result;
        try{
          result=await request('/api/v1/identity-pilot/pair',{body:{extension_id:id,pairing_code:code}});
          assertCurrent(work);if(!validSession(result,true))throw new Error('bad_response');
          await persistDevice({format:'identity-pilot-device-1',extension_id:id,credential:result.device_credential},work);
          assertCurrent(work);return await installPilotSession(result,work);
        }catch(error){await disposeToken(result);throw error;}
      });
    }
    function base64url(bytes) {
      const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';let result='',bits=0,acc=0;
      for(const byte of bytes){acc=(acc<<8)|byte;bits+=8;while(bits>=6){bits-=6;result+=alphabet[(acc>>>bits)&63];}}
      if(bits)result+=alphabet[(acc<<(6-bits))&63];return result;
    }
    function hex(bytes) {return Array.from(bytes,byte=>byte.toString(16).padStart(2,'0')).join('');}
    async function proveHost(device,work) {
      if(!cryptoProvider||typeof cryptoProvider.getRandomValues!=='function'||!cryptoProvider.subtle)throw new Error('unavailable');
      const client=base64url(cryptoProvider.getRandomValues(new Uint8Array(32)));
      const challenge=await request('/api/v1/identity-pilot/device-challenge',{body:{extension_id:id,client_challenge:client}});
      assertCurrent(work);
      if(!exactData(challenge,['extension_id','client_challenge','server_challenge','proof'])||challenge.extension_id!==id||
        challenge.client_challenge!==client||typeof challenge.server_challenge!=='string'||!secretPattern.test(challenge.server_challenge)||
        typeof challenge.proof!=='string'||!/^[0-9a-f]{64}$/.test(challenge.proof))throw new Error('bad_response');
      const encode=text=>new TextEncoder().encode(text);
      const key=await cryptoProvider.subtle.importKey('raw',encode(device.credential),{name:'HMAC',hash:'SHA-256'},false,['sign','verify']);
      assertCurrent(work);
      const suffix=id+'\n'+client+'\n'+challenge.server_challenge;
      const hostProof=Uint8Array.from(challenge.proof.match(/../g),part=>Number.parseInt(part,16));
      const verified=await cryptoProvider.subtle.verify('HMAC',key,hostProof,encode('identity-pilot-host-1\n'+suffix));
      assertCurrent(work);if(!verified)throw new Error('bad_response');
      const proof=hex(new Uint8Array(await cryptoProvider.subtle.sign('HMAC',key,encode('identity-pilot-client-1\n'+suffix))));
      assertCurrent(work);
      return {extension_id:id,client_challenge:client,server_challenge:challenge.server_challenge,proof};
    }
    async function pilotConnect() {
      await ready;
      if(selectedKind==='workbench')return {...publicSession(),state:'workbench_session_retained'};
      requireDeviceStorage();
      if(pilotWork){if(pilotWork.kind==='connect'&&pilotWork.stamp===epoch)return pilotWork.promise;throw new Error('stale_session');}
      return launchPilot('connect',async work=>{
        let result;
        try{
          const device=await deviceStorage.read();assertCurrent(work);
          if(!validDevice(device)){
            await clearPilotSession(work);
            return {state:device===undefined||device===null?'needs_pairing':'pairing_memory_unavailable',authenticated:false};
          }
          if((await pilotConnection()).state!=='connected')throw new Error('unavailable');assertCurrent(work);
          // Type-only health is not host authentication. The secret never travels
          // over HTTP: prove the host before transporting any cached bearer token.
          const proof=await proveHost(device,work);
          if(session&&validSession(session)){
            const previous=session;
            try{
              const status=await request('/api/v1/identity-pilot/status',{token:previous.token});assertCurrent(work);
              if(!status||status.account_key!=='snapshot-owner-unverified'||status.provider_namespace!=='douyin-export-sender'||
                typeof status.artifact_sha256!=='string'||!/^[0-9a-f]{64}$/.test(status.artifact_sha256)||
                status.private_context_verified!==false||status.owner_verified!==false)throw new Error('bad_response');
              return {...publicSession(),state:'connected'};
            }catch(error){
              assertCurrent(work);if(error.message!=='unauthorized')throw error;
              await clearPilotSession(work);
            }
          }
          assertCurrent(work);result=await request('/api/v1/identity-pilot/device-session',{body:proof});
          assertCurrent(work);if(!validSession(result))throw new Error('bad_response');
          return await installPilotSession(result,work);
        }catch(error){
          // Keep device memory for retry; a failed or untrusted connection must
          // not leave status() advertising a cached authenticated session.
          if(work.stamp===epoch&&pilotWork===work){
            try{await clearPilotSession(work);}catch(clearError){error=clearError;}
          }
          await disposeToken(result);throw error;
        }
      });
    }
    async function pilotForget() {
      await ready;
      if(selectedKind==='workbench')throw new Error('forbidden');
      requireDeviceStorage();
      // Revoke first, even if an earlier request or storage write is in flight.
      const previous=session;session=null;
      const work={kind:'forget',stamp:++epoch};pilotWork=work;
      work.promise=(async()=>{
        let failure;
        try{await persistDevice(null);}catch{failure=new Error('storage_unavailable');}
        try{await persistCurrent();}catch{failure=new Error('storage_unavailable');}
        await disposeToken(previous);
        if(failure)throw failure;
        return {state:'needs_pairing',authenticated:false};
      })().finally(()=>{if(pilotWork===work)pilotWork=null;});
      return work.promise;
    }
    // Trusted worker dependency only. No token or principal leaves the bridge;
    // a new authentication epoch invalidates ephemeral historical recall.
    function pilotEpoch(){return session?epoch:null;}
    return {handle,flush,pilotConnection,pilotStatus,pilotLookup,pilotPair,pilotConnect,pilotForget,pilotEpoch,nativeProfile,nativeEditor,nativeRegistrationStatus,nativeRuntimeResolve,connectionKind,workbenchNativeStatus,workbenchStatus,workbenchPair,workbenchConnect,workbenchForget,workbenchResume,setWorkbenchResume};
  }
  return {createBridge};
});
