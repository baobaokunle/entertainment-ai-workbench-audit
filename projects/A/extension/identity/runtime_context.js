(function(root,factory){
  const node=typeof module==='object'&&module.exports;
  const api=factory(node?require('../lib/protocol.js'):root.DSHProtocol);
  if(node)module.exports=api;else root.DSHNativeRuntime=api;
})(globalThis,function(P){
  'use strict';
  const same=(a,b)=>!!a&&!!b&&Object.keys(a).length===Object.keys(b).length&&Object.keys(a).every(k=>a[k]===b[k]);
  function create({readMetadata,resolve,registrationStatus,epoch,clock=()=>Date.now()}){
    const records=new Set();
    function live(r){const now=clock();return r.active&&r.epoch===epoch()&&now>=r.started&&now<r.deadline&&r.current()===true;}
    function invalidate(){for(const r of records)r.active=false;records.clear();}
    function select(proposal){return [...records].find(r=>live(r)&&same(proposal.scope,r.scope)&&same(proposal.context,r.context)&&same(proposal.query,r.grant.query)&&same(proposal.binding,r.grant.binding));}
    function grantValid(g,metadata){
      if(!g||!g.policy||!g.binding||!g.query||typeof g.fingerprint!=='string'||!/^[0-9a-f]{64}$/.test(g.fingerprint))return false;
      const q=P.nativeProfileRequest(g.query),p=g.policy;
      return q.source_id.startsWith('native-reviewed-')&&q.owner_uid===metadata.owner_uid&&q.sec_uid===metadata.sec_uid&&
        q.platform_conversation_key===metadata.platform_conversation_key&&
        ['native-store-metadata-1','native-bound-conversation-metadata-1','native-bound-context-data-1','native-panel-context-data-1'].includes(metadata.format)&&p.reader===metadata.format&&
        ['account_namespace','owner_provider_namespace','provider_namespace'].every(k=>typeof p[k]==='string'&&/^[A-Za-z0-9_-]{1,200}$/.test(p[k]))&&
        Object.keys(g.binding).length===3&&['account_key','person_key','conversation_key'].every(k=>typeof g.binding[k]==='string'&&/^[A-Za-z0-9_-]{1,256}$/.test(g.binding[k]))&&
        (p.expires_at===null||typeof p.expires_at==='string'&&Number.isFinite(Date.parse(p.expires_at)));
    }
    async function read(scope,current){
      for(const r of records)if(!live(r)){r.active=false;records.delete(r);}
      if(records.size>=128)return null;
      const started=clock(),revision=epoch();
      const stable=()=>current()===true&&epoch()===revision&&clock()>=started&&clock()<started+30000;
      if(!stable())return null;
      const first=await readMetadata(scope,stable);if(!first||!stable())return null;
      const result=await resolve(first.metadata,stable);if(!stable()||result.state!=='permitted'||!grantValid(result.grant,first.metadata))return null;
      const last=await readMetadata(scope,stable);
      if(!last||!stable()||!same(first.metadata,last.metadata)||!same(first.scope,scope)||!same(last.scope,scope))return null;
      const grant=result.grant,p=grant.policy,q=grant.query;
      const context=Object.freeze({account_key:grant.binding.account_key,account_namespace:p.account_namespace,
        owner_provider_namespace:p.owner_provider_namespace,owner_identity_kind:'uid',owner_identity_value:q.owner_uid,
        tab_id:String(scope.tab_id),platform_conversation_key:q.platform_conversation_key,object_kind:'person',identity_kind:'sec_uid',
        identity_value:q.sec_uid,subject_role:'conversation_peer',provider_namespace:p.provider_namespace,
        source_id:q.source_id,source_version:q.source_version,object_token:scope.head_token,route_key:'/chat',state:'eligible'});
      const deadline=p.expires_at===null?started+30000:Math.min(started+30000,Date.parse(p.expires_at));
      const r={scope,context,grant,metadata:first.metadata,started,deadline,renewable:p.expires_at===null,epoch:revision,current,active:true};
      if(!live(r))return null;records.add(r);
      return {scope,context,binding:grant.binding};
    }
    async function status(query,current,proposal){
      const r=select(proposal);if(!r)throw Error('context_unavailable');
      if(r.checking){r.active=false;records.delete(r);throw Error('context_unavailable');}
      r.checking=true;
      const stable=()=>live(r);
      const check=async()=>stable()&&await current()===true&&stable();
      try{
        // Only an independently granted until-revoked policy may refresh a
        // still-live page lease after all metadata and authority checks finish.
        if(!await check())throw Error('context_unavailable');
        const observed=await readMetadata(r.scope,stable);
        if(!observed||!await check()||!same(observed.scope,r.scope)||!same(observed.metadata,r.metadata))throw Error('context_unavailable');
        const result=await resolve(r.metadata,check);
        if(!await check()||result.state!=='permitted'||!grantValid(result.grant,r.metadata)||result.grant.fingerprint!==r.grant.fingerprint)throw Error('context_unavailable');
        const registered=await registrationStatus(query,check);
        if(!await check())throw Error('context_unavailable');
        const final=await readMetadata(r.scope,stable);
        if(!final||!await check()||!same(final.scope,r.scope)||!same(final.metadata,r.metadata))throw Error('context_unavailable');
        if(r.renewable&&result.grant.policy.expires_at===null)r.deadline=clock()+30000;
        return registered;
      }catch(error){r.active=false;records.delete(r);throw Error('context_unavailable');}
      finally{r.checking=false;}
    }
    return Object.freeze({read,authorize:proposal=>!!select(proposal),registrationStatus:status,invalidate,
      renewable:proposal=>select(proposal)?.renewable===true,expiresAt:proposal=>select(proposal)?.deadline??null});
  }
  return Object.freeze({create});
});
