(function (root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHCD = value;
})(globalThis, function () {
  'use strict';
  const TAGS = Object.freeze({wechat:'微信主联系',maintenance:'日常维护',spark:'火花维护',clip:'剪辑支持',vip:'VIP会员',star:'星守护',local_time:'显示当地时间'});
  const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  function roleLabel(role) { return role === 'manager' ? '管理' : role === 'baobao' ? '饱饱' : '已认证操作者'; }
  function contactLabel(item) { return `${item.display_name && item.display_name !== item.person_key ? item.display_name : '未命名对象'} · ${item.object_kind === 'group' ? '群' : '个人'} · ${item.verified === true ? '档案身份已核验' : '档案身份待核验'}`; }
  function fieldLabel(field) {
    if (field.startsWith('free_term.')) return '自由词条';
    if (field.startsWith('entitlement.vip.')) return 'VIP独立期限';
    if (field.startsWith('entitlement.star.')) return '星守护独立期限';
    if (field.startsWith('formal_tag.')) return '正式 · ' + (TAGS[field.slice(11)] || '未知标签');
    return {display_name:'档案显示名称',note:'共享备注',location:'国家／城市／时区',birthday:'本人生日依据',anniversary:'聊天周年依据',reminder_settings:'独立提前提醒天数'}[field] || '未知资料';
  }
  function fieldType(field) {
    if (['display_name','note'].includes(field) || /^free_term\.[a-zA-Z0-9_-]{1,100}$/.test(field)) return 'text';
    if (field.startsWith('formal_tag.') && Object.prototype.hasOwnProperty.call(TAGS,field.slice(11))) return 'boolean';
    if (['location','birthday','anniversary','reminder_settings'].includes(field) || /^entitlement\.(vip|star)\.[a-zA-Z0-9_-]{1,100}$/.test(field)) return 'object';
    throw new Error('unsupported_field');
  }
  function stateLabel(state) { return {empty:'未记录',unknown:'未知',unverified:'待核验',pending:'待核验',conflict:'有冲突，需人工核对',cleared:'已明确清空',clear:'已明确清空',set:'已记录',known:'已记录',ready:'已保存',active:'已记录',effective:'已记录',expired:'已到期',not_enabled:'未启用',unavailable:'不可用'}[state] || '状态待核验'; }
  function fieldValue(field,record) {
    const value = record && (Object.prototype.hasOwnProperty.call(record,'effective_value') ? record.effective_value : record.value);
    if (value === null || value === undefined) return '未知／未记录';
    if (typeof value === 'boolean') return value ? '是' : '否（已明确记录）';
    if (typeof value === 'string') return value || '已明确清空';
    if (field === 'location') return [value.country,value.city,value.timezone].filter(Boolean).join(' · ') || '地点或时区未记录';
    if (field === 'birthday') return `${value.month || '?'}月${value.day || '?'}日 · ${{gregorian:'公历',lunar:'农历',lunar_leap:'农历闰月，待核对'}[value.calendar] || '历法未知'} · ${{self:'本人',other:'其他人物',unknown:'人物待核对'}[value.subject] || '人物待核对'}`;
    if (field === 'anniversary') return value.origin_date || '日期未知';
    if (field === 'reminder_settings') return `VIP ${value.vip ?? '?'}天 · 星守护 ${value.star ?? '?'}天 · 生日 ${value.birthday ?? '?'}天 · 周年 ${value.anniversary ?? '?'}天`;
    if (field.startsWith('entitlement.')) return `${value.start || '?'} 至 ${value.end || '?'} · ${value.source_text || '依据未记录'}`;
    return '资料类型待核对';
  }
  function rows(value) {
    if (Array.isArray(value)) return value;
    if (value && Array.isArray(value.items)) return value.items;
    return [];
  }
  function overviewState(data) {
    if (!data || typeof data !== 'object') return {global_s:[],coverage_known:false,can_prioritize:false,union:[],waiting:[],wechat:[],unknown:[],optional:[],counts:null};
    const gate = data.coverage_gate;
    const known = gate === 'verified';
    const people = new Map(rows(data.people).map(person => [person.person_key,person]));
    const group = value => rows(value).map(item => typeof item === 'string' ? people.get(item) || {person_key:item,display_name:'对象显示名未知',reasons:[]} : item);
    return {global_s:group(data.global_s),coverage_known:known,can_prioritize:known,union:group(data.action),waiting:group(data.waiting),wechat:group(data.wechat),unknown:group(data.unknown),optional:group(data.optional),counts:Number.isInteger(data.count) ? {observed_action:data.count} : null,coverage:gate};
  }
  function stableRows(previous,incoming) {
    const pending = new Map(incoming.map(row => [row.person_key,row])); const output = [];
    for (const row of previous) if (pending.has(row.person_key)) {output.push(pending.get(row.person_key)); pending.delete(row.person_key);}
    output.push(...pending.values()); return output;
  }
  const text = value => typeof value === 'string' && value ? value : '未知';
  function rankLines(rank) {
    rank=rank&&typeof rank==='object'?rank:{};const metric=rank.metric&&typeof rank.metric==='object'?rank.metric:{};
    const a=rank.adoption,business=a&&a.format==='rank-observed-adoption-1'&&a.time_basis==='user_confirmed_previous_broadcast'&&rank.coverage==='observed_rows';
    const kind={contribution:'贡献榜',watch:'观看榜',comment:'评论榜'}[rank.kind]||'榜种未知';
    const known=metric.state==='known';const display=typeof metric.raw_display==='string'&&metric.raw_display?metric.raw_display:known?text(metric.value):'未知';
    return [
      ['榜种',kind],['原位',Number.isInteger(rank.position)&&rank.position>0?String(rank.position):rank.position===null&&rank.outside_top100_verified===true&&rank.coverage==='source_complete'?'已核验不在前100名':'未知，不能判为榜外'],
      ['指标',known?display:'未知'+(display!=='未知'?'（原显示：'+display+'）':'')],['原数值',known?text(metric.value):'未知'],['单位',text(metric.unit)],
      ['精度',{exact:'来源精确值',display:'来源显示精度，未换算',unknown:'未知'}[metric.precision]||'未知'],
      ['原周期',text(rank.period_label)+' · 周期标识：'+text(rank.period_key)],['周期窗口',business?'近30天，截至'+text(a.statistics_through_day)+'直播后；用户确认口径，平台精确起止未返回':text(rank.period_start_utc)+' 至 '+text(rank.period_end_utc)],
      ['分层适用日',text(rank.rank_as_of_day)+'；每日滚动更新，与采集时间分开'],
      ['分层适用',rank.tier_period_eligible===true&&(business||Date.parse(rank.period_end_utc)-Date.parse(rank.period_start_utc)===30*86400000)?'30日榜期；还须核对三榜同期':'不能代替30日榜'],
      ['实际采集',text(rank.effective_cutoff)],['覆盖',{observed_rows:'已采纳原名次；未出现不代表榜外',source_complete:'此来源周期完整',provenance_suspended:'来源依据暂停',no_complete_revision:'没有完整来源修订'}[rank.coverage]||'未知'],['来源修订',text(rank.revision_id)]
    ];
  }
  function tierFreshness(value,now=Date.now()) {
    const today=new Date(now+8*3600000).toISOString().slice(0,10);
    if(!value||value.format!=='rolling-rank-state-1'||typeof value.required_day!=='string')return {state:'unknown',label:'榜单所属日待核验；每日滚动更新'};
    const ready=value.required_day===today&&value.state==='ready';
    return {state:ready?'ready':'today_pending',day:value.required_day,label:ready?value.required_day+' 三榜已齐；每日滚动更新':today+' 今日待更新；本次资料读取日 '+value.required_day};
  }
  function tierLines(value,grade) {
    if(!value||value.format!=='tier-explanation-1'||value.rule_version!=='tier-20261008-combo-v1'||value.final_grade!==grade||!['S','A','B','C','D','E','unknown'].includes(grade))return [['分层依据','尚未提供可靠解释；单场数据不参与分层']];
    const reasons={identity_unknown:'身份未核验',input_invalid:'资格输入无效',period_missing:'缺少适用的同期30天榜',period_mismatch:'三榜周期不一致',qualification_incomplete:'资格证据不足，不能判为E',sufficient:'资格证据足以确定等级'};
    const raw=value.original_qualifications||{},names={S:'贡献1–50',A:'贡献51–100',B:'评论及观看均1–50',C:'正式切片资格',D:'观看1–100'};
    return [['判定依据',reasons[value.reason]||'未知'],...Object.entries(names).map(([key,label])=>['原始 '+key+' 资格',label+'：'+(raw[key]===true?'满足':raw[key]===false?'不满足':'未知')]),
      ['组合升级',{'A+B':'A+B→S（仅一次）','B+C':'B+C→A（仅一次）','C+D':'C+D→B（仅一次）'}[value.combination]||'未采用组合升级'],
      ['规则版本',value.rule_version],['数据范围','官方同期30天三榜及正式切片资格；单场数据不参与分层']];
  }
  function ruleLines(data) {
    data=data||{};const reply=data.reply||{};const pending=reply.pending;const spark=data.spark||{};
    return [
      ['回复状态',pending?'已观察待回段 · '+(Number.isInteger(pending.count)?pending.count+'条来信':'条数未知')+' · 起始 '+text(pending.start):reply.unknown_time?'时间未知，待回状态需核对':'没有已观察待回段；完整覆盖另行核验'],
      ['主联系路由',{wechat:'正式微信',douyin:'抖音',outside_tier_scope:'当前等级处理范围外',none:'无已观察待回路由'}[data.route]||'未知'],
      ['维护',{pending:'待办',completed:'可靠成功外发已覆盖今日维护',stopped:'正式维护未启用',unknown:'未知，需核验覆盖'}[data.maintenance]||'未知'],
      ['火花',{pending:'待发',waiting:'已发待亮',completed:'已观察到完成条件',stopped:'正式火花未启用',unknown:'未知',special_unverified:'特殊样式待核验'}[spark.state]||'未知'],
      ['时间与覆盖',(reply.unknown_time?'包含未知时间消息；':'')+(data.coverage&&data.coverage.state==='complete'?'此会话来源覆盖完整':'此会话完整覆盖未知')]
    ];
  }
  function dateLines(dates) {
    dates=dates||{};const lines=[];
    for(const [kind,label] of [['vip','VIP'],['star','星守护']]) {
      const terms=dates.terms&&Array.isArray(dates.terms[kind])?dates.terms[kind]:[];
      if(!terms.length)lines.push([label+'期限','未记录可靠独立期限']);
      for(const term of terms){let status={active:'本期有效',today_expiry:'今天到期（今天仍有效）',expired:'已到期',future:'本期尚未开始',unknown:'期限未知',invalid:'期限需核对'}[term.status]||'期限未知';if(Number.isInteger(term.days))status+=' · '+(term.status==='expired'?'已过':'剩余')+term.days+'天';lines.push([label+'独立期限',status+' · '+(term.remind?'已进入提前提醒窗口':'未处于可靠提醒窗口')+' · 原文依据：'+text(term.source_text)]);}
    }
    for(const [kind,label] of [['birthday','生日'],['anniversary','周年']]) {
      const item=dates[kind]||{};let status={known:'下一日期 '+text(item.next),unknown:'未知',verify:'历法或日期待核对',verify_subject:'人物归属待核对',not_completed_year:'尚未完成首年'}[item.status]||'未知';
      if(item.status==='known'&&Number.isInteger(item.days))status+=' · '+item.days+'天后'+(item.days<=7?'（未来七天）':'');
      if(kind==='anniversary')status+=' · '+({manual:'人工起始依据',recorded:'已记录起始依据',first_chat:'来源核验首聊',earliest_recorded:'最早已保存聊天依据，完整首聊未核验',unknown:'起始依据未知'}[item.basis]||'起始依据未知');
      if(item.source_text)status+=' · 原文：'+text(item.source_text);lines.push([label+'预留',status]);
    }
    return lines;
  }
  function recentLines(recent) {
    recent=recent||{};if(!['known','partial'].includes(recent.state))return [['近七天','未知；缺少已核验的场次与采集窗口']];
    const current=recent.current||{},complete=current.complete||{},window=Array.isArray(current.window)?current.window:[];
    return [['近七天窗口',text(window[0])+' 至 '+text(window[1])+' · 采集日 '+text(recent.effective_collection_day)],
      ['窗口覆盖',recent.state==='known'?'已核验可比窗口':'窗口不完整，不能从部分值推断可靠零'],
      ['支持',complete.support===true&&current.support!==null&&current.support!==undefined?String(current.support)+' '+text(recent.support_unit):'未知'],
      ['观看秒数',complete.arrival===true&&current.watch_seconds!==null&&current.watch_seconds!==undefined?String(current.watch_seconds):'未知'],
      ['评论',complete.comment===true&&current.comments!==null&&current.comments!==undefined?String(current.comments):'未知'],
      ['到场比例',complete.arrival===true&&current.arrival_ratio!==null&&current.arrival_ratio!==undefined?String(current.arrival_ratio):'未知'],
      ['数据时间',text(recent.captured_at_utc)+'；统计保留上述窗口，不等于今日实时状态'],
      ['归日依据',recent.window_basis==='session_start_day'?'现行规则按场次开始日归属，跨午夜整场未拆分':'待核验']];
  }
  function activityLines(slots,opportunity) {
    const lines=[];const sources=slots&&slots.sources||{};
    for(const [key,label] of [['incoming','来信'],['entry','入场'],['interaction','互动']]) {
      const source=sources[key];if(!source){lines.push([label+'北京时段','缺少可靠样本，未知']);continue;}
      const selected=Array.isArray(source.selected)?source.selected.filter(value=>Number.isInteger(value)&&value>=0&&value<12).slice(0,2):[];
      const windows=selected.map(value=>String(value*2).padStart(2,'0')+':00–'+String(value*2+2).padStart(2,'0')+':00');
      lines.push([label+'北京时段',Number.isInteger(source.days)&&source.days>=5&&windows.length?windows.join('、')+' · 观察到'+source.days+'个样本日':source.dispersed?'样本分散，未展示偏好时段':'可靠样本不足或未达到阈值，偏好时段未知']);
    }
    lines.push(['可选机会',{eligible:'此样本有可靠可选依据，人工决定',sample_unknown:'样本目录或时间未知，不能判断机会',insufficient:'样本不完整，依据不足',not_eligible:'已核验样本尚未达到机会条件'}[opportunity&&opportunity.status]||'未知']);
    return lines;
  }
  function sidebarMatches(data,binding) {
    const matches = value => value && typeof value === 'object' && !Array.isArray(value) &&
      value.account_key === binding.account_key && value.person_key === binding.person_key;
    // Both the card and its editable profile must belong to the requested object.
    return Boolean(matches(data) && matches(data.profile));
  }
  class WorkbenchController {
    constructor(api,callbacks) { this.api = api; this.callbacks = callbacks; this.accountGeneration = 0; this.personGeneration = 0; this.overviewGeneration = 0; this.account = null; this.person = null; this.contacts = []; this.overview = []; }
    async accountSelect(account) {
      const stamp = ++this.accountGeneration; this.personGeneration++; this.account = account; this.person = null; this.contacts = []; this.overview = [];
      this.callbacks.contact(null); this.callbacks.contacts({state:'loading',items:[]}); this.callbacks.overview({state:'loading',data:null});
      const contacts = this.api.contacts({account_key:account}).then(data => {
        if (stamp !== this.accountGeneration) return;
        if (!data || !Array.isArray(data.items)) throw new Error('bad_response');
        this.contacts = data.items; this.callbacks.contacts({state:data.items.length ? 'ready' : 'empty',items:data.items});
      }).catch(() => {if (stamp === this.accountGeneration) this.callbacks.contacts({state:'unavailable',items:[]});});
      const overview = this.refreshOverview();
      await Promise.all([contacts,overview]);
    }
    async refreshOverview() {
      if (!this.account) return;
      const account = this.account; const stamp = this.accountGeneration; const request = ++this.overviewGeneration;
      try {
        const data = await this.api.overview({account_key:account});
        if (stamp !== this.accountGeneration || request !== this.overviewGeneration) return;
        const normalized = overviewState(data); normalized.union = stableRows(this.overview,normalized.union); this.overview = normalized.union;
        this.callbacks.overview({state:'ready',data:normalized});
      } catch {if (stamp === this.accountGeneration && request === this.overviewGeneration) this.callbacks.overview({state:'unavailable',data:null});}
    }
    async contactSelect(person) {
      const stamp = ++this.personGeneration; const account = this.account; this.person = person;
      if (!person) {this.callbacks.contact(null); return;}
      const contact = this.contacts.find(item => item.person_key === person);
      if (!contact) throw new Error('contact_not_available');
      this.callbacks.contact({state:'loading',account_key:account,person_key:person,contact});
      try {
        const data = await this.api.sidebar({account_key:account,person_key:person});
        if (stamp !== this.personGeneration || account !== this.account) return;
        if (!sidebarMatches(data,{account_key:account,person_key:person})) throw new Error('bad_response');
        this.callbacks.contact({state:'ready',account_key:account,person_key:person,contact,data});
      } catch {if (stamp === this.personGeneration && account === this.account) this.callbacks.contact({state:'unavailable',account_key:account,person_key:person,contact});}
    }
    clear() {this.accountGeneration++; this.personGeneration++; this.account = null; this.person = null; this.contacts = []; this.overview = []; this.callbacks.contact(null); this.callbacks.contacts({state:'empty',items:[]}); this.callbacks.overview({state:'empty',data:null});}
  }
  class FieldEditor {
    constructor(save,binding,record) {
      fieldType(binding.field); this.saveRequest = save; this.binding = Object.freeze({...binding});
      this.expectedRevision = clone(record ? record.revision : []); this.value = clone(record && (Object.prototype.hasOwnProperty.call(record,'effective_value')?record.effective_value:record.value)); this.clear = false; this.state = record&&record.state==='conflict'?'conflict':'idle';
    }
    draft(value,clear = false) {if (this.state === 'saving') throw new Error('save_in_progress'); this.value = clone(value); this.clear = clear; if (this.state !== 'conflict') this.state = 'edited';}
    async save() {
      if (this.state === 'saving') throw new Error('save_in_progress');
      if (this.state === 'conflict') throw new Error('rebase_required');
      validateFieldValue(this.binding.field,this.clear?'':this.value,this.clear);
      this.state = 'saving';
      const payload = {...this.binding,value:this.clear ? '' : clone(this.value),clear:this.clear,expected_revision:clone(this.expectedRevision)};
      try {const result = await this.saveRequest(payload); if (!result || result.status !== 'saved' || !Array.isArray(result.revision) || !result.revision.length || result.revision.some(value=>typeof value!=='string'||!value) || result.field!==undefined&&result.field!==this.binding.field) throw new Error('bad_response'); this.state = 'saved'; return result;}
      catch (error) {this.state = error.message === 'conflict' ? 'conflict' : 'failed'; throw error;}
    }
    rebase(record) {if (this.state === 'saving') throw new Error('save_in_progress'); if (!record || !Array.isArray(record.revision) || record.revision.some(value=>typeof value!=='string'||!value)) throw new Error('bad_response'); this.expectedRevision = clone(record.revision); this.state = 'edited';}
  }
  function isoDay(value) {
    if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value)||value.slice(0,4)==='0000')throw new Error('invalid_value');
    const day=new Date(value+'T00:00:00Z');if(!Number.isFinite(day.getTime())||day.toISOString().slice(0,10)!==value)throw new Error('invalid_value');return day;
  }
  function remainingEnd(reference,days,basis) {
    const date=isoDay(reference);if(!Number.isInteger(days)||days<0||days>3660||!['after_reference','including_reference'].includes(basis)||basis==='including_reference'&&days<1)throw new Error('invalid_value');
    date.setUTCDate(date.getUTCDate()+days-(basis==='including_reference'?1:0));const result=date.toISOString().slice(0,10);isoDay(result);return result;
  }
  function validateFieldValue(field,value,clear=false) {
    const kind=fieldType(field);if(clear){if(value!=='')throw new Error('invalid_value');return value;}
    const invalid=()=>{throw new Error('invalid_value');};
    if(kind==='text'){if(typeof value!=='string'||value.length>10000||value.includes('\0'))invalid();return value;}
    if(kind==='boolean'){if(typeof value!=='boolean')invalid();return value;}
    if(!value||typeof value!=='object'||Array.isArray(value))invalid();
    const keys=field==='location'?['country','city','timezone']:field==='birthday'?['month','day','calendar','subject','source_text']:field==='anniversary'?['origin_date','source_text','manual']:field==='reminder_settings'?['vip','star','birthday','anniversary']:['start','end','source_text'];
    if(Object.keys(value).sort().join()!==keys.sort().join())invalid();
    if('source_text'in value&&(typeof value.source_text!=='string'||value.source_text.length>10000||value.source_text.includes('\0')))invalid();
    if(field==='location'){if(Object.values(value).some(item=>typeof item!=='string'||!item.trim()||item.length>200)||! /^[A-Za-z0-9_+\-/]+$/.test(value.timezone)||value.timezone.includes('..')||value.timezone.startsWith('/'))invalid();}
    else if(field==='reminder_settings'){if(Object.values(value).some(item=>!Number.isInteger(item)||item<0||item>365))invalid();}
    else if(field==='birthday'){
      if(!Number.isInteger(value.month)||!Number.isInteger(value.day)||value.month<1||value.month>12||value.day<1||value.day>31||!['gregorian','lunar','lunar_leap','unknown'].includes(value.calendar)||!['self','other','unknown'].includes(value.subject))invalid();
      if(value.calendar==='gregorian')isoDay('2000-'+String(value.month).padStart(2,'0')+'-'+String(value.day).padStart(2,'0'));
      if(['lunar','lunar_leap'].includes(value.calendar)&&value.day>30)invalid();
    } else if(field==='anniversary'){isoDay(value.origin_date);if(typeof value.manual!=='boolean')invalid();}
    else {isoDay(value.start);isoDay(value.end);if(value.end<value.start)invalid();}
    return value;
  }
  function formValue(field,entries) {
    const values=Object.fromEntries(entries),kind=fieldType(field);if(kind==='text')return validateFieldValue(field,values.value);
    if(kind==='boolean'){if(!['true','false'].includes(values.value))throw new Error('invalid_value');return values.value==='true';}
    if(field==='reminder_settings'||field==='birthday')for(const key of field==='birthday'?['month','day']:['vip','star','birthday','anniversary']){if(typeof values[key]!=='string'||!/^\d+$/.test(values[key]))throw new Error('invalid_value');values[key]=Number(values[key]);}
    if(field==='anniversary')values.manual=true;return validateFieldValue(field,values);
  }
  function candidateLines(field,candidate) {
    if(!candidate||!['set','clear'].includes(candidate.operation))return [['版本','格式待核对']];
    const lines=[['版本',text(candidate.event_id)],['操作',candidate.operation==='clear'?'明确清空':'设置'],['资料',candidate.operation==='clear'?'已选择空值':fieldValue(field,{effective_value:candidate.value})]];
    const value=candidate.value;if(candidate.operation!=='clear'&&value&&typeof value==='object')for(const [key,label] of [['source_text','原文依据'],['manual','人工起始确认']])if(Object.prototype.hasOwnProperty.call(value,key))lines.push([label,typeof value[key]==='boolean'?(value[key]?'是':'否'):text(value[key])]);
    return lines;
  }
  return {TAGS,roleLabel,contactLabel,fieldLabel,fieldType,stateLabel,fieldValue,overviewState,stableRows,rankLines,tierLines,tierFreshness,ruleLines,dateLines,recentLines,activityLines,isoDay,remainingEnd,validateFieldValue,formValue,candidateLines,sidebarMatches,WorkbenchController,FieldEditor};
});
