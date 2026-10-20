(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DSHHistorySummary=api;})(globalThis,function(){
  'use strict';
  const keys=['format','state','scope','coverage','message_count','conversation_count','private_message_count','owner_verified','private_context_verified','formal_identity_match'];
  function normalize(value,eligible=false){
    try{
      if(!value||typeof value!=='object'||Array.isArray(value))return null;
      const fields=Object.getOwnPropertyDescriptors(value);
      if(Object.keys(fields).length!==keys.length||keys.some(k=>!fields[k]||!Object.hasOwn(fields[k],'value')))return null;
      const v=Object.fromEntries(keys.map(k=>[k,fields[k].value]));
      if(v.format!=='historical-sender-summary-1'||!['available','unknown'].includes(v.state)||v.scope!=='export_sender_all_contexts'||v.coverage!=='qualified_root_sender_pairs_only'||v.private_message_count!==null||v.owner_verified!==false||v.private_context_verified!==false||v.formal_identity_match!==false)return null;
      if(v.state==='available'){
        if(!eligible||!Number.isSafeInteger(v.message_count)||v.message_count<1||v.message_count>1000000000||!Number.isSafeInteger(v.conversation_count)||v.conversation_count<1||v.conversation_count>100000||v.conversation_count>v.message_count)return null;
      }else if(v.message_count!==null||v.conversation_count!==null)return null;
      return Object.freeze(v);
    }catch{return null;}
  }
  function fromResponse(response,eligible){
    try{const f=Object.getOwnPropertyDescriptor(response,'history_summary');return f&&Object.hasOwn(f,'value')?normalize(f.value,eligible):null;}catch{return null;}
  }
  return {normalize,fromResponse};
});
