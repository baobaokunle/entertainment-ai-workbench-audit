(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DSHOperations=api;})(globalThis,function(){
  'use strict';
  const beijingDay=()=>new Date(Date.now()+8*3600000).toISOString().slice(0,10);
  function day(value){if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value)||!Number.isFinite(Date.parse(value+'T00:00:00Z'))||new Date(value+'T00:00:00Z').toISOString().slice(0,10)!==value)throw Error('invalid_value');return value;}
  function make(document){return (tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};}
  function opportunity(target,api,binding,initial,isCurrent,onSaved=()=>{},document=globalThis.document){
    const n=make(document),notice=n('p'),time=n('input'),save=n('button','保存提醒时间'),display=n('button','查看本轮机会'),cancel=n('button','取消今天这一轮'),reload=n('button','重新读取本轮状态');time.type='time';time.setAttribute('aria-label','机会提醒时间');
    for(const b of [save,display,cancel,reload])b.type='button';target.append(notice,time,save,display,cancel,reload);
    let model=initial,busy=false,serial=0,disposed=false,needsRead=false;
    const live=()=>!disposed&&target.isConnected&&isCurrent();
    function valid(){const c=model?.control;return c&&['time','round'].every(k=>c[k]&&Array.isArray(c[k].revision)&&['empty','effective','conflict'].includes(c[k].state))&&typeof c.day==='string';}
    function render(keepTime=false){
      const c=model?.control,access=api.editingAccess?.(binding.account_key,'opportunity_time')?.allowed===true,good=valid(),today=good&&c.day===beijingDay(),round=good&&c.round.effective_value;
      if(!keepTime)time.value=good?(c.time.effective_value||'18:30'):'18:30';
      const ready=good&&!needsRead&&c.time.state!=='conflict'&&c.round.state!=='conflict',nowTime=new Date(Date.now()+8*3600000).toISOString().slice(11,16),due=good&&nowTime>=(c.time.effective_value||'18:30');
      time.disabled=save.disabled=busy||!access||!ready||!today;
      display.disabled=busy||!access||!ready||!today||!due||model.status!=='eligible'||round==='canceled'||round==='displayed';
      cancel.disabled=busy||!access||!ready||!today||model.status!=='eligible'||round==='canceled';reload.disabled=busy;
      notice.textContent=!good?'尚无可核对的机会版本。':!today?'页面已跨天，请读取今天的状态。':!ready?'存在并发版本，请核对后处理；当前不能覆盖。':(model.status==='eligible'?'已满足现有机会门槛。':'机会依据不足，保留未知。')+' '+(round==='canceled'?'今天这轮已取消，次日重新计算。':round==='displayed'?'本轮已经展示；改时间从明天生效。':'默认18:30；修改时间不改变聊天完成状态。')+(!access?' 当前连接不能修改。':'');
    }
    async function refresh(keepTime=false){const fresh=await api.sidebar(binding);if(!live())throw Error('stale_session');if(fresh.account_key!==binding.account_key||fresh.person_key!==binding.person_key)throw Error('bad_response');model=fresh.opportunity;needsRead=false;render(keepTime);}
    async function act(action){if(busy||!live()||!valid())return;const c=model.control;if(c.day!==beijingDay()){render(true);return;}
      const body={...binding,expected_revision:[...c[action==='time'?'time':'round'].revision],[action==='time'?'time':'day']:action==='time'?time.value:c.day};
      const stamp=++serial;busy=true;render(true);try{const result=await api.opportunity(action,body);if(!live()||stamp!==serial)return;if(result?.status!=='saved'||!Array.isArray(result.revision)||!result.revision.length||result.revision.some(v=>typeof v!=='string'||!v))throw Error('bad_response');await refresh();if(!live())return;notice.textContent=c.day!==beijingDay()||model.control?.day!==c.day?'操作期间已跨天；回执针对原轮次或提醒设置，当前状态请按新日期核对。':action==='time'?'提醒时间已保存；'+(result.time_change_effect==='tomorrow'?'本轮不复活，明天生效。':'今天未处理的轮次按新时间判断。'):action==='cancel'?'今天这轮已取消；没有标记为已聊天。':'本轮已展示；仍需人工处理聊天。';onSaved();}
      catch(error){if(live()&&stamp===serial){needsRead=true;notice.textContent=error.message==='conflict'?'版本发生变化；输入保留，请重新读取，不会自动重试。':'未确认操作结果；请先重新读取状态，勿重复提交。';}}
      finally{if(live()&&stamp===serial){busy=false;const text=notice.textContent;render(true);notice.textContent=text;}}
    }
    save.addEventListener('click',()=>act('time'));display.addEventListener('click',()=>act('display'));cancel.addEventListener('click',()=>act('cancel'));
    reload.addEventListener('click',async()=>{if(busy||!live())return;busy=true;render(true);try{await refresh(true);}catch{if(live())notice.textContent='状态读取失败，输入保留。';}finally{if(live()){busy=false;const text=notice.textContent;render(true);notice.textContent=text;}}});
    const timer=document.defaultView?.setInterval(()=>{if(live()&&!busy)render(true);},60000);
    render();return {dispose(){disposed=true;serial++;if(timer!==undefined)document.defaultView.clearInterval(timer);target.replaceChildren();}};
  }
  function statistics(target,api,document=globalThis.document){
    const n=make(document),status=n('p','选择账号后查询已保存联系事实。'),start=n('input'),end=n('input'),operator=n('input'),query=n('button','查询联系统计'),results=n('div'),presets=[];
    for(const [label,input]of [['联系开始日期',start],['联系结束日期',end],['操作者登记键（管理可选）',operator]]){const l=n('label',label);input.type=input===operator?'text':'date';input.setAttribute('aria-label',label);input.maxLength=200;l.append(input);target.append(l);}
    for(const [days,label]of [[1,'今天'],[7,'近7天'],[30,'近30天']]){const b=n('button',label);b.type='button';b.addEventListener('click',()=>{end.value=beijingDay();start.value=new Date(Date.parse(end.value+'T00:00:00Z')-(days-1)*86400000).toISOString().slice(0,10);});presets.push(b);target.append(b);}
    query.type='button';target.append(query,status,results);let account=null,generation=0,busy=false;
    function select(value){generation++;account=value||null;busy=false;results.replaceChildren();end.value=beijingDay();start.value=end.value;const a=api.contactStatisticsAccess?.(account)||{allowed:false};operator.value=a.operator_key||'';operator.disabled=a.role!=='manager';query.disabled=!a.allowed;status.textContent=a.allowed?'今天、近7天、近30天均包含今天；只查询已保存事实，不采集或发送。':'当前账号未获准查询联系统计。';}
    query.addEventListener('click',async()=>{if(!account||busy)return;let q;try{q={account_key:account,start_day:day(start.value),end_day:day(end.value)};if(q.start_day>q.end_day)throw Error('invalid_value');if(operator.value.trim())q.operator_key=operator.value.trim();}catch{status.textContent='请核对起止日期。';return;}
      const stamp=++generation;busy=true;query.disabled=true;results.replaceChildren();status.textContent='查询已保存事实…';
      try{const data=await api.contactStatistics(q);if(stamp!==generation||!target.isConnected)return;if(data.account_key!==q.account_key||data.contact_window?.start_day!==q.start_day||data.contact_window?.end_day!==q.end_day||q.operator_key&&data.operator_key!==q.operator_key)throw Error('bad_response');
        const count=v=>Number.isSafeInteger(v)&&v>=0?String(v):'未知',ratio=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0&&v<=1?(v*100).toFixed(1)+'%':'未知';
        const lines=[['联系窗口',q.start_day+' 至 '+q.end_day],['本人已归属联系人数',count(data.personal_count)],['账号已观察人工联系人数',count(data.account_observed_manual_count)],['已观察范围占比',ratio(data.observed_ratio)],['完整分母占比',data.ratio_state==='complete'?ratio(data.ratio):'未知，覆盖未核验'],['人工归属未知人数',Array.isArray(data.unknown_manual_keys)?count(data.unknown_manual_keys.length):'未知'],['自动发送对象数',Array.isArray(data.automatic_keys)?count(data.automatic_keys.length):'未知']];
        const c=data.contribution;if(c?.state==='known_subtotal'&&c.unit&&Array.isArray(c.source_window)&&c.source_window.length===2){lines.push(['独立30日贡献已知小计',String(c.known_subtotal)+' '+c.unit],['贡献来源窗口',c.source_window.join(' 至 ')],['贡献未知人数',Array.isArray(c.unknown_keys)?count(c.unknown_keys.length):'未知']);}else lines.push(['独立30日贡献','来源、单位或窗口不足，未知']);
        for(const [label,value]of lines)results.append(n('p',label+'：'+value));status.textContent='仅反映已观察事实；贡献不是人工联系带来的收入，不生成工时或绩效结论。';
      }catch{if(stamp===generation){results.replaceChildren();status.textContent='统计暂不可用，未将缺失值记作0。';}}
      finally{if(stamp===generation){busy=false;query.disabled=!api.contactStatisticsAccess(account).allowed;}}
    });
    select(null);return {selectAccount:select,clear:()=>select(null)};
  }
  return {opportunity,statistics,beijingDay,day};
});
