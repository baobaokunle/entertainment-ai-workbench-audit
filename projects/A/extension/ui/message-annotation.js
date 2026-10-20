(function (root,factory) {
  const value = factory(typeof module === 'object' && module.exports ? require('../adapters/chat.js') : root.DSHChat);
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHAnnotation = value;
})(globalThis,function (Chat) {
  'use strict';
  function key(binding) {
    if (!binding || binding.verified !== true || binding.layout_verified !== true || typeof binding.account_key !== 'string' || typeof binding.person_key !== 'string' || typeof binding.message_key !== 'string' || !Number.isInteger(binding.body_revision) || typeof binding.body_sha256 !== 'string') return null;
    return JSON.stringify([binding.account_key,binding.person_key,binding.message_key,binding.body_revision,binding.body_sha256]);
  }
  function ignoredMutation(mutation) {
    if (Chat.ownNode(mutation.target)) return true;
    const changes=[...(mutation.addedNodes || []),...(mutation.removedNodes || [])];
    return changes.length > 0 && changes.every(Chat.ownNode);
  }
  class AnnotationBinder {
    constructor(document,{readBinding = null,evidence = null} = {}) {
      this.document=document; this.readBinding=evidence && evidence.verified === true && readBinding ? readBinding : null;
      this.bindings=new Map(); this.generations=new WeakMap(); this.generation=0;
    }
    capability() {return {state:this.readBinding ? 'fixture_binding_available' : 'unavailable',real_dom_verified:false};}
    detach(node) {const record=this.bindings.get(node); if (record) {record.host.remove(); this.bindings.delete(node);} this.generations.set(node,(this.generations.get(node) || 0)+1);}
    refresh(node) {const record=this.bindings.get(node); if (record && (!this.readBinding || key(this.readBinding(node))!==record.key)) this.detach(node);}
    hide(node) {this.detach(node);}
    dispose() {this.generation++; for (const node of [...this.bindings.keys()]) this.detach(node);}
    async schedule(node,promise) {
      if (!this.readBinding || Chat.ownNode(node)) return {state:'unavailable'};
      const stamp=key(this.readBinding(node)); if (!stamp) return {state:'pending'};
      const local=this.generations.get(node) || 0; const global=this.generation;
      const result=await promise;
      if (global!==this.generation || local!==(this.generations.get(node) || 0) || stamp!==key(this.readBinding(node))) return {state:'stale'};
      return this.attach(node,result);
    }
    attach(node,annotation) {
      if (!this.readBinding || Chat.ownNode(node)) return {state:'unavailable'};
      const binding=this.readBinding(node); const stamp=key(binding); this.refresh(node);
      if (!stamp || !annotation || !['ready','corrected','unknown'].includes(annotation.state) || annotation.account_key!==binding.account_key || annotation.person_key!==binding.person_key) {this.detach(node); return {state:'pending'};}
      const emotion=annotation.result && annotation.result.emotion;
      if (!emotion) {this.detach(node); return {state:'unknown'};}
      if (typeof emotion.label !== 'string' || emotion.label.length>24 || !Array.isArray(emotion.refs) || !emotion.refs.some(reference => reference.message_key===binding.message_key && reference.body_revision===binding.body_revision)) {this.detach(node); return {state:'stale'};}
      this.detach(node);
      const host=this.document.createElement('span'); host.setAttribute('data-dsh-owned','true'); host.setAttribute('data-dsh-annotation','true');
      const shadow=host.attachShadow({mode:'closed'}); const style=this.document.createElement('style');
      style.textContent=':host{display:block;font:12px system-ui;margin-top:4px;color:#607c75}.label{background:#edf3ef;border-radius:5px;padding:2px 6px}details{margin-top:4px;max-width:260px}summary{cursor:pointer}p{white-space:pre-wrap;overflow-wrap:anywhere;margin:4px 0}';
      const label=this.document.createElement('span'); label.className='label'; label.textContent=emotion.label;
      const details=this.document.createElement('details'); const summary=this.document.createElement('summary'); summary.textContent='原文引用'; details.append(summary);
      for (const reference of emotion.refs) if (reference.message_key===binding.message_key && reference.body_revision===binding.body_revision && typeof reference.quote==='string' && reference.quote.length<=120) {const quote=this.document.createElement('p'); quote.textContent=reference.quote; details.append(quote);}
      shadow.append(style,label,details); node.append(host); this.bindings.set(node,{host,key:stamp}); return {state:'attached'};
    }
  }
  // Not loaded by the manifest. Real source/layout proof is absent; no observer installed.
  return {AnnotationBinder,ignoredMutation};
});
