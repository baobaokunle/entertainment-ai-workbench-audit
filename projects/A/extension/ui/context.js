(function (root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHContext = value;
})(globalThis, function () {
  'use strict';
  class Context {
    constructor() { this.generation = 0; this.value = null; }
    change(value) { this.generation++; this.value = value ? Object.freeze({...value}) : null; return this.snapshot(); }
    snapshot() { return {generation:this.generation,value:this.value}; }
    current(stamp) { return !!stamp && stamp.generation === this.generation && stamp.value === this.value; }
  }
  class SidebarController {
    constructor(request, render) { this.request = request; this.render = render; this.context = new Context(); this.requestGeneration = 0; }
    async select(value) {
      const stamp = this.context.change(value); const requestId = ++this.requestGeneration;
      if (!value || value.verified !== true) { this.render({state:'pending',message:'页面身份和消息来源尚未核验，不能自动挂载档案。'}); return; }
      this.render({state:'loading'});
      try {
        const data = await this.request({account_key:value.account_key,person_key:value.person_key});
        if (this.context.current(stamp) && requestId === this.requestGeneration) this.render({state:'ready',data});
      } catch {
        if (this.context.current(stamp) && requestId === this.requestGeneration) this.render({state:'unavailable',message:'本机服务暂不可用。'});
      }
    }
    dispose() { this.context.change(null); this.requestGeneration++; }
  }
  return {Context,SidebarController};
});
