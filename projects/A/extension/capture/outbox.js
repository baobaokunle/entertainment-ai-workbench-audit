(function (root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHOutbox = value;
})(globalThis, function () {
  'use strict';
  function canonical(value) {
    if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
    if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
    return JSON.stringify(value);
  }
  class Outbox {
    constructor(storage, {maxItems = 500, maxBytes = 2 * 1024 * 1024, now = Date.now} = {}) {
      this.storage = storage; this.maxItems = maxItems; this.maxBytes = maxBytes; this.now = now; this.serial = Promise.resolve();
    }
    exclusive(fn) { const task = this.serial.then(fn); this.serial = task.catch(() => {}); return task; }
    async read() {
      const value = await this.storage.read();
      if (value === undefined) return [];
      if (!Array.isArray(value) || value.some(x => !x || typeof x.id !== 'string' || !['waiting','pending','rejected'].includes(x.state) || !x.owner || typeof x.owner.operator_key !== 'string' || typeof x.owner.device_key !== 'string' || !x.observation || !Number.isInteger(x.attempts) || !Number.isFinite(x.nextAttempt))) throw new Error('outbox_corrupt');
      return value;
    }
    async write(items) {
      if (items.length > this.maxItems || new TextEncoder().encode(JSON.stringify(items)).length > this.maxBytes) throw new Error('outbox_full');
      await this.storage.write(items);
    }
    enqueue(observation, owner) {
      return this.exclusive(async () => {
        const items = await this.read(); const id = observation.observation_id;
        const previous = items.find(x => x.id === id);
        if (previous) {
          if (canonical(previous.observation) !== canonical(observation) || previous.owner.operator_key !== owner.operator_key || previous.owner.device_key !== owner.device_key) throw new Error('observation_id_conflict');
          return {state:previous.state, queued:true};
        }
        items.push({id, observation:JSON.parse(JSON.stringify(observation)), owner:{operator_key:owner.operator_key,device_key:owner.device_key}, state:'waiting', attempts:0, nextAttempt:0});
        await this.write(items); return {state:'waiting',queued:true};
      });
    }
    stats(owner) {
      return this.exclusive(async () => {
        const result = {waiting:0,pending:0,rejected:0,other_session:0};
        for (const item of await this.read()) {
          if (!owner || item.owner.operator_key !== owner.operator_key || item.owner.device_key !== owner.device_key) result.other_session++;
          else result[item.state]++;
        }
        return result;
      });
    }
    flush(send, owner, {force = false, limit = 10, stillAuthorized = () => true} = {}) {
      return this.exclusive(async () => {
        const items = await this.read(); let count = 0;
        for (const item of [...items]) {
          if (count >= limit || !stillAuthorized()) break;
          if (item.state !== 'waiting' || item.owner.operator_key !== owner.operator_key || item.owner.device_key !== owner.device_key || !owner.account_keys.includes(item.observation.account_key) || (!force && item.nextAttempt > this.now())) continue;
          count++; item.attempts++;
          try {
            const result = await send(item.observation);
            if (result && result.committed === true && ['accepted','duplicate'].includes(result.status)) items.splice(items.indexOf(item), 1);
            else if (result && ['pending','rejected'].includes(result.status)) { item.state = result.status; item.reason = typeof result.code === 'string' ? result.code.slice(0,80) : 'requires_review'; }
            else item.nextAttempt = this.now() + this.backoff(item.attempts);
          } catch { item.nextAttempt = this.now() + this.backoff(item.attempts); }
          await this.write(items);
        }
        return {attempted:count};
      });
    }
    backoff(attempts) { return Math.min(300000, 1000 * 2 ** Math.min(attempts, 9)); }
  }
  return {Outbox};
});
