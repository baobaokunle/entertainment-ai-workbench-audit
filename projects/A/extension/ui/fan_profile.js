(function(root,factory){
  const api=factory(typeof module==='object'&&module.exports?require('../identity/history_summary.js'):root.DSHHistorySummary);
  if(typeof module==='object'&&module.exports)module.exports=api;else root.DSHFanProfile=api;
})(globalThis,function(H){
  'use strict';
  const states=['candidate','selection_candidate','resumed_candidate','recalled_candidate'];
  const get=(value,key)=>{try{if(!value||typeof value!=='object')return undefined;const d=Object.getOwnPropertyDescriptor(value,key);return d&&Object.hasOwn(d,'value')?d.value:undefined;}catch{return undefined;}};
  function project(view){
    const state=get(view,'state'),historical=get(view,'historical_identity'),connection=get(view,'connection');
    const stopped=['service_unavailable','login_required'].includes(connection);
    const current=get(view,'current_identity');
    const accepted=!stopped&&states.includes(state)&&get(view,'owner')==='unverified'&&get(view,'private_context')==='unverified'&&get(view,'formal_binding')===false&&
      (state==='candidate'?(current===undefined||current==='unverified'):current==='unverified');
    const matched=accepted&&historical==='sender_candidate';
    const summary=H&&H.fromResponse(view,matched),available=matched&&summary&&summary.state==='available';
    const identity=matched?(state==='recalled_candidate'?'此前匹配到历史候选，当前对象待核实':'历史候选命中，正式身份未绑定'):
      accepted&&historical==='zero'?'未找到可靠历史候选':accepted&&historical==='conflict'?'历史候选冲突，资料未知':'当前对象待核实';
    const declared=!stopped&&get(view,'historical_export_source_statement')==='user_confirmed';
    const rows=[
      ['正式档案','尚未关联'],
      ['正式标签','未知，尚未关联'],
      ['地区与时区','未知，尚未关联'],
      ['日期与期限','未知，尚未关联'],
      ['当地时间','未开启或来源未知'],
      ['关注与互关','未知，尚未关联'],
      ['取关变化','尚无可靠连续观测'],
      ['明显参与回落','未知，比较窗口待核验'],
      ['直播常见时段','未知，样本不足；不表示当前在线'],
      ['聊天机会','未知，场次依据待核验'],
      ['SABCDE等级','未计算，资料不足'],
      ['榜单日更','今日待更新；须核对三榜所属日'],
      ['分层依据','资料不足；单场数据不参与分层'],
      ['已保存分层参考','暂无可靠的已保存30天榜期'],
      ['账号归属','程序尚未核实'],
      ['私聊范围','尚未核实'],
      ['历史身份',identity],
      ['有效历史记录',available?summary.message_count+'条（导出内）':'未知'],
      ['涉及会话',available?summary.conversation_count+'个':'未知'],
      ['导出来源',declared?'已由你确认':'待确认'],
      ['统计窗口','未知；缺少已核验的场次与采集窗口'],
      ['窗口覆盖','未知'],
      ['近7天支持','未知'],
      ['近7天观看','未知'],
      ['近7天评论','未知'],
      ['近7天到场','未知'],
      ['统计依据','按已保存正式场次；不影响今天的待回、维护和火花'],
      ['单场来源','未知，尚未关联'],
      ['礼物贡献','未知，尚未关联'],
      ['评论','未知，尚未关联'],
      ['点赞','未知，尚未关联'],
      ['观看','未知，尚未关联'],
      ['陪伴','未知，尚未关联'],
      ['贡献榜','未知'],
      ['评论榜','未知'],
      ['观看榜','未知'],
    ];
    return Object.freeze({title:'当前对象资料',summary_state:available?'available':'unknown',
      rows:Object.freeze(rows.map(row=>Object.freeze(row)))});
  }
  function metricText(metric,single=true){
    const value=get(metric,'value'),unit=get(metric,'unit'),precision=get(metric,'precision');
    if(get(metric,'state')!=='known'||typeof value!=='string'||value.length>100||
       !/^\+?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value)||!Number.isFinite(Number(value))||
       Number(value)<0||Number(value)>1e30||typeof unit!=='string'||!unit||unit.length>80||
       !['exact','display'].includes(precision))return '未知';
    const raw=get(metric,'raw_display');
    return (typeof raw==='string'&&raw&&raw.length<=256?'原显示 '+raw+'；':'')+value+' '+unit+'（'+(single?'所示已保存单场；':'')+(precision==='exact'?'来源精确值':'来源显示精度')+
      (single?'；'+(get(metric,'coverage')==='complete'?'该指标覆盖完整':'该指标覆盖不完整'):'')+'）';
  }
  const dateText=value=>typeof value==='string'&&value.length<=40&&Number.isFinite(Date.parse(value))?value:'未知';
  function profileText(profile){
    const fields=get(profile,'fields'),tags=[];
    for(const [key,label]of [['clip','正式切片'],['wechat','微信主联系'],['maintenance','日常维护'],['spark','火花维护'],['vip','VIP会员'],['star','星守护'],['local_time','当地时间']]){
      const record=get(fields,'formal_tag.'+key),value=get(record,'effective_value');
      tags.push(label+'：'+(get(record,'state')==='conflict'?'有冲突':get(record,'state')==='effective'&&typeof value==='boolean'?(value?'是':'否（已明确登记）'):'未记录'));
    }
    const record=get(fields,'location'),value=get(record,'effective_value');
    const place=['country','city','timezone'].map(k=>get(value,k)).filter(x=>typeof x==='string'&&x&&x.length<=200).join(' · ');
    return {tags:tags.join('；'),location:get(record,'state')==='conflict'?'有冲突，需在工作台核对':get(record,'state')==='effective'&&place?place:'未知／未记录'};
  }
  function recentRows(recent){
    if(!['known','partial'].includes(get(recent,'state')))return {};
    const current=get(recent,'current'),complete=get(current,'complete'),window=get(current,'window');
    const day=value=>typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&dateText(value)!=='未知'?value:'未知';
    const start=Array.isArray(window)&&window.length===2?day(get(window,'0')):'未知',end=Array.isArray(window)&&window.length===2?day(get(window,'1')):'未知';
    if(start==='未知'||end==='未知'||(Date.parse(end)-Date.parse(start))!==6*86400000)return {};
    const number=(key,direction)=>{const v=get(current,key);return get(complete,direction)===true&&(typeof v==='number'||typeof v==='string')&&/^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(String(v))&&String(v).length<=100&&Number.isFinite(Number(v))&&Number(v)>=0?String(v):null;};
    const support=number('support','support'),unit=get(recent,'support_unit'),watch=number('watch_seconds','arrival'),comments=number('comments','comment'),arrival=number('arrival_days','arrival'),days=get(current,'broadcast_days');
    const full=get(recent,'state')==='known';
    return {'统计窗口':start+' 至 '+end+'（北京时间；已保存统计）',
      '窗口覆盖':full?'两期场次目录已核验；各指标仍单独核验':'部分窗口；不足以判断完整总量或可靠零',
      '近7天支持':full&&support!==null&&typeof unit==='string'&&unit&&unit.length<=80?support+' '+unit:'未知，单位或覆盖待补齐',
      '近7天观看':full&&watch!==null?watch+' 秒':'未知，覆盖待补齐',
      '近7天评论':full&&comments!==null?comments+' 条':'未知，覆盖待补齐',
      '近7天到场':full&&arrival!==null&&Number.isSafeInteger(days)&&days>0?arrival+' / '+days+' 个开播日':'未知，出席或分母待补齐',
      '统计依据':'采集日 '+day(get(recent,'effective_collection_day'))+'；采集截止 '+dateText(get(recent,'captured_at_utc'))+'；'+(get(recent,'window_basis')==='session_start_day'?'现行规则按场次开始日归属，跨午夜整场未拆分。':'归日依据待核验。')+'不是今天的实时会话状态。'};
  }
  function tierText(value,grade){
    if(get(value,'format')!=='tier-explanation-1'||get(value,'rule_version')!=='tier-20261008-combo-v1'||
       get(value,'final_grade')!==grade||!['S','A','B','C','D','E','unknown'].includes(grade))return '分层依据待补齐；单场数据不参与分层';
    const reason={identity_unknown:'身份未核验',input_invalid:'资格输入无效',period_missing:'缺少适用的同期30天榜',period_mismatch:'三榜周期不一致',qualification_incomplete:'资格证据不足，不能判为E',sufficient:'资格证据足以确定等级'}[get(value,'reason')]||'依据待核验';
    const raw=get(value,'original_qualifications'),names={S:'贡献1–50',A:'贡献51–100',B:'评论及观看均1–50',C:'正式切片资格',D:'观看1–100'};
    const original=Object.entries(names).map(([key,label])=>key+' '+label+'：'+(get(raw,key)===true?'满足':get(raw,key)===false?'不满足':'未知')).join('；');
    const combo={'A+B':'A+B→S','B+C':'B+C→A','C+D':'C+D→B'}[get(value,'combination')];
    return reason+'；原始资格：'+original+'；'+(combo?'一次组合 '+combo+'；':'')+'规则 '+get(value,'rule_version')+'；单场数据不参与分层';
  }
  function rankText(rank){
    const position=get(rank,'position'),start=get(rank,'period_start_utc'),end=get(rank,'period_end_utc');
    const a=get(rank,'adoption'),rawThrough=get(a,'statistics_through_day'),through=typeof rawThrough==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(rawThrough)&&Number.isFinite(Date.parse(rawThrough))?rawThrough:'未知';
    const business=get(a,'format')==='rank-observed-adoption-1'&&get(a,'time_basis')==='user_confirmed_previous_broadcast'&&through!=='未知'&&get(rank,'coverage')==='observed_rows'&&start===null&&end===null;
    if(!business&&(typeof start!=='string'||typeof end!=='string'||start.length>40||end.length>40||
       !Number.isFinite(Date.parse(start))||!Number.isFinite(Date.parse(end))||Date.parse(end)<=Date.parse(start)))return '未知';
    const outside=get(rank,'outside_top100_verified')===true&&get(rank,'coverage')==='source_complete'&&position===null;
    const place=Number.isSafeInteger(position)&&position>0?'原名次 '+position:outside?'已核验不在前100名':'名次未知，不能判为榜外';
    const safe=key=>{const value=get(rank,key);return typeof value==='string'&&value.length<=256?value:'未知';};
    const windowText=business?'近30天 · 截至'+through+'直播后（用户确认口径；平台精确起止未返回）':start+' 至 '+end+'（'+(get(rank,'tier_period_eligible')===true&&Date.parse(end)-Date.parse(start)===30*24*60*60*1000?'30日榜期':'来源榜期，不能代替30日榜')+'）';
    return place+'；'+metricText(get(rank,'metric'),false)+'；'+windowText+
      '；分层适用日 '+safe('rank_as_of_day')+'；原周期 '+safe('period_label')+' / '+safe('period_key')+'；实际采集 '+safe('effective_cutoff')+'；'+
      (get(rank,'coverage')==='source_complete'?'此来源周期完整':business?'已采纳原名次；未出现不代表榜外':'覆盖未核验')+'；修订 '+safe('revision_id');
  }
  function projectSaved(result,binding,qualified=false){
    const fallback=project(),saved=get(result,'binding'),card=get(result,'card');
    if(get(result,'format')!=='native-profile-lookup-1'||get(result,'state')!=='saved_binding_ready'||
       get(result,'authority_scope')!=='current_independent_approval'||get(result,'native_page_verification')!=='background_required'||
       get(result,'message_history')!=='not_loaded'||get(result,'AI_reference')!=='not_requested'||
       !['account_key','person_key','conversation_key'].every(key=>typeof get(binding,key)==='string'&&
         get(binding,key).length>0&&get(saved,key)===get(binding,key))||get(card,'format')!=='native-profile-card-1'||
       get(card,'account_key')!==get(binding,'account_key')||get(card,'person_key')!==get(binding,'person_key')||
       get(card,'object_kind')!=='person'||get(card,'verified')!==true)return fallback;
    const rows=fallback.rows.map(row=>[...row]),set=(label,value)=>{rows.find(row=>row[0]===label)[1]=value;};
    const name=get(card,'display_name'),grade=get(card,'grade');
    const verified=qualified===true&&get(result,'fixture')===false;
    set('历史身份',verified?'已保存对应；当前聊天对象核对通过':'已保存对应；当前聊天待核验');
    set('正式档案',typeof name==='string'&&name.length<=256?'已保存：'+name:'已保存档案');
    const details=profileText(get(card,'profile'));set('正式标签',details.tags);set('地区与时区',details.location);
    for(const [label,value]of Object.entries(recentRows(get(card,'recent'))))set(label,value);
    const dates=get(card,'dates'),dateDay=get(dates,'as_of_day'),todayDate=new Date(Date.now()+8*3600000).toISOString().slice(0,10);
    if(dateDay===todayDate){
      const summary=value=>{const state=get(value,'status'),days=get(value,'days'),next=get(value,'next');return ({active:'有效',today_expiry:'今日到期',expired:'已过期',future:'尚未开始',known:'日期已记录',verify:'日期／历法待核对',verify_subject:'人物待核对',not_completed_year:'尚未满周年',unknown:'未知'}[state]||'未知')+(Number.isSafeInteger(days)&&days>=0?' · '+days+'天':'')+(dateText(next)!=='未知'?' · '+next:'')+(get(value,'remind')===true?' · 已进入提醒期':'');};
      const parts=['生日：'+summary(get(dates,'birthday')),'周年：'+summary(get(dates,'anniversary'))];
      for(const [kind,label]of [['vip','VIP'],['star','星守护']]){const terms=get(get(dates,'terms'),kind);if(Array.isArray(terms)&&terms.length<=500)for(let i=0;i<Math.min(terms.length,20);i++)parts.push(label+'第'+(i+1)+'期：'+summary(get(terms,String(i))));if(terms?.length>20)parts.push(label+'其余期间请在工作台查看');}
      set('日期与期限',parts.join('；')+'（读取日 '+dateDay+'）');
      const local=get(dates,'local_time'),time=get(local,'time'),zone=get(local,'timezone');if(typeof time==='string'&&/^\d{2}:\d{2}$/.test(time)&&typeof zone==='string'&&zone.length<=200)set('当地时间','读取时 '+time+' · '+zone+'；不是持续走动的时钟');
    }else if(typeof dateDay==='string')set('日期与期限','读取日 '+dateDay+' 已过期，请更新当前日期状态');
    const relation=get(card,'relations'),directions=get(relation,'directions');
    const follow=key=>({known_following:'已关注',known_not_following:'明确未关注',unknown:'未知'}[get(directions,key)]||'未知');
    set('关注与互关','对方关注我：'+follow('fan_follows_me')+'；我关注对方：'+follow('i_follow_fan')+'；互关：'+(get(relation,'mutual')===true?'是':get(relation,'mutual')===false?'否（已核验）':'未知'));
    const changes=get(relation,'changes');if(Array.isArray(changes)&&changes.length&&changes.length<=20){const rows=[];for(let i=0;i<changes.length;i++){const change=get(changes,String(i)),label={fan_follows_me:'对方取关',i_follow_fan:'我取关'}[get(change,'direction')];if(label)rows.push(label+' · 实际时间 '+dateText(get(change,'actual_at_utc'))+' · 发现时间 '+dateText(get(change,'discovered_at_utc')));}if(rows.length)set('取关变化',rows.join('；'));}
    const decline=get(get(card,'recent'),'declines');if(get(get(card,'recent'),'state')==='known'&&typeof get(decline,'observation')==='boolean')set('明显参与回落',get(decline,'observation')?'可比窗口内已观察到回落；不推断原因或意愿':'可比窗口内未达到既定回落条件');
    const slots=get(card,'live_slots'),preferred=get(slots,'live_preferred'),slot=get(get(slots,'sources'),preferred),selected=get(slot,'selected');if(dateDay===todayDate&&['entry','interaction'].includes(preferred)&&Array.isArray(selected)&&selected.length>0&&selected.length<=2&&selected.every(x=>Number.isInteger(x)&&x>=0&&x<12)&&get(slot,'days')>=5)set('直播常见时段',selected.map(x=>String(x*2).padStart(2,'0')+':00–'+String(x*2+2).padStart(2,'0')+':00').join('、')+'（北京时间，近30天含今天，已观察直播'+(preferred==='entry'?'入场':'互动')+'；不表示在线；私信时段未读取）');
    if(dateDay===todayDate&&get(get(card,'opportunity'),'status')==='eligible')set('聊天机会','已满足现有机会门槛；本轮提醒时间与取消在工作台处理');
    set('账号归属',verified?'当前账号与本机登记范围一致':'已保存账号依据；当前登录待核验');
    set('私聊范围',verified?'当前私聊与本机登记对象一致':'已保存私聊依据；当前会话待核验');
    if(typeof grade==='string'&&/^[SABCDE]$/.test(grade))set('SABCDE等级',grade+'（依据已保存资料）');
    set('分层依据',tierText(get(card,'tier_explanation'),grade));
    const freshness=get(card,'tier_freshness'),day=get(freshness,'required_day'),today=new Date(Date.now()+8*3600000).toISOString().slice(0,10);
    if(verified)set('SABCDE等级','未知，榜单所属日待核验');
    if(get(freshness,'format')==='rolling-rank-state-1'&&typeof day==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(day)){
      const current=day===today&&get(freshness,'state')==='ready';
      set('榜单日更',current?day+' 三榜已齐；每日滚动更新':today+' 今日待更新；本次资料读取日 '+day);
      set('SABCDE等级',current&&/^[SABCDE]$/.test(grade)?grade+'（'+day+'榜）':'未知，今日榜或资格资料不足');
      if(day!==today){set('分层依据','今日待更新；上次读取日 '+day+'，原等级仅作历史参考');set('已保存分层参考',day+'记录等级：'+(/^[SABCDE]$/.test(grade)?grade:'未知')+'；'+tierText(get(card,'tier_explanation'),grade));}
    }
    const reference=get(card,'saved_tier_reference'),explanation=get(reference,'explanation'),prior=get(reference,'rankings');
    if(get(reference,'scope')==='saved_snapshot'&&Array.isArray(prior)&&prior.length>0&&prior.length<=3){
      const priorGrade=get(explanation,'final_grade');
      const parts=[];for(let i=0;i<prior.length;i++){const rank=get(prior,String(i)),label={contribution:'贡献',comment:'评论',watch:'观看'}[get(rank,'kind')];if(label)parts.push(label+'：'+rankText(rank));}
      set('已保存分层参考',(['S','A','B','C','D','E'].includes(priorGrade)?priorGrade:'未知')+'；仅对应下列已保存榜期，不用于当前处理队列。正式切片资格按当前登记。'+tierText(explanation,priorGrade)+'；'+parts.join('；'));
    }
    const live=get(card,'live'),sessions=get(live,'sessions');
    if(get(live,'format')==='v1-live-profile-1'&&get(live,'account_key')===get(binding,'account_key')&&
       get(live,'person_key')===get(binding,'person_key')&&get(live,'state')==='observed'&&
       get(live,'scope')==='saved_contact_observed_sessions'&&get(live,'complete')===false&&
       get(live,'tier_from_session_metrics')===false&&Array.isArray(sessions)&&sessions.length>0&&sessions.length<=20){
      const first=get(sessions,'0');
      if(get(first,'basis')==='single_session'){
        const start=dateText(get(first,'started_lower_utc')),end=dateText(get(first,'ended_upper_utc'));
        const exact=get(first,'started_lower_utc')===get(first,'started_upper_utc')&&get(first,'ended_lower_utc')===get(first,'ended_upper_utc');
        const revision=get(first,'revision_id');
        set('单场来源',start+' 至 '+end+'（'+(exact?'来源边界':'来源时间范围，边界非精确')+'）；采集截止 '+dateText(get(first,'captured_at_utc'))+'；修订 '+(typeof revision==='string'&&revision.length<=256?revision:'未知')+'；按采集时间选取的一场，不等于全量或最近结束的一场');
        const metrics=get(first,'metrics');
        for(const [key,label] of [['contribution','礼物贡献'],['comment_count','评论'],['watch_seconds','观看']])
          set(label,metricText(get(metrics,key)));
        const likes=get(metrics,'like_count'),value=get(likes,'value');
        if(get(likes,'unit')==='count'&&typeof value==='string'&&/^(?:0|[1-9][0-9]{0,29}|10{30})$/.test(value))set('点赞',metricText(likes));
      }
    }
    const rankings=get(card,'rankings');
    if(Array.isArray(rankings)&&rankings.length<=3){
      for(const [kind,label] of [['contribution','贡献榜'],['comment','评论榜'],['watch','观看榜']]){
        const matches=[];
        for(let index=0;index<rankings.length;index++){const rank=get(rankings,String(index));if(get(rank,'kind')===kind)matches.push(rank);}
        if(matches.length!==1)continue;
        set(label,rankText(matches[0]));
      }
    }
    return Object.freeze({title:verified?'当前对象资料（登记范围内已核对）':'已保存资料（当前页面待核验）',summary_state:'saved_binding',
      rows:Object.freeze(rows.map(row=>Object.freeze(row)))});
  }
  function mount(document){
    const element=document.createElement('article');element.className='fan-profile';element.setAttribute('aria-label','当前对象资料');
    const heading=document.createElement('h3');heading.textContent='当前对象资料';
    const list=document.createElement('dl'),values=[],sections={},panes=[];
    list.className='profile-overview';
    const groups=[['资料与标签',['正式档案','正式标签','地区与时区','日期与期限','当地时间','关注与互关','取关变化']],
      ['30天分层与原榜',['分层依据','已保存分层参考','贡献榜','评论榜','观看榜']],
      ['近7天直播统计',['统计窗口','窗口覆盖','近7天支持','近7天观看','近7天评论','近7天到场','统计依据','明显参与回落','直播常见时段','聊天机会']],
      ['已保存单场参考',['单场来源','礼物贡献','评论','点赞','观看','陪伴']],
      ['身份与历史来源',['账号归属','私聊范围','历史身份','有效历史记录','涉及会话','导出来源']]];
    element.append(heading,list);
    for(const [label,keys]of groups){
      const section=document.createElement('details'),title=document.createElement('summary'),body=document.createElement('dl');
      const page=label==='资料与标签'?'profile':label==='身份与历史来源'?'source':'live';
      section.className='profile-section';section.open=label==='资料与标签'||label==='近7天直播统计';title.textContent=label;
      panes.push({element:section,page});section.setAttribute('data-profile-page',page);
      section.append(title,body);element.append(section);for(const key of keys)sections[key]=body;
    }
    for(const [label] of project().rows){
      const name=document.createElement('dt');name.textContent=label;
      const cell=document.createElement('dd');cell.className='fan-profile-value';
      if(['分层依据','已保存分层参考','贡献榜','评论榜','观看榜'].includes(label)){
        const details=document.createElement('details'),summary=document.createElement('summary'),value=document.createElement('p');
        details.append(summary,value);cell.append(details);values.push({value,summary});
      }else values.push({value:cell,tags:label==='正式标签'});
      (sections[label]||list).append(name,cell);
    }
    function render(card){
      heading.textContent=card.title;
      for(let i=0;i<values.length;i++){
        const text=card.rows[i][1],item=values[i];item.value.textContent=text;
        if(item.summary)item.summary.textContent=text.split('；')[0];
        if(item.tags&&card.summary_state==='saved_binding'){
          item.value.textContent='';const unknown=[],known=[];
          for(const part of text.split('；'))(/未记录|未知/.test(part)?unknown:known).push(part.trim());
          for(const part of known){const chip=document.createElement('span');chip.className='profile-tag';chip.textContent=part;item.value.append(chip);}
          if(unknown.length){const more=document.createElement('details'),label=document.createElement('summary'),body=document.createElement('p');label.textContent=known.length?'其余 '+unknown.length+' 项待补充':'标签待补充';body.textContent=unknown.join('；');more.append(label,body);item.value.append(more);}
        }
      }
      element.setAttribute('data-dsh-profile-summary',card.summary_state);return card;
    }
    let lastSaved=null,lastDay=null;
    function show(view){lastSaved=null;return render(project(view));}
    function showSaved(result,binding,qualified){lastSaved={result,binding,qualified};lastDay=new Date(Date.now()+8*3600000).toISOString().slice(0,10);return render(projectSaved(result,binding,qualified));}
    // A long-lived identity card must stop presenting yesterday's grade as current.
    const timer=document.defaultView?.setInterval(()=>{const day=new Date(Date.now()+8*3600000).toISOString().slice(0,10);if(lastSaved&&day!==lastDay){lastDay=day;render(projectSaved(lastSaved.result,lastSaved.binding,lastSaved.qualified));}},60000);
    function setPage(page){if(!['profile','live','source'].includes(page))return;for(const item of panes)item.element.hidden=item.page!==page;}
    show();return {element,show,showSaved,setPage,dispose(){lastSaved=null;if(timer!==undefined)document.defaultView.clearInterval(timer);}};
  }
  return {project,projectSaved,mount};
});
