(function (root, factory) {
  const guard = typeof module === 'object' && module.exports ? require('../identity/context_guard.js') : root.DSHIdentityContext;
  const value = factory(guard);
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHChat = value;
})(globalThis, function (identity) {
  'use strict';
  function ownNode(node) {
    for (let current = node; current; current = current.parentNode || current.host) {
      if (current.getAttribute && current.getAttribute('data-dsh-owned') === 'true') return true;
    }
    return false;
  }
  function readBody(node) {
    if (!node || ownNode(node)) return '';
    if (node.nodeType === 3) return node.nodeValue || '';
    if (node.nodeType !== 1 && node.nodeType !== 11) return '';
    if (node.matches && node.matches('input,textarea,[contenteditable],script,style')) return '';
    return Array.from(node.childNodes || []).map(readBody).join('');
  }
  class UnverifiedChatAdapter {
    capability() { return {state:'unavailable',verified:false,source_id:'douyin-dom-unverified',source_version:'A1',coverage:'unknown',reason:'stable_binding_not_verified'}; }
    context() { return null; }
    observations() { return []; }
  }
  // Adapter interface only: real deployment has no provenance-qualified binding.
  class BoundAdapter {
    constructor(binding) {
      if (!binding || !binding.evidence || typeof binding.authorize !== 'function' || typeof binding.context !== 'function' || typeof binding.read !== 'function' || typeof binding.nodeKey !== 'function' ||
          !['provider_namespace','source_id','source_version'].every(key => typeof binding.evidence[key] === 'string' && binding.evidence[key].length > 0 && binding.evidence[key].length <= 512)) throw new Error('binding_unverified');
      this.binding = Object.freeze({authorize:binding.authorize,context:binding.context,read:binding.read,nodeKey:binding.nodeKey,evidence:Object.freeze({...binding.evidence})});
      Object.freeze(this);
    }
    async capture(nodes, contextManager, emit) {
      if (!identity || !(contextManager instanceof identity.ContextGuard) || typeof emit !== 'function' || !Array.isArray(nodes) || nodes.length > 2000) throw new Error('identity_guard_required');
      const stamp = contextManager.snapshot();
      if (!stamp.value) return {state:'pending',emitted:0,emit_attempted:0,durable_state_unknown:false};
      let emitted = 0, emitAttempted = 0, durableStateUnknown = false;
      const check = () => {
        try {
          contextManager.observe(this.binding.context());
          if (!contextManager.current(stamp)) return false;
          const value = stamp.value;
          if (!['provider_namespace','source_id','source_version'].every(key => this.binding.evidence[key] === value[key]) || this.binding.authorize(value) !== true || !contextManager.current(stamp)) {
            contextManager.invalidate('binding_approval_required'); return false;
          }
          return true;
        } catch { contextManager.invalidate('binding_context_failed'); return false; }
      };
      for (const node of nodes) {
        if (!check()) break;
        if (ownNode(node) || (node && node.matches && node.matches('input,textarea,[contenteditable],script,style'))) continue;
        let nodeKey, result;
        try {
          nodeKey = this.binding.nodeKey(node);
          if (typeof nodeKey !== 'string' || !nodeKey || nodeKey.length > 512) throw new Error('invalid_node_key');
          if (!check()) break;
          result = await this.binding.read(node, {body:readBody(node),context:stamp.value,proof:stamp});
        } catch { contextManager.invalidate('binding_read_failed'); break; }
        if (!check()) break;
        let stable = false;
        try { stable = !ownNode(node) && this.binding.nodeKey(node) === nodeKey; } catch {}
        if (!stable) { contextManager.invalidate('node_reused'); break; }
        if (result) {
          let fields, valid = false;
          try {
            fields = Object.getOwnPropertyDescriptors(result);
            valid = Object.getPrototypeOf(result) === Object.prototype && Reflect.ownKeys(fields).length === 2 &&
              fields.scope && Object.hasOwn(fields.scope,'value') && fields.scope['value'] === stamp.value &&
              fields.observation && Object.hasOwn(fields.observation,'value') && this.matches(fields.observation.value, stamp.value);
          } catch {}
          if (!valid) {
            contextManager.invalidate('result_scope_conflict'); break;
          }
          // Consumers receive the live proof and must perform their own commit CAS.
          // No after-await check can undo an already persisted emission.
          if (!check()) break;
          try {
            if (this.binding.nodeKey(node) !== nodeKey) { contextManager.invalidate('node_reused'); break; }
          } catch { contextManager.invalidate('node_reused'); break; }
          if (!contextManager.current(stamp)) break;
          emitAttempted++;
          try { await emit(fields.observation.value, stamp); emitted++; }
          catch { durableStateUnknown = true; contextManager.invalidate('emit_failed'); break; }
          if (!check()) break;
        }
      }
      return {state:contextManager.current(stamp) ? 'partial' : 'stale',emitted,emit_attempted:emitAttempted,durable_state_unknown:durableStateUnknown};
    }
    matches(a,b) {
      if (!a || Object.getPrototypeOf(a) !== Object.prototype) return false;
      const fields = Object.getOwnPropertyDescriptors(a);
      return Reflect.ownKeys(fields).every(key => Object.hasOwn(fields[key],'value')) &&
        ['account_key','platform_conversation_key','identity_kind','identity_value','object_kind','source_id','source_version'].every(key => fields[key] && fields[key]['value'] === b[key]) &&
        identity.FIELDS.every(key => !fields[key] || fields[key]['value'] === b[key]);
    }
  }
  return {ownNode,readBody,UnverifiedChatAdapter,BoundAdapter};
});
