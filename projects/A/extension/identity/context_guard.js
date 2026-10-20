(function (root, factory) {
  const value = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHIdentityContext = value;
})(globalThis, function (root) {
  'use strict';
  const FIELDS = Object.freeze(['account_key','account_namespace','owner_provider_namespace','owner_identity_kind','owner_identity_value','tab_id','platform_conversation_key','object_kind','identity_kind','identity_value','subject_role','provider_namespace','source_id','source_version','object_token','route_key','state']);
  const KINDS = new Set(['uid','sec_uid','platform_uid','douyin_id','im_uid','live_uid','group_id']);
  function normalize(dto) {
    if (!dto || Object.getPrototypeOf(dto) !== Object.prototype) throw new Error('identity_incomplete');
    const props = Object.getOwnPropertyDescriptors(dto);
    if (Reflect.ownKeys(props).length !== FIELDS.length || FIELDS.some(key => !props[key] || !Object.hasOwn(props[key], 'value'))) throw new Error('identity_incomplete');
    const value = {};
    for (const key of FIELDS) {
      const field = props[key].value;
      if (typeof field !== 'string' || !field || field.length > 512 || field.trim() !== field || /[\u0000-\u001f\u007f]/u.test(field)) throw new Error('identity_incomplete');
      value[key] = field;
    }
    if (!KINDS.has(value.identity_kind) || !KINDS.has(value.owner_identity_kind) || value.owner_identity_kind === 'group_id' ||
        (['uid','platform_uid'].includes(value.identity_kind) && !/^[0-9]+$/.test(value.identity_value)) ||
        (['uid','platform_uid'].includes(value.owner_identity_kind) && !/^[0-9]+$/.test(value.owner_identity_value)) ||
        !['pending','eligible'].includes(value.state) || !['/chat','/anchor/dashboard/home'].includes(value.route_key) ||
        (value.route_key === '/chat' && !['conversation_peer','group'].includes(value.subject_role)) ||
        (value.route_key === '/anchor/dashboard/home' && (value.object_kind !== 'person' || value.subject_role !== 'anchor_subject')) ||
        (value.object_kind === 'person' ? !['conversation_peer','anchor_subject'].includes(value.subject_role) || value.identity_kind === 'group_id' :
          value.object_kind !== 'group' || value.subject_role !== 'group' || value.identity_kind !== 'group_id')) throw new Error('identity_conflict');
    return Object.freeze(value);
  }
  function same(a, b) { return !!a && !!b && FIELDS.every(key => a[key] === b[key]); }
  function sessionId() {
    if (root.crypto && typeof root.crypto.randomUUID === 'function') return root.crypto.randomUUID();
    if (typeof require === 'function') return require('node:crypto').randomUUID();
    throw new Error('identity_random_unavailable');
  }
  // authorize is a trusted host dependency, never a property of the page DTO.
  // This guard has no DOM reader, timers, storage, or source activation capability.
  class ContextGuard {
    #clock; #authorize; #ttl; #last = -Infinity; #candidate = null; #stamp = null;
    #generation = 0; #session = sessionId(); #reason = 'unobserved';
    constructor({clock = () => root.performance.now(), authorize, ttlMs = 30000} = {}) {
      if (typeof clock !== 'function' || typeof authorize !== 'function' || typeof ttlMs !== 'number' || !Number.isFinite(ttlMs) || ttlMs <= 0 || ttlMs > 120000) throw new Error('identity_configuration');
      this.#clock = clock; this.#authorize = authorize; this.#ttl = ttlMs;
    }
    #now() {
      let now;
      try { now = this.#clock(); } catch { this.invalidate('clock_invalid'); return null; }
      if (typeof now !== 'number' || !Number.isFinite(now) || now < this.#last) { this.invalidate('clock_invalid'); return null; }
      this.#last = now; return now;
    }
    invalidate(reason = 'explicit_invalidation') {
      this.#generation++; this.#candidate = null; this.#stamp = null;
      this.#reason = typeof reason === 'string' && reason.length <= 80 ? reason : 'explicit_invalidation';
      return this.snapshot();
    }
    observe(dto) {
      let candidate;
      try { candidate = normalize(dto); } catch { return this.invalidate('identity_incomplete_or_conflict'); }
      if (!same(candidate, this.#candidate)) { this.#generation++; this.#stamp = null; this.#candidate = candidate; this.#reason = 'pending'; }
      if (this.#stamp) this.current(this.#stamp);
      return this.candidateSnapshot();
    }
    candidateSnapshot() { return Object.freeze({session:this.#session,generation:this.#generation,value:this.#candidate}); }
    lock(candidateStamp) {
      if (!candidateStamp || candidateStamp.session !== this.#session || candidateStamp.generation !== this.#generation || candidateStamp.value !== this.#candidate || !this.#candidate || this.#candidate.state !== 'eligible') return null;
      // A repeated lock never renews the deadline.
      if (this.#stamp) return this.current(this.#stamp) ? this.#stamp : null;
      const now = this.#now(); if (now === null) return null;
      const candidate = this.#candidate; const generation = this.#generation;
      let approved = false; try { approved = this.#authorize(candidate) === true; } catch {}
      if (!approved || candidate !== this.#candidate || generation !== this.#generation) { this.invalidate('approval_required'); return null; }
      this.#reason = 'locked';
      this.#stamp = Object.freeze({session:this.#session,generation:this.#generation,value:Object.freeze({...candidate,verified:true}),expires_at:now + this.#ttl});
      return this.#stamp;
    }
    current(stamp) {
      if (!stamp || stamp !== this.#stamp) return false;
      const now = this.#now(); if (now === null) return false;
      if (now >= stamp.expires_at) { this.invalidate('expired'); return false; }
      let approved = false; try { approved = this.#authorize(this.#candidate) === true; } catch {}
      if (!approved) { this.invalidate('approval_revoked'); return false; }
      return this.#stamp === stamp;
    }
    snapshot() {
      if (this.#stamp && this.current(this.#stamp)) return this.#stamp;
      return Object.freeze({session:this.#session,generation:this.#generation,value:null});
    }
    status() { const stamp = this.snapshot(); return Object.freeze({state:stamp.value ? 'locked' : 'pending',generation:this.#generation,reason:this.#reason}); }
  }
  return {ContextGuard,normalize,same,FIELDS};
});
