(function (root, factory) {
  const node=typeof module==='object'&&module.exports;
  const value = factory(node?require('../identity/history_summary.js'):root.DSHHistorySummary,node?require('./fan_profile.js'):root.DSHFanProfile,node?require('./profile_editor.js'):root.DSHProfileEditorUI,node?require('./chat_layout.js'):root.DSHChatLayout);
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DSHSidebar = value;
})(globalThis, function (H,Profile,Editor,Layout) {
  'use strict';
  function mount(document, capability, options={}) {
    const host = document.createElement('aside'); host.setAttribute('data-dsh-owned','true');host.setAttribute('data-dsh-build','F-autostart-sidebar-20261019');
    const shadow = host.attachShadow({mode:'closed'});
    const style = document.createElement('style');
    style.textContent = `
      :host{--bg:#232429;--surface:#2d2f36;--line:#41434d;--text:#f2f3f5;--muted:#b3b6c4;--accent:#ff6481;
        position:fixed;right:var(--dsh-right,12px);top:var(--dsh-top,76px);z-index:2147483000;font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif;color:var(--text);color-scheme:dark}
      :host([data-dsh-theme="light"]){--bg:#fff;--surface:#f5f6fa;--line:#e0e3eb;--text:#242634;--muted:#686d7f;--accent:#bd3452;color-scheme:light}
      *{box-sizing:border-box}[hidden]{display:none!important}button,input,textarea,select{font:inherit;color:inherit}
      button{border:1px solid var(--line);background:var(--surface);padding:7px 10px;border-radius:8px;cursor:pointer;min-height:36px}
      button:hover{border-color:var(--accent)}button:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}button:disabled{opacity:.5;cursor:default}
      .sidebar-panel{width:336px;max-width:calc(100vw - 24px);height:min(570px,calc(100dvh - 300px));min-height:190px;display:flex;flex-direction:column;background:var(--bg);border:1px solid var(--line);border-radius:14px;box-shadow:0 8px 30px #0003;overflow:hidden}
      :host([data-dsh-layout="docked"]) .sidebar-panel{height:var(--dsh-height,calc(100dvh - 100px));border-radius:12px;box-shadow:none}
      .sidebar-heading{display:flex;align-items:center;gap:8px;padding:14px 14px 8px}.sidebar-heading h2{font-size:17px;letter-spacing:.03em;margin:0;flex:1}.sidebar-heading button{font-size:12px;min-height:30px;padding:3px 7px}
      .sidebar-caption{font-size:12px;color:var(--muted);margin:0;padding:0 14px 10px}.sidebar-tabs{display:flex;gap:4px;padding:0 12px 10px;border-bottom:1px solid var(--line)}
      .sidebar-tabs button{flex:1;border-color:transparent;background:transparent}.sidebar-tabs button[aria-pressed="true"]{background:var(--surface);color:var(--accent);font-weight:650;border-color:var(--line)}
      .sidebar-scroll{overflow:auto;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:var(--line) transparent;min-height:0;padding:12px 14px 18px}
      p{line-height:1.7;margin:8px 0}.sidebar-status{font-size:12px;color:var(--muted);padding:9px 10px;background:var(--surface);border-radius:8px;margin:0 0 12px}
      .fan-profile h3{font-size:12px;font-weight:400;color:var(--muted);margin:0 0 10px}.fan-profile dl{display:grid;grid-template-columns:82px minmax(0,1fr);gap:9px 12px;margin:0;line-height:1.65}.fan-profile dt{color:var(--muted);font-size:12px}.fan-profile dd{margin:0;overflow-wrap:anywhere}
      .profile-overview{background:var(--surface);border-radius:10px;padding:12px!important;margin-bottom:14px!important}.profile-overview dd:first-of-type{color:var(--accent);font-size:16px;font-weight:650}
      .profile-section{border-top:1px solid var(--line);padding:12px 0}.profile-section>summary{font-weight:600;cursor:pointer;min-height:26px}.profile-section>dl{padding-top:12px}
      summary{overflow-wrap:anywhere}.fan-profile details p{font-size:12px;color:var(--muted)}.sidebar-sources{font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:8px}
      .profile-tag{display:inline-block;border:1px solid var(--line);border-radius:5px;padding:2px 7px;margin:0 4px 5px 0;font-size:12px;background:var(--surface)}
      .profile-editor{margin-top:12px}.profile-editor>button{width:100%;border-color:var(--accent)}.profile-editor details>div{border-top:1px solid var(--line);padding:10px 0}.profile-editor label{display:block;margin:6px 0}
      .profile-editor textarea,.profile-editor select,.profile-editor input:not([type="checkbox"]){display:block;width:100%;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:8px;margin-top:5px}
      .profile-editor button{margin:3px 3px 3px 0;padding:5px 8px;font-size:12px}.profile-editor p{font-size:12px;color:var(--muted)}
      @media(max-height:600px){.sidebar-panel{height:calc(100dvh - 160px);min-height:150px}}
    `;
    const button = document.createElement('button'); button.type = 'button'; button.textContent = '助手 · 来源待核验（F盘试点）'; button.setAttribute('aria-expanded','false');
    button.setAttribute('aria-label','展开粉丝资料侧栏');
    const panel = document.createElement('section'); panel.hidden = true;panel.className='sidebar-panel';panel.setAttribute('aria-label','聊天辅助资料');
    const heading=document.createElement('header'),title=document.createElement('h2'),theme=document.createElement('button'),close=document.createElement('button'),caption=document.createElement('p'),tabs=document.createElement('nav'),scroll=document.createElement('div'),sources=document.createElement('div');
    heading.className='sidebar-heading';title.textContent='粉丝资料';theme.type=close.type='button';theme.textContent='浅色';theme.setAttribute('aria-label','切换侧栏明暗主题');close.textContent='收起';close.setAttribute('aria-label','收起粉丝资料侧栏');heading.append(title,theme,close);
    caption.className='sidebar-caption';caption.textContent='边聊边看 · 消息仍在抖音发送';tabs.className='sidebar-tabs';tabs.setAttribute('aria-label','资料栏目');scroll.className='sidebar-scroll';sources.className='sidebar-sources';sources.hidden=true;
    const status = document.createElement('p'); status.textContent = '当前页面的账号、对象和消息来源尚未核验。自动采集与个人发送归属不可用。';
    status.className='sidebar-status';
    const note = document.createElement('p'); note.textContent = '已配对的扩展会自动连接本机工作台；主动断开后需在扩展中恢复。历史查询仅显示已存储事实。';
    const history=document.createElement('p');history.textContent='历史导出记录：未知。';
    const profile=Profile.mount(document);
    let editorBinding=null;
    const editor=options.editor&&Editor?Editor.mount(document,options.editor,result=>{
      if(result.profile&&editorBinding&&['account_key','person_key','conversation_key'].every(k=>result.profile.binding?.[k]===editorBinding[k]))profile.showSaved(result.profile,editorBinding,true);
      else{profile.show();note.textContent='修改已保存，最新资料卡暂未读回；请关闭并重新打开编辑核对已存值。';}
    }):null;
    const tabButtons=[];
    function selectPage(page){profile.setPage(page);sources.hidden=page!=='source';for(const item of tabButtons)item.button.setAttribute('aria-pressed',String(item.page===page));}
    for(const [page,label]of [['profile','资料'],['live','直播'],['source','来源']]){const b=document.createElement('button');b.type='button';b.textContent=label;b.addEventListener('click',()=>selectPage(page));tabs.append(b);tabButtons.push({page,button:b});}
    sources.append(history,note);scroll.append(status,profile.element);if(editor)scroll.append(editor.element);scroll.append(sources);panel.append(heading,caption,tabs,scroll);shadow.append(style,button,panel);document.body.append(host);selectPage('profile');
    const layout=Layout?.mount(document,host,mode=>{caption.textContent=mode==='docked'?'边聊边看 · 消息仍在抖音发送':'悬浮显示 · 可随时收起查看聊天';});
    const toggle = () => { panel.hidden = !panel.hidden;button.hidden=!panel.hidden;button.setAttribute('aria-expanded',String(!panel.hidden));layout?.setOpen(!panel.hidden); };
    button.addEventListener('click',toggle);
    close.addEventListener('click',()=>{if(!panel.hidden){toggle();button.focus?.();}});
    let light=false;theme.addEventListener('click',()=>{light=!light;host.setAttribute('data-dsh-theme',light?'light':'dark');theme.textContent=light?'深色':'浅色';});
    // Default open on real DOMs; no request, message read or edit is triggered.
    if(document.defaultView)toggle();
    // A trusted, context-checked caller may render pilot metadata only. This
    // method itself neither inspects the native DOM nor queries a database.
    const showIdentityPilot = view => {
      if(editor)editor.clear();editorBinding=null;
      // Fixed local diagnostics only; never put identifiers or content in the DOM.
      host.setAttribute('data-dsh-profile-display','pending');
      const recalled=view&&view.state==='recalled_candidate'&&view.current_identity==='unverified'&&view.owner==='unverified'&&view.private_context==='unverified'&&view.formal_binding===false;
      const selected=view&&view.state==='selection_candidate'&&view.current_identity==='unverified'&&view.owner==='unverified'&&view.private_context==='unverified'&&view.formal_binding===false;
      const resumed=view&&view.state==='resumed_candidate'&&view.current_identity==='unverified'&&view.owner==='unverified'&&view.private_context==='unverified'&&view.formal_binding===false;
      const state=resumed?'resumed_candidate':selected?'selection_candidate':view&&view.state==='candidate'?'candidate':recalled?'recalled_candidate':'pending';
      // Projection has no person_key, editor, network call, or database write.
      profile.show(view);
      const summary=H&&H.fromResponse(view,state!=='pending'&&view.historical_identity==='sender_candidate'&&view.owner==='unverified'&&view.private_context==='unverified'&&view.formal_binding===false&&!['login_required','service_unavailable'].includes(view.connection));
      const available=summary&&summary.state==='available';
      host.setAttribute('data-dsh-history-summary',available?'available':'unknown');
      history.textContent=available?'历史导出中该候选发送者的有效记录：'+summary.message_count+'条，涉及'+summary.conversation_count+'个会话。包含各类会话，不等于私聊次数；仅统计导出中标识有效的记录，未核验完整性及去重。':'历史导出记录：未知。资料未关联或尚未核实时，不按0条计算。';
      host.setAttribute('data-dsh-pilot-state',state);
      host.setAttribute('data-dsh-history',state!=='pending'&&view&&['sender_candidate','zero','conflict'].includes(view.historical_identity)?view.historical_identity:'not_checked');
      host.setAttribute('data-dsh-header-bound',view&&typeof view.header_bound==='boolean'?String(view.header_bound):'unknown');
      const declared=view&&view.historical_export_source_statement==='user_confirmed'&&!['service_unavailable','login_required'].includes(view.connection);
      host.setAttribute('data-dsh-export-source',declared?'user_confirmed':'unknown');
      const label = view && ['甲','乙'].includes(view.sample) ? '样本'+view.sample : '当前对象';
      const states = {sender_candidate:'历史发送者候选命中（未正式绑定）',zero:'历史发送者未命中',conflict:'历史身份候选冲突'};
      const result = view && state === 'candidate' && states[view.historical_identity];
      const recalledStates={sender_candidate:'此页面标识曾核对到历史发送者候选（未正式绑定）',zero:'此页面标识上次核对未命中历史发送者',conflict:'此页面标识上次核对存在历史候选冲突'};
      button.textContent = '助手 · 身份核对（F盘试点）';
      const selectedStates={sender_candidate:'页面提供的标识与历史记录匹配到候选；当前聊天对象尚未核验。',zero:'页面提供的标识未匹配到历史发送者；当前聊天对象尚未核验。',conflict:'页面提供的标识匹配到多个历史候选；当前聊天对象尚未核验。'};
      status.textContent = (selected||resumed)&&selectedStates[view.historical_identity]?selectedStates[view.historical_identity]:recalled&&recalledStates[view.historical_identity]?'当前页面身份未核验；'+recalledStates[view.historical_identity]+'。':result?label+'：'+result+'。':'当前聊天身份尚未核验，暂不能匹配历史候选。';
      const connection={login_required:'本机连接尚未恢复；若已主动断开，请在扩展中恢复。',service_unavailable:'本机服务暂不可用，保留配对并等待恢复。',connected:'本机核对服务已连接。',workbench_connected:'本机工作台已连接；当前聊天与已保存资料尚未建立可靠对应。'};
      const firstCheck=state==='pending'&&view&&view.connection==='connected'&&view.header_bound===true?'正常选择聊天后自动核对历史候选；尚未核实的资料继续显示未知。':'';
      const sourceNote=declared?'历史导出来源已由你确认；当前登录账号与私聊范围未验证。':'本人账号与私聊范围未验证。';
      note.textContent = ((view&&connection[view.connection])||'')+(view&&view.header_bound===false?'当前聊天来源尚未核实。':'')+firstCheck+sourceNote+'群消息不计作与我的私聊；不展示贡献或等级。';
    };
    const diagnosticEnums=Object.freeze({
      'metadata-status':['not-read','reading','unverified-metadata','store-missing','store-accessor','conversation-missing','conversation-accessor','metadata-unavailable'],
      'selection-ack':['none','sent','unbound','budget_exhausted','stale','pending','selection_candidate','rejected'],
      'resume-ack':['none','sent','unbound','stale','pending','resumed_candidate','rejected'],
    });
    const diagnostic=(key,value)=>{
      if(typeof key!=='string')return;
      if(key==='metadata-shape'){
        if(typeof value==='string'&&/^(?:not-read|b1:(?:unbound|unavailable)|b1:[yn][mda]{8}[01]{8}(?::p(?:[mda]{6}|unavailable))?)(?![\s\S])/.test(value))host.setAttribute('data-dsh-metadata-shape',value);
        return;
      }
      if(key==='nav-trace'){
        const pattern=/^[1-9][0-9]{0,2}:(native|replay):(entry|resume):(blank|expected|other):(fresh|same|replaced):(newer|same|older):(idle|pending_navigation|pending_profile|candidate|failed)(?::(link|typed|auto_bookmark|auto_subframe|manual_subframe|generated|auto_toplevel|form_submit|reload|keyword|keyword_generated|unknown):([01]{4}|unknown))?$/;
        if(typeof value==='string'&&(value===''||(value.length<=2400&&value.split(';').length<=16&&value.split(';').every(item=>pattern.test(item)))))host.setAttribute('data-dsh-nav-trace',value);
        return;
      }
      if(Object.hasOwn(diagnosticEnums,key)){
        if(typeof value==='string'&&diagnosticEnums[key].includes(value))host.setAttribute('data-dsh-'+key,value);
        return;
      }
      if(['generation','reset-reason','arm-ack','nav-phase','nav-reason','nav-passed'].includes(key)&&/^[A-Za-z0-9_-]{1,64}$/.test(String(value)))host.setAttribute('data-dsh-'+key,String(value));
    };
    // Internal consumer only. The pilot's candidate path never invokes it.
    // A reliable background producer and the caller's live-context guard are
    // still required; a saved card cannot attest the current Chrome page.
    let disposed=false;
    function showSavedProfile(result,binding,current,qualified=false){
      if(disposed)return;
      const live=()=>{try{return !disposed&&typeof current==='function'&&current()===true;}catch{return false;}};
      showIdentityPilot({state:'pending'});
      if(!live())return;
      const verified=qualified===true&&result.fixture===false;
      const card=profile.showSaved(result,binding,verified);
      if(!live()){showIdentityPilot({state:'pending'});return;}
      history.textContent='历史聊天正文未读取。';
      if(card.summary_state==='saved_binding'){
        host.setAttribute('data-dsh-profile-display','saved-binding');
        status.textContent=verified?'当前聊天对象、账号与私聊范围已按本机登记核对通过。':(result.fixture===true?'虚构数据接线验证。':'')+'已保存资料对应核对通过；当前Chrome聊天身份仍待核验。';
        if(verified)button.textContent='助手 · 当前对象资料';
        if(verified&&editor){editorBinding={...binding};editor.activate();}
        note.textContent='30天三榜用于SABCDE分层，单场数据独立展示。缺失指标保持未知；已保存分层参考不参与当前处理队列。';
      }
      return card;
    }
    return {capability,showIdentityPilot,showSavedProfile,diagnostic,dispose() { disposed=true;layout?.dispose();editor?.dispose();profile.dispose();button.removeEventListener('click',toggle); host.remove(); }};
  }
  return {mount};
});
