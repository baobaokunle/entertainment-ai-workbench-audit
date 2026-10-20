(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DSHLive=api;})(globalThis,function(){
  'use strict';
  const text=(value)=>typeof value==='string'&&value.length<=256?value:'未知';
  function metricText(metric){
    if(!metric||metric.state!=='known'||typeof metric.value!=='string'||metric.value.length>100||!/^\+?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(metric.value)||!Number.isFinite(Number(metric.value))||Number(metric.value)<0||Number(metric.value)>1e30||!['exact','display'].includes(metric.precision)||typeof metric.unit!=='string'||!metric.unit||metric.unit.length>80)return '未知';
    return metric.value+' '+metric.unit+(metric.precision==='display'?'（来源显示精度）':'（来源精确值）')+' · '+(metric.coverage==='complete'?'该指标覆盖完整':'该指标覆盖不完整');
  }
  function project(data,binding){
    const unavailable={state:'unknown',sessions:[],note:'尚无可展示的可靠场次；点赞与独立陪伴指标仍未知。'};
    if(!data||data.format!=='v1-live-profile-1'||!binding||data.account_key!==binding.account_key||data.person_key!==binding.person_key||data.scope!=='saved_contact_observed_sessions'||data.complete!==false||data.tier_from_session_metrics!==false||!Array.isArray(data.sessions)||data.sessions.length>20)return unavailable;
    if(data.state==='identity_unverified')return {...unavailable,note:'联系人身份尚未核实，直播资料保持未知。'};
    if(data.state!=='observed')return unavailable;
    const sessions=[];
    for(const row of data.sessions){
      if(!row||row.basis!=='single_session'||!row.metrics)return unavailable;
      const lines=[['场次窗口（UTC）',text(row.started_lower_utc)+' 至 '+text(row.ended_upper_utc)],['保存时间（UTC）',text(row.captured_at_utc)],['来源版本',text(row.source_id)+' / '+text(row.source_version)]];
      for(const [key,label]of [['contribution','礼物贡献'],['watch_seconds','观看'],['comment_count','评论'],['pk_contribution','PK贡献'],['attendance','出席']])lines.push([label,metricText(row.metrics[key])]);
      const likes=row.metrics.like_count;
      const likeText=likes&&likes.unit==='count'&&typeof likes.value==='string'&&/^(?:0|[1-9][0-9]{0,29}|10{30})$/.test(likes.value)?metricText(likes):'未知，未保存可靠指标';
      lines.push(['点赞',likeText],['陪伴','未知，不能以出席或观看替代']);
      sessions.push({lines});
    }
    return {state:sessions.length?'observed':'unknown',sessions,note:'仅展示已保存场次，保留原单位与来源精度；单场数据不代替30日榜，也不推算累计值。'+(data.more_observed_sessions?'当前仅显示最近记录。':'')};
  }
  function render(target,data,binding){
    const doc=target.ownerDocument;const create=(tag,value)=>{const node=doc.createElement(tag);if(value!==undefined)node.textContent=value;return node;};
    const value=project(data,binding);target.replaceChildren();target.append(create('p',value.note));
    for(let i=0;i<value.sessions.length;i++){
      const section=create('section');section.append(create('h3','已保存场次 '+(i+1)));const list=create('dl');
      for(const [name,detail]of value.sessions[i].lines)list.append(create('dt',name),create('dd',detail));
      section.append(list);target.append(section);
    }
    return value;
  }
  return {metricText,project,render};
});
