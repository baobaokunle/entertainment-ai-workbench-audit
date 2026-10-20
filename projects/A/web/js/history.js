(function (root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHHistory = value;
})(globalThis, function () {
  'use strict';
  class HistoryController {
    constructor(request,render) { this.request = request; this.render = render; this.generation = 0; this.state = {state:'empty',items:[],next_cursor:null,query:null,busy:false}; }
    publish(patch) { this.state = {...this.state,...patch}; this.render({...this.state,items:[...this.state.items]}); }
    select(query) {
      this.generation++; this.publish({query:{...query,limit:50},items:[],next_cursor:null,busy:false,state:'loading'}); return this.load(false);
    }
    async load(more = true) {
      if (!this.state.query || this.state.busy || (more && !this.state.next_cursor)) return;
      const stamp = this.generation; const query = {...this.state.query};
      if (more) query.cursor = this.state.next_cursor;
      this.publish({busy:true,state:'loading'});
      try {
        const result = await this.request(query);
        if (stamp !== this.generation) return;
        if (!result || !Array.isArray(result.items)) throw new Error('bad_response');
        let cursor = result.next_cursor;
        if (cursor && typeof cursor === 'object' && !Array.isArray(cursor) &&
            (Object.keys(cursor).length === 2 || Object.keys(cursor).length === 3 && typeof cursor.scope === 'string' && /^[a-f0-9]{64}$/.test(cursor.scope)) &&
            cursor.account_key === query.account_key && typeof cursor.message_key === 'string') cursor = JSON.stringify(cursor);
        if (cursor !== null && cursor !== undefined && typeof cursor !== 'string') throw new Error('bad_response');
        const items = more ? [...this.state.items,...result.items] : [...result.items];
        this.publish({items,next_cursor:cursor || null,busy:false,state:items.length ? 'ready' : 'empty'});
      } catch (error) {
        if (stamp !== this.generation) return;
        if (['unauthorized','forbidden','stale_session'].includes(error.message)) {
          this.generation++;
          this.publish({query:null,items:[],next_cursor:null,busy:false,state:error.message === 'forbidden' ? 'forbidden' : 'unauthorized'});
        } else this.publish({busy:false,state:'unavailable'});
      }
    }
    clear() { this.generation++; this.publish({query:null,items:[],next_cursor:null,busy:false,state:'empty'}); }
  }
  function messageLabel(item) {
    const direction = {in:'收到',out:'账号出站',system:'系统',unknown:'方向未知'}[item.direction] || '方向未知';
    const precision = {second:'秒',minute:'分钟',date:'日期'}[item.time_precision];
    const lower = item.occurred_lower_utc, upper = item.occurred_upper_utc;
    const hasBounds = typeof lower === 'string' && typeof upper === 'string' &&
      Number.isFinite(Date.parse(lower)) && Number.isFinite(Date.parse(upper)) && Date.parse(lower) <= Date.parse(upper);
    // Preserve source precision and both bounds, including sub-millisecond ranges.
    // Missing time is never replaced with collection time or a guessed instant.
    const time = !precision || !hasBounds ? '发送时间未知' : lower === upper ?
      `${lower}（${precision}精度）` : `${lower} 至 ${upper}（${precision}精度范围）`;
    const success = {confirmed:'平台成功已核验',pending:'平台发送中',failed:'平台发送失败',unknown:'平台状态未知'}[item.success_state] || '平台状态未知';
    return `${direction} · ${time} · ${success} · ${item.actor_state === 'attributed' && item.actor_key ? '已核验操作者：' + item.actor_key : '操作者未知'}`;
  }
  return {HistoryController,messageLabel};
});
