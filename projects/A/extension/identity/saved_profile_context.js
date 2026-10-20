(function(root,factory){
  const node=typeof module==='object'&&module.exports;
  const api=factory(node?require('./context_guard.js'):root.DSHIdentityContext,node?require('../lib/protocol.js'):root.DSHProtocol);
  if(node)module.exports=api;else root.DSHSavedProfileContext=api;
})(globalThis,function(I,P){
  'use strict';
  const scopeKeys=['tab_id','document_id','context_id','generation','head_token','header_bound'];
  function exact(value,keys){
    if(!value||Object.getPrototypeOf(value)!==Object.prototype)throw Error('context_incomplete');
    const fields=Object.getOwnPropertyDescriptors(value);
    if(Reflect.ownKeys(fields).length!==keys.length||keys.some(k=>!fields[k]||!Object.hasOwn(fields[k],'value')))throw Error('context_incomplete');
    return Object.fromEntries(keys.map(k=>[k,fields[k].value]));
  }
  function scope(value){
    const result=exact(value,scopeKeys);
    if(!Number.isSafeInteger(result.tab_id)||result.tab_id<0||!Number.isSafeInteger(result.generation)||result.generation<0||result.header_bound!==true||
      ['document_id','context_id','head_token'].some(k=>typeof result[k]!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(result[k])))throw Error('context_incomplete');
    return Object.freeze(result);
  }
  // Only trusted background dependencies may supply read/authorize. Neither a
  // page DTO nor a saved-card response supplies authority or an expected binding.
  function create({read=null,authorize=null,registrationStatus=null,renewable=null,expiresAt=null,editor=null,lookup,clock=()=>Date.now()}={}){
    if(typeof lookup!=='function'||typeof clock!=='function'||
      (read!==null&&typeof read!=='function')||(authorize!==null&&typeof authorize!=='function')||
      (registrationStatus!==null&&typeof registrationStatus!=='function')||
      (renewable!==null&&typeof renewable!=='function')||(expiresAt!==null&&typeof expiresAt!=='function'))throw Error('context_configuration');
    const enabled=typeof read==='function'&&typeof authorize==='function',leases=new WeakMap(),listeners=new Set();
    let revision=0;
    // Trusted background notification only: this can withdraw authority, never
    // supply identity or approval. Pending reads are cancelled as well as leases.
    function invalidate(){
      revision++;
      for(const listener of [...listeners])try{listener();}catch{/* Continue clearing other consumers. */}
    }
    function onInvalidate(listener){
      if(typeof listener!=='function')throw Error('context_configuration');
      listeners.add(listener);return ()=>listeners.delete(listener);
    }
    function current(lease){
      const state=lease&&leases.get(lease);if(!state)return false;
      try{return state.revision===revision&&state.hostCurrent()===true&&state.guard.current(state.stamp)&&state.revision===revision;}catch{return false;}
    }
    function release(lease){const state=lease&&leases.get(lease);if(state){leases.delete(lease);state.guard.invalidate('released');}}
    function canRenew(lease){
      const state=lease&&leases.get(lease);
      return !!state&&state.registrationRequired&&!!renewable&&!!expiresAt&&current(lease)&&renewable(state.proposal)===true&&current(lease);
    }
    function leaseDeadline(lease){
      const state=lease&&leases.get(lease);if(!state||!current(lease))return null;
      const deadline=expiresAt?expiresAt(state.proposal):state.stamp.expires_at;
      return Number.isFinite(deadline)&&current(lease)?Math.min(deadline,state.stamp.expires_at):null;
    }
    function revoke(lease,state){
      if(leases.get(lease)!==state)return false;
      // End the lease before notifications: Host may synchronously release it.
      release(lease);
      for(const listener of [...listeners])try{listener(lease);}catch{}
      return false;
    }
    async function recheck(lease,hostLive){
      const state=lease&&leases.get(lease);
      if(!state||typeof hostLive!=='function')return false;
      const live=async()=>leases.get(lease)===state&&current(lease)&&await hostLive()===true&&
        leases.get(lease)===state&&current(lease);
      if(!state.registrationRequired)return live();
      // Overlapping checks end this lease. A late success cannot undo failure.
      if(state.checking)return revoke(lease,state);
      const checking={};state.checking=checking;
      try{
        if(!await live())return revoke(lease,state);
        const locator=P.nativeRegistrationRequest({source_id:lease.query.source_id,
          source_version:lease.query.source_version,account_key:lease.binding.account_key});
        // This trusted bridge dependency verifies session/device and request
        // digest. It never supplies the independent context authorizer.
        const raw=await registrationStatus(locator,live,state.proposal);
        if(!await live())return revoke(lease,state);
        const field=(value,key)=>{const d=value&&Object.getOwnPropertyDescriptor(value,key);return d&&Object.hasOwn(d,'value')?d.value:undefined;};
        const device=field(field(raw,'registration'),'native_device_key');
        const result=P.nativeRegistrationResponse(raw,locator,device);
        if(result.state!=='registered'||typeof device!=='string'||!/^[A-Za-z0-9_:.\-]{1,128}(?![\s\S])/.test(device)||
          Object.keys(lease.query).some(k=>result.registration.query[k]!==lease.query[k])||
          Object.keys(lease.binding).some(k=>result.registration.binding[k]!==lease.binding[k]))return revoke(lease,state);
        const fingerprint=JSON.stringify(result.registration);
        if(state.registrationFingerprint!==null&&state.registrationFingerprint!==fingerprint)return revoke(lease,state);
        if(!await live())return revoke(lease,state);
        state.registrationFingerprint=fingerprint;
        if(canRenew(lease)){
          const old=state.guard;
          const guard=new I.ContextGuard({clock,authorize:()=>state.revision===revision&&state.hostCurrent()===true&&
            authorize(state.proposal)===true&&state.revision===revision&&state.hostCurrent()===true});
          const stamp=guard.lock(guard.observe(state.proposal.context));
          // Never revive an expired old stamp or a reentrantly withdrawn lease.
          if(!stamp||leases.get(lease)!==state||!current(lease)){guard.invalidate('unavailable');return revoke(lease,state);}
          state.guard=guard;state.stamp=stamp;old.invalidate('rechecked');
        }
        return true;
      }catch{return revoke(lease,state);}
      finally{if(state.checking===checking)delete state.checking;}
    }
    async function resolve(value,hostCurrent){
      if(!enabled||typeof hostCurrent!=='function')return null;
      let guard,lease;
      try{
        const expected=scope(value),started=revision;
        const live=()=>started===revision&&hostCurrent()===true&&started===revision;
        if(!live())return null;
        const raw=await read(expected,live);if(!live())return null;
        const input=exact(raw,['scope','context','binding']),observed=scope(input.scope);
        if(scopeKeys.some(k=>observed[k]!==expected[k]))return null;
        const context=I.normalize(input.context),binding=exact(input.binding,['account_key','person_key','conversation_key']);
        if(Object.values(binding).some(v=>typeof v!=='string'||!/^[A-Za-z0-9_-]{1,256}$/.test(v))||
          context.account_key!==binding.account_key||context.tab_id!==String(expected.tab_id)||context.object_token!==expected.head_token||
          context.route_key!=='/chat'||context.object_kind!=='person'||context.subject_role!=='conversation_peer'||
          context.owner_identity_kind!=='uid'||context.identity_kind!=='sec_uid'||context.state!=='eligible')return null;
        Object.freeze(binding);
        // Query has one source of truth. A second query in read() is rejected.
        const query=P.nativeProfileRequest({source_id:context.source_id,source_version:context.source_version,
          owner_uid:context.owner_identity_value,sec_uid:context.identity_value,platform_conversation_key:context.platform_conversation_key});
        const proposal=Object.freeze({scope:expected,context,query,binding});
        const registrationRequired=query.source_id.startsWith('native-reviewed-');
        if(registrationRequired&&!registrationStatus)return null;
        // authorize must independently check namespaces, owner/peer/private
        // semantics, source version and binding, including revocation each time.
        guard=new I.ContextGuard({clock,authorize:()=>live()&&authorize(proposal)===true&&live()});
        const stamp=guard.lock(guard.observe(context));if(!stamp)return null;
        lease=Object.freeze({query,binding});leases.set(lease,{guard,stamp,hostCurrent,revision:started,proposal,
          registrationRequired,registrationFingerprint:null});
        if(registrationRequired&&!await recheck(lease,live))return null;
        if(!current(lease)){release(lease);return null;}return lease;
      }catch{if(lease)release(lease);if(guard)guard.invalidate('unavailable');return null;}
    }
    async function query(lease,hostLive){
      const state=lease&&leases.get(lease);
      if(!state||typeof hostLive!=='function')throw Error('context_unavailable');
      const live=async()=>current(lease)&&await hostLive()===true&&current(lease);
      if(!await (state.registrationRequired?recheck(lease,live):live()))throw Error('context_unavailable');
      const result=await lookup(lease.query,live);
      if(!await (state.registrationRequired?recheck(lease,live):live()))throw Error('context_unavailable');
      return result;
    }
    async function edit(lease,command,hostLive){
      const state=lease&&leases.get(lease);
      if(!state||typeof editor!=='function'||typeof hostLive!=='function')throw Error('context_unavailable');
      const live=async()=>current(lease)&&await hostLive()===true&&current(lease);
      if(!await recheck(lease,live))throw Error('stale_session');
      const result=await editor(lease.binding,command,live);
      if(!await recheck(lease,live))throw Error('stale_session');
      return result;
    }
    return Object.freeze({enabled,resolve,current,release,lookup:query,edit,recheck,invalidate,onInvalidate,
      renewable:canRenew,expiresAt:leaseDeadline});
  }
  return Object.freeze({create});
});
