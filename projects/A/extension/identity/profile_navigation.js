(function (root, factory) {
  const value = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHProfileNavigation = value;
})(globalThis, function (root) {
  'use strict';
  const SCOPE_FIELDS = Object.freeze(['account_key','account_namespace','owner_state','owner_provider_namespace','owner_identity_kind','owner_identity_value','provider_namespace','source_id','source_version','opener_tab_id','opener_document_id','route_key','document_generation','selection_generation','head_generation','selection_token','head_token']);
  const GENERATIONS = ['document_generation','selection_generation','head_generation'];
  const OWNER_FIELDS = ['owner_provider_namespace','owner_identity_kind','owner_identity_value'];
  const CREATED_FIELDS = ['attempt_id','opener_tab_id','opener_document_id','target_tab_id','target_document_id','new_tab'];
  const NAVIGATED_FIELDS = ['attempt_id','opener_tab_id','opener_document_id','target_tab_id','target_document_id','url'];
  const REPLACED_FIELDS = ['attempt_id','opener_tab_id','opener_document_id','target_tab_id','previous_document_id','target_document_id','previous_url','url'];
  function data(value, keys) {
    if (!value || Object.getPrototypeOf(value) !== Object.prototype) throw new Error('profile_navigation_invalid');
    const fields = Object.getOwnPropertyDescriptors(value);
    if (Reflect.ownKeys(fields).length !== keys.length || keys.some(key => !fields[key] || !Object.hasOwn(fields[key],'value'))) throw new Error('profile_navigation_invalid');
    return Object.fromEntries(keys.map(key => [key,fields[key].value]));
  }
  function text(value, maximum = 256) {
    if (typeof value !== 'string' || !value || value.length > maximum || value.trim() !== value || /[\u0000-\u001f\u007f]/u.test(value)) throw new Error('profile_navigation_invalid');
    return value;
  }
  function normalizeScope(input) {
    const scope = data(input,SCOPE_FIELDS);
    for (const key of SCOPE_FIELDS) {
      if (GENERATIONS.includes(key)) {
        if (!Number.isSafeInteger(scope[key]) || scope[key] < 0) throw new Error('profile_navigation_invalid');
      } else if (!OWNER_FIELDS.includes(key)) text(scope[key]);
    }
    if (scope.route_key !== '/chat') throw new Error('profile_navigation_invalid');
    if (scope.owner_state === 'unknown') {
      if (!OWNER_FIELDS.every(key => scope[key] === null)) throw new Error('profile_navigation_invalid');
    } else if (scope.owner_state === 'observed') {
      OWNER_FIELDS.forEach(key => text(scope[key]));
      if (!['uid','platform_uid','sec_uid','im_uid','live_uid','douyin_id'].includes(scope.owner_identity_kind)) throw new Error('profile_navigation_invalid');
      if (['uid','platform_uid'].includes(scope.owner_identity_kind) && !/^[0-9]+$/.test(scope.owner_identity_value)) throw new Error('profile_navigation_invalid');
    } else throw new Error('profile_navigation_invalid');
    return Object.freeze(scope);
  }
  function sameScope(a,b) { return !!a && !!b && SCOPE_FIELDS.every(key => a[key] === b[key]); }
  function parseProfileURL(raw) {
    text(raw,2048);
    // Check the original spelling before URL's dot-path/escape normalization.
    const match = /^https:\/\/www\.douyin\.com\/user\/([A-Za-z0-9_-]{1,256})(?:\?([^#]*))?$/.exec(raw);
    if (!match || match[1].toLowerCase() === 'self' || (match[2] && match[2].includes('%'))) throw new Error('profile_route_invalid');
    const url = new URL(raw);
    const names = [];
    for (const [key,value] of url.searchParams) {
      if (!['from_tab_name','enter_from','enter_method'].includes(key) || names.includes(key) || !/^[A-Za-z0-9_-]{1,128}$/.test(value)) throw new Error('profile_route_invalid');
      names.push(key);
    }
    return Object.freeze({identity_kind:'sec_uid',identity_value:match[1]});
  }
  function eventData(input, fields) {
    const event = data(input,fields);
    for (const key of fields) {
      if (key === 'new_tab') { if (event[key] !== true) throw new Error('profile_navigation_invalid'); }
      else text(event[key],key === 'url' ? 2048 : 256);
    }
    return Object.freeze(event);
  }
  function randomId() {
    if (root.crypto && typeof root.crypto.randomUUID === 'function') return root.crypto.randomUUID();
    if (typeof require === 'function') return require('node:crypto').randomUUID();
    throw new Error('profile_random_unavailable');
  }
  // Host callbacks are trusted synchronous dependencies. No tab creation, click,
  // DOM inspection, network, timer, storage, or business sink exists in this module.
  class ProfileNavigationCoordinator {
    #read; #approve; #clock; #ttl; #last = -Infinity; #epoch = 0; #session = randomId(); #attempt = null; #starting = false; #checking = false;
    constructor(options = {}) {
      if (!options || Object.getPrototypeOf(options) !== Object.prototype) throw new Error('profile_configuration_invalid');
      const fields = Object.getOwnPropertyDescriptors(options);
      if (Reflect.ownKeys(fields).some(key => !['readCurrent','approve','clock','ttlMs'].includes(key) || !Object.hasOwn(fields[key],'value'))) throw new Error('profile_configuration_invalid');
      const readCurrent = fields.readCurrent && fields.readCurrent.value;
      const approve = fields.approve && fields.approve.value;
      const clock = fields.clock ? fields.clock.value : () => root.performance.now();
      const ttlMs = fields.ttlMs ? fields.ttlMs.value : 10000;
      if (typeof readCurrent !== 'function' || typeof approve !== 'function' || typeof clock !== 'function' || typeof ttlMs !== 'number' || !Number.isFinite(ttlMs) || ttlMs <= 0 || ttlMs > 30000) throw new Error('profile_configuration_invalid');
      this.#read = readCurrent; this.#approve = approve; this.#clock = clock; this.#ttl = ttlMs;
    }
    #fail(reason) {
      this.#epoch++;
      if (this.#attempt) { this.#attempt.state = 'failed'; this.#attempt.reason = reason; this.#attempt.candidate = null; }
      return null;
    }
    #now() {
      let now; try { now = this.#clock(); } catch { return this.#fail('clock_invalid'); }
      if (typeof now !== 'number' || !Number.isFinite(now) || now < this.#last) return this.#fail('clock_invalid');
      this.#last = now; return now;
    }
    #readScope() { try { return normalizeScope(this.#read()); } catch { this.#fail('scope_invalid'); return null; } }
    #check(proof,phase,event = null) {
      if (this.#checking) { this.#fail('reentrant_callback'); return false; }
      this.#checking = true;
      try { return this.#checkCore(proof,phase,event); } finally { this.#checking = false; }
    }
    #checkCore(proof,phase,event) {
      const attempt = this.#attempt;
      if (!attempt || proof !== attempt.proof || attempt.state === 'failed') return false;
      let now = this.#now();
      if (now === null || now >= proof.expires_at) { if (now !== null) this.#fail('expired'); return false; }
      const before = this.#readScope();
      if (!before || !sameScope(before,proof.scope) || this.#attempt !== attempt || attempt.state === 'failed') { if (before) this.#fail('scope_changed'); return false; }
      let approved = false;
      try { approved = this.#approve(Object.freeze({phase,scope:proof.scope,attempt:proof,event})) === true; } catch {}
      if (!approved) { this.#fail('approval_required'); return false; }
      const after = this.#readScope(); now = this.#now();
      if (!after || now === null || this.#attempt !== attempt || attempt.state === 'failed' || !sameScope(after,proof.scope) || now >= proof.expires_at) {
        if (attempt.state !== 'failed') this.#fail(now !== null && now >= proof.expires_at ? 'expired' : 'scope_changed');
        return false;
      }
      return true;
    }
    begin() {
      if (this.#starting || this.#checking) return this.#fail('reentrant_callback');
      if (this.#attempt && this.#attempt.state !== 'failed') return this.#fail('concurrent_attempt');
      this.#starting = true;
      try {
        const epoch = this.#epoch;
        const now = this.#now(); if (now === null || epoch !== this.#epoch) return null;
        const scope = this.#readScope(); if (!scope || epoch !== this.#epoch) return null;
        const proof = Object.freeze({session:this.#session,attempt_id:randomId(),generation:++this.#epoch,scope,created_at:now,expires_at:now + this.#ttl});
        this.#attempt = {proof,state:'pending_navigation',reason:null,target:null,candidate:null};
        return this.#check(proof,'begin') ? proof : null;
      } finally { this.#starting = false; }
    }
    #related(event,proof) {
      return event.attempt_id === proof.attempt_id && event.opener_tab_id === proof.scope.opener_tab_id && event.opener_document_id === proof.scope.opener_document_id &&
        event.target_tab_id !== proof.scope.opener_tab_id && event.target_document_id !== proof.scope.opener_document_id;
    }
    created(proof,input) {
      if (!this.#check(proof,'current')) return false;
      let event; try { event = eventData(input,CREATED_FIELDS); } catch { this.#fail('event_invalid'); return false; }
      if (!this.#related(event,proof)) { this.#fail('event_unrelated'); return false; }
      const attempt = this.#attempt;
      if (attempt.target && (attempt.target.tab_id !== event.target_tab_id || attempt.target.document_id !== event.target_document_id)) { this.#fail('multiple_targets'); return false; }
      if (!this.#check(proof,'created',event)) return false;
      attempt.target = Object.freeze({tab_id:event.target_tab_id,document_id:event.target_document_id});
      if (!attempt.candidate) attempt.state = 'pending_profile';
      return true;
    }
    navigated(proof,input) {
      if (!this.#check(proof,'current')) return false;
      let event, identity;
      try { event = eventData(input,NAVIGATED_FIELDS); identity = parseProfileURL(event.url); } catch { this.#fail('route_or_event_invalid'); return false; }
      const attempt = this.#attempt;
      if (!this.#related(event,proof) || !attempt.target || attempt.target.tab_id !== event.target_tab_id || attempt.target.document_id !== event.target_document_id) { this.#fail('event_unrelated'); return false; }
      if (attempt.candidate && attempt.candidate.identity_value !== identity.identity_value) { this.#fail('profile_redirect_conflict'); return false; }
      if (!this.#check(proof,'navigated',event)) return false;
      attempt.candidate = Object.freeze({state:'pending',verified:false,object_kind:'person',subject_role:'profile_subject',...identity,
        source_kind:'profile_route',source_permission:'not_assessed',account_binding:'unverified',context_binding:'unverified',relationship:'unknown',
        attempt_id:proof.attempt_id,generation:proof.generation,scope:proof.scope,target:attempt.target});
      attempt.state = 'candidate'; return true;
    }
    // Only a trusted host may attest the initial about:blank commit lineage.
    // Do not relax navigated() or synthesize a stable document ID.
    replaced(proof,input) {
      if (!this.#check(proof,'current')) return false;
      let event;
      try { event = eventData(input,REPLACED_FIELDS); parseProfileURL(event.url); }
      catch { this.#fail('replacement_invalid'); return false; }
      const attempt = this.#attempt;
      if (!this.#related(event,proof) || attempt.state !== 'pending_profile' || attempt.candidate || !attempt.target ||
          event.previous_url !== 'about:blank' || event.previous_document_id !== attempt.target.document_id ||
          event.target_tab_id !== attempt.target.tab_id || event.target_document_id === event.previous_document_id) {
        this.#fail('replacement_unrelated'); return false;
      }
      if (!this.#check(proof,'replaced',event)) return false;
      attempt.target = Object.freeze({tab_id:event.target_tab_id,document_id:event.target_document_id});
      return true;
    }
    candidate(proof) { return this.#check(proof,'candidate') ? this.#attempt.candidate : null; }
    current(proof) { return this.#check(proof,'current'); }
    cancel(proof) { if (this.#attempt && proof === this.#attempt.proof) this.#fail('cancelled'); return null; }
    invalidate() { return this.#fail('invalidated'); }
    diagnosticStatus() {
      // Pure snapshot: unlike status(), never call clock, scope or approval.
      const states=['idle','pending_navigation','pending_profile','candidate','failed'];
      const reasons=['clock_invalid','scope_invalid','reentrant_callback','expired','scope_changed','approval_required','concurrent_attempt','event_invalid','event_unrelated','multiple_targets','route_or_event_invalid','profile_redirect_conflict','replacement_invalid','replacement_unrelated','cancelled','invalidated'];
      const state=this.#attempt?this.#attempt.state:'idle',reason=this.#attempt?this.#attempt.reason:null;
      return Object.freeze({state:states.includes(state)?state:'idle',reason:reasons.includes(reason)?reason:null});
    }
    status() {
      if (this.#attempt && this.#attempt.state !== 'failed') this.#check(this.#attempt.proof,'current');
      return Object.freeze({state:this.#attempt ? this.#attempt.state : 'idle',reason:this.#attempt ? this.#attempt.reason : null,verified:false});
    }
  }
  return {ProfileNavigationCoordinator,normalizeScope,parseProfileURL,SCOPE_FIELDS};
});
