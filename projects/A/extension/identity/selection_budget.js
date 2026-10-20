(function(root,factory){const v=factory();if(typeof module==='object'&&module.exports)module.exports=v;else root.DSHSelectionBudget=v;})(globalThis,function(){
  'use strict';
  const VALIDATION_SCOPE='task001-selection-receipt-20261013';
  function validationScope(config){
    if(!config||typeof config!=='object')return null;
    const field=Object.getOwnPropertyDescriptor(config,'validation_budget_scope');
    if(!field)return undefined;
    return Object.hasOwn(field,'value')&&field.value===VALIDATION_SCOPE?VALIDATION_SCOPE:null;
  }
  function createScopedBudget({storageForKey,digest}){
    const budgets=new Map();
    function scoped(scope){
      const key=scope===undefined?'dshSelectionTrialBudgetV1':scope===VALIDATION_SCOPE?'dshSelectionReceiptBudget20261013':null;
      if(!key)throw Error('invalid_request');
      if(!budgets.has(key))budgets.set(key,new SelectionBudget({storage:storageForKey(key),digest}));
      return budgets.get(key);
    }
    return {reserve:(query,scope)=>scoped(scope).reserve(query),contains:(query,scope)=>scoped(scope).contains(query)};
  }
  class SelectionBudget {
    constructor({storage,digest=async value=>{
      const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value));
      return Array.from(new Uint8Array(bytes),b=>b.toString(16).padStart(2,'0')).join('');
    }}){this.storage=storage;this.digest=digest;this.tail=Promise.resolve();this.blocked=false;}
    contains(query){return this.check(query,false);}
    reserve(query){return this.check(query,true);}
    check(query,reserve){
      const task=this.tail.then(async()=>{
        if(this.blocked)throw Error('storage_unavailable');
        try{
          const hash=await this.digest(JSON.stringify([query.account_key,query.provider_namespace,query.artifact_sha256,query.sec_uid]));
          if(typeof hash!=='string'||!/^[a-f0-9]{64}$/.test(hash))throw Error('storage_unavailable');
          const stored=await this.storage.read();
          const valid=v=>v&&Object.getPrototypeOf(v)===Object.prototype&&Object.keys(v).length===2&&v.format==='selection-trial-budget-1'&&Array.isArray(v.hashes)&&v.hashes.length<=2&&new Set(v.hashes).size===v.hashes.length&&v.hashes.every(x=>typeof x==='string'&&/^[a-f0-9]{64}$/.test(x));
          if(stored!==undefined&&!valid(stored))throw Error('storage_unavailable');
          if(!reserve)return !!stored&&stored.hashes.includes(hash);
          const hashes=stored?stored.hashes.slice():[];
          if(hashes.includes(hash))return true;
          if(hashes.length>=2)return false;
          hashes.push(hash);const next={format:'selection-trial-budget-1',hashes};
          await this.storage.write(next);
          const confirmed=await this.storage.read();
          if(!valid(confirmed)||JSON.stringify(confirmed.hashes)!==JSON.stringify(hashes))throw Error('storage_unavailable');
          return true;
        }catch{this.blocked=true;throw Error('storage_unavailable');}
      });
      this.tail=task.catch(()=>{});return task;
    }
  }
  return {SelectionBudget,VALIDATION_SCOPE,validationScope,createScopedBudget};
});
