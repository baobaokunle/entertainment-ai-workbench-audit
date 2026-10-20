(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.DSHDesk=value;})(globalThis,function(){
  'use strict';
  function mount(document){
    const get=id=>document.getElementById(id),layout=get('desk-layout');if(!layout)return null;
    const hero=document.createElement('div');hero.id='profile-summary';hero.className='profile-summary';get('profile-status').after(hero);
    let detail='profile',selected=null;
    function pressed(selector,key,value){for(const button of document.querySelectorAll(selector))button.setAttribute('aria-pressed',String(button.dataset[key]===value));}
    function view(value){get('desk-layout').hidden=value!=='desk';get('tools-view').hidden=value!=='tools';pressed('[data-view]','view',value);}
    function panel(value){layout.dataset.panel=value;pressed('[data-panel]','panel',value);}
    function details(value){detail=value;get('profile-body').hidden=value!=='profile';get('reference-body').hidden=value!=='reference';pressed('[data-detail]','detail',value);}
    for(const b of document.querySelectorAll('[data-view]'))b.addEventListener('click',()=>view(b.dataset.view));
    for(const b of document.querySelectorAll('button[data-panel]'))b.addEventListener('click',()=>{view('desk');panel(b.dataset.panel);});
    for(const b of document.querySelectorAll('[data-detail]'))b.addEventListener('click',()=>details(b.dataset.detail));
    function health(data){const banner=get('workspace-mode'),state=get('health');if(data?.source_mode==='fictional'){
      banner.textContent='功能试用 · 全部为虚构样本；保存写入独立测试工作区，不影响抖音或正式数据。';banner.dataset.mode='fictional';state.textContent='本机已连接 · 虚构测试';
    }else{banner.textContent=data?'已保存资料 · 来源与截止日期逐项核对；当前页面不代表平台实时聊天。':'数据环境未核对 · 请检查本机连接';banner.dataset.mode='unverified';state.textContent=data?'本机已连接 · 按登记权限使用':'本机连接暂不可用';}}
    function auth(value){get('auth-card').classList.toggle('authenticated',value);if(!value){selected=null;view('desk');panel('history');details('profile');}}
    function profile(value){get('reference-body').replaceChildren();const contact=value?.contact,name=contact?.display_name||'选择一个联系人',key=contact?JSON.stringify([value.account_key,value.person_key]):null;get('selected-name').textContent=name;get('selected-avatar').textContent=contact?'人':'◌';get('selected-context').textContent=value?.state==='ready'?'已保存档案 · 手动查询不核验当前抖音会话':value?.state==='loading'?'正在读取此人的档案…':'从左侧选择联系人；未知状态保持未知';get('search').querySelector('button').disabled=!contact;if(contact&&key!==selected)panel('history');selected=key;details(detail);}
    function section(title,description){const n=document.createElement('details');n.className='profile-section';n.dataset.section=title;n.open=['正式资料','共享备注与自由词条','AI只读参考','定时聊天机会'].includes(title);const s=document.createElement('summary');s.textContent=title;n.append(s);if(description){const p=document.createElement('p');p.className='muted';p.textContent=description;n.append(p);}return n;}
    function arrange(target){const reference=get('reference-body');for(const n of Array.from(target.children))if(['AI只读参考','定时聊天机会','生日候选核对'].includes(n.dataset.section))reference.append(n);const order=['共享备注与自由词条','正式资料','共享分组','等级与来源','原榜位置','直播数据 · 已保存场次','近七天与日期预留','可靠关系与变化'];for(const title of order){const part=Array.from(target.children).find(n=>n.dataset.section===title);if(part)target.append(part);}details(detail);}
    function grade(data,fresh,value){hero.replaceChildren();hero.hidden=false;const badge=document.createElement('strong'),info=document.createElement('div'),label=document.createElement('b'),source=document.createElement('small');badge.className='grade-badge';badge.dataset.grade=value||'unknown';badge.textContent=value||'—';label.textContent=value?'当前等级 '+value:'当前等级待核验';source.textContent=fresh.label;info.append(label,source);hero.append(badge,info);}
    function history(state){const empty=get('history-empty');empty.hidden=state.items.length>0;empty.querySelector('h3').textContent=!state.query?'从一个人开始':state.state==='loading'?'正在查询已保存消息':state.state==='unavailable'?'暂时无法读取历史':state.state==='unauthorized'||state.state==='forbidden'?'查询权限已失效':'暂无匹配的已保存消息';empty.querySelector('p').textContent='只查询已保存记录；这里的空白不代表平台没有对话。';}
    function decorateMessage(li,item){const [original,body,...actions]=Array.from(li.children),meta=document.createElement('details'),summary=document.createElement('summary'),stamp=document.createElement('small');meta.className='message-provenance';summary.textContent='来源与状态';meta.append(summary,original,...actions);for(const action of actions)action.addEventListener('click',()=>{view('tools');const section=get('deletion-panel').parentElement;section.open=true;section.scrollIntoView?.({block:'nearest'});});let time='时间未知';const lower=item.occurred_lower_utc,upper=item.occurred_upper_utc;
      if(['second','minute','date'].includes(item.time_precision)&&typeof lower==='string'&&typeof upper==='string'&&Number.isFinite(Date.parse(lower))&&Number.isFinite(Date.parse(upper))&&Date.parse(lower)<=Date.parse(upper)){time=lower===upper?new Date(lower).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+' 北京时间 · '+({second:'秒',minute:'分钟',date:'日期'}[item.time_precision])+'精度':'时间为范围，展开来源查看';}
      stamp.textContent=({in:'收到',out:'账号出站',system:'系统'}[item.direction]||'方向未知')+' · '+time;li.replaceChildren(stamp,body,meta);
    }
    function showBirthday(){details('profile');panel('profile');const formal=Array.from(get('profile-body').children).find(n=>n.dataset.section==='正式资料');if(formal)formal.open=true;}
    return {health,auth,profile(value){hero.replaceChildren();hero.hidden=true;profile(value);},grade,section,arrange,history,panel,decorateMessage,showBirthday};
  }
  return {mount};
});
