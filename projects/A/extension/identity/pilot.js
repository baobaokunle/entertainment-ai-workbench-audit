(function(root,factory){
  const navigation=typeof module==='object'&&module.exports?require('./profile_navigation.js'):root.DSHProfileNavigation;
  const value=factory(navigation,typeof module==='object'&&module.exports?require('./history_summary.js'):root.DSHHistorySummary);
  if(typeof module==='object'&&module.exports)module.exports=value;else root.DSHIdentityPilot=value;
})(globalThis,function(navigation,H){
  'use strict';
  // Passive host wiring. Does not click, collect messages, change tabs, or call
  // unauthenticated HTTP. The real producer/lookup are trusted dependencies.
  class IdentityPilot {
    #coordinator; #lookup; #render; #artifact; #proof=null; #label=null; #serial=0;
    constructor({readCurrent,approve,lookup,render,artifactSha256,clock,ttlMs=10000}) {
      if(typeof lookup!=='function'||typeof render!=='function'||!/^[a-f0-9]{64}$/.test(artifactSha256))throw new Error('pilot_configuration');
      this.#coordinator=new navigation.ProfileNavigationCoordinator({readCurrent,approve,clock:clock||(()=>performance.now()),ttlMs});
      this.#lookup=lookup;this.#render=render;this.#artifact=artifactSha256;
    }
    invalidate() {
      this.#serial++;this.#coordinator.invalidate();this.#proof=null;this.#label=null;
      this.#render(Object.freeze({state:'pending',sample:null,historical_identity:'not_checked',private_context:'unverified'}));
    }
    begin(label) {
      this.invalidate();
      if(!['甲','乙','当前对象'].includes(label))return null;
      this.#label=label;this.#proof=this.#coordinator.begin();
      return this.#proof;
    }
    created(proof,event){return proof===this.#proof&&this.#coordinator.created(proof,event);}
    replaced(proof,event){return proof===this.#proof&&this.#coordinator.replaced(proof,event);}
    navigated(proof,event){return proof===this.#proof&&this.#coordinator.navigated(proof,event);}
    coordinatorStatus(){return this.#coordinator.diagnosticStatus();}
    refresh() {
      if(this.#proof&&!this.#coordinator.current(this.#proof))this.invalidate();
    }
    #stale(proof,serial) {
      if(proof===this.#proof&&serial===this.#serial)this.invalidate();
      return {state:'stale'};
    }
    async query(proof) {
      const candidate=this.#coordinator.candidate(proof),serial=this.#serial;
      if(!candidate||proof!==this.#proof)return this.#stale(proof,serial);
      const request=Object.freeze({account_key:proof.scope.account_key,provider_namespace:proof.scope.provider_namespace,artifact_sha256:this.#artifact,sec_uid:candidate.identity_value});
      let response;
      try {response=await this.#lookup(request,()=>proof===this.#proof&&serial===this.#serial&&this.#coordinator.current(proof));}
      catch {return this.#reject(proof,serial,'lookup_unavailable');}
      if(proof!==this.#proof||serial!==this.#serial||!this.#coordinator.current(proof))return this.#stale(proof,serial);
      // Do not execute response getters supplied by an untrusted producer.
      if (!response || Object.getPrototypeOf(response)!==Object.prototype)return this.#reject(proof,serial,'response_conflict');
      const fields=Object.getOwnPropertyDescriptors(response),keys=['format','account_key','provider_namespace','artifact_sha256','sec_uid','status','candidate_count','private_context_verified','owner_verified','formal_identity_match'];
      if(keys.some(key=>!fields[key]||!Object.hasOwn(fields[key],'value')))return this.#reject(proof,serial,'response_conflict');
      const summary=H.fromResponse(response,fields.status.value==='pending'&&fields.candidate_count.value===1);
      response=Object.fromEntries(keys.map(key=>[key,fields[key].value]));
      if(!response||response.format!=='pending-profile-lookup-1'||response.account_key!==request.account_key||
          response.provider_namespace!==request.provider_namespace||response.artifact_sha256!==request.artifact_sha256||
          response.sec_uid!==request.sec_uid||!['pending','zero','conflict'].includes(response.status)||
          !Number.isSafeInteger(response.candidate_count)||response.candidate_count<0||
          (response.status==='pending'&&response.candidate_count!==1)||
          response.private_context_verified!==false||response.owner_verified!==false||response.formal_identity_match!==false)
        return this.#reject(proof,serial,'response_conflict');
      const view=Object.freeze({state:'candidate',sample:this.#label,historical_identity:response.status==='pending'?'sender_candidate':response.status,
        private_context:'unverified',owner:'unverified',formal_binding:false,artifact_sha256:this.#artifact,...(summary?{history_summary:summary}:{})});
      // Commit check immediately before the only display sink; never emit IDs.
      if(!this.#coordinator.current(proof)||serial!==this.#serial)return this.#stale(proof,serial);
      this.#render(view);
      if(!this.#coordinator.current(proof)||serial!==this.#serial)return this.#stale(proof,serial);
      return view;
    }
    #reject(proof,serial,reason) {
      if(proof!==this.#proof||serial!==this.#serial||!this.#coordinator.current(proof))return this.#stale(proof,serial);
      this.#render(Object.freeze({state:'pending',sample:this.#label,historical_identity:reason,private_context:'unverified'}));
      return {state:'pending',reason};
    }
  }
  return {IdentityPilot};
});
