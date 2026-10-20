'use strict';
if (location.origin !== DSHApi.BASE) throw new Error('fixed_loopback_origin_required');
const api = DSHApi.createApi();
const authStatus = document.getElementById('auth-status');
const workspace = document.getElementById('workspace');
const loginForm = document.getElementById('login');
const webLoginForm = document.getElementById('web-login');
const credentialEntry = document.getElementById('credential-entry');
const searchForm = document.getElementById('search');
const accountSelect = document.getElementById('account');
const contactSelect = document.getElementById('contact');
const contactRetry = document.getElementById('contact-retry');
const more = document.getElementById('more');
const desk = globalThis.DSHDesk?.mount(document) || null;
const editorBook = new Map();
let loginLifecycle = 0;
let loginPending = false;
let activeReference = null;
const deletionPanel = DSHDeletion.mount(document.getElementById('deletion-panel'),api,document);
const importBinding = DSHImportBinding.mount(document.getElementById('import-binding'),api,document);
const fileImport = DSHFileImport.mount(document.getElementById('file-import'),api,document,{deletionButton:binding=>deletionPanel.button(binding)});
const activityPanel = DSHActivity.mount(document.getElementById('activity-panel'),api,document);
const diagnosticsPanel = DSHDiagnostics.mount(document.getElementById('diagnostics-panel'),api,document);
const contactStatistics = DSHOperations.statistics(document.getElementById('contact-statistics'),api,document);
let activeOpportunity=null;
let activeBirthdays=null;
const groupBrowser = document.getElementById('group-browser') && DSHGroups.mountDirectory ? DSHGroups.mountDirectory(document.getElementById('group-browser'),api,person=>run(()=>selectAndSearch(()=>{contactSelect.value=person;return controller.contactSelect(person);}),document.getElementById('contact-status')),{workbench:!!desk}) : null;
function node(tag,text,className) {const value = document.createElement(tag); if (text !== undefined) value.textContent = text; if (className) value.className = className; return value;}
const history = new DSHHistory.HistoryController(query => api.history(query),state => {
  const messages = document.getElementById('messages'); messages.replaceChildren();
  for (const item of state.items) {const li = node('li'); if(desk)li.dataset.direction=['in','out'].includes(item.direction)?item.direction:'unknown'; li.append(node('small',DSHHistory.messageLabel(item)),node('p',typeof item.body === 'string' ? item.body : '正文不可用')); const inspect=deletionPanel.button({account_key:state.query&&state.query.account_key,target_kind:'message',target_key:item.message_key});if(inspect)li.append(inspect);desk?.decorateMessage(li,item);messages.append(li);}
  const labels = {loading:'正在查询已保存消息…',empty:'没有匹配的已保存消息；这不证明平台没有消息或已回复。',ready:`已显示 ${state.items.length} 条已保存消息；完整覆盖仍未知。`,unavailable:'查询暂不可用，已显示的消息予以保留。',unauthorized:'会话已失效，已清除历史记录，请退出后重新登录。',forbidden:'当前账号没有查询权限，已清除历史记录。'};
  document.getElementById('history-status').textContent = labels[state.state]; more.hidden = !state.next_cursor; more.disabled = state.busy;
  desk?.history(state);
});
function searchHistory() {if(desk&&!contactSelect.value){history.clear();return Promise.resolve();}return history.select({account_key:accountSelect.value,person_key:contactSelect.value,keyword:searchForm.elements.keyword.value,day:searchForm.elements.day.value});}
async function selectAndSearch(select) {
  history.clear();
  fileImport.selectPerson(null);
  const stamp = history.generation;
  await select();
  // Controllers discard stale replies, but their promises still settle.
  // An older handler must not restart the current search and erase its pages.
  if (stamp === history.generation) return Promise.all([searchHistory(),fileImport.selectPerson(contactSelect.value||null)]);
}
const errors = {unauthorized:'凭据无效或会话已过期。',forbidden:'当前本机登记未授予此权限。',read_only:'当前为只读回退，不能保存资料。',editing_unavailable:'当前写入模式尚未启用或状态未核验；仅可查询。',stale_session:'会话已切换，请重新查询。',already_authenticated:'已有本机会话，请先退出。',conflict:'资料版本发生冲突，输入已保留，请查看最新版本后重新确认。',rebase_required:'请先查看并确认最新版本，再重新保存。',invalid_value:'资料未通过验证。输入已保留，请核对日期、时区、人物与字段值。'};
async function run(fn,status = authStatus) {try {await fn();} catch (e) {status.textContent = errors[e.message] || '本机服务暂不可用，输入予以保留，请检查后重试。';}}
function taskRows(target,rows,empty) {
  target.replaceChildren(); if (!rows.length) {target.append(node('li',empty,'empty-row')); return;}
  for (const item of rows) {
    const li = node('li'); const line = node('div'); line.append(node('strong',item.display_name || '显示名未知'));
    const reasons = Array.isArray(item.reasons) ? item.reasons.map(reason => ({reply:'待回',maintenance:'维护待办',spark:'火花待发'}[reason] || '状态待核对')) : [];
    line.append(node('span',reasons.length ? reasons.join(' · ') : '请核对来源与独立状态','muted'));
    const button = node('button','查看已保存档案'); button.type = 'button'; button.className = 'secondary';
    button.addEventListener('click',() => run(async () => {if (!controller.contacts.some(contact => contact.person_key === item.person_key)) throw new Error('forbidden'); contactSelect.value = item.person_key; await selectAndSearch(() => controller.contactSelect(item.person_key));}));
    li.append(line,button); target.append(li);
  }
}
function section(title,description) {if(desk)return desk.section(title,description);const value = node('section',undefined,'profile-section'); value.append(node('h3',title)); if (description) value.append(node('p',description,'muted')); return value;}
function appendLines(target,lines) {for (const [label,text] of lines) target.append(node('p',label+'：'+text));}
function infoValue(value) {if (value === null || value === undefined) return '未知'; if (typeof value === 'boolean') return value ? '已核验是' : '已核验否'; if (typeof value === 'number' || typeof value === 'string') return String(value); return null;}
function summaryFacts(target,data,keys) {
  if (!data || typeof data !== 'object') {target.append(node('p','来源未核验／尚无已保存结果。','muted')); return;}
  let found = false;
  for (const [key,label] of keys) {if (!Object.prototype.hasOwnProperty.call(data,key)) continue; const text = infoValue(data[key]); if (text === null) continue; target.append(node('p',label + '：' + text)); found = true;}
  if (!found) target.append(node('p','状态未知，不能从缺失资料推断可靠零或已完成。','muted'));
}
function input(labelText,name,value,type = 'text') {
  const label = node('label',labelText); const control = node('input'); control.name = name; control.type = type; control.value = value == null ? '' : String(value); control.required = true;
  if (type === 'number') {control.min = ['month','day'].includes(name)?'1':'0';control.max=name==='month'?'12':name==='day'?'31':'365'; control.step = '1';} else control.maxLength = name==='source_text'?10000:200; label.append(control); return label;
}
function fieldForm(field,value) {
  const controls = node('div',undefined,'field-controls'); const kind = DSHCD.fieldType(field);
  if (kind === 'text') {const textarea = node('textarea'); textarea.name = 'value'; textarea.value = value == null ? '' : String(value); textarea.rows = field === 'note' ? 4 : 2; textarea.maxLength = 10000; controls.append(textarea);}
  else if (kind === 'boolean') {const select = node('select'); select.name = 'value'; select.required = true; for (const [v,text] of [['','请选择明确值'],['true','是'],['false','否']]) {const option = node('option',text); option.value = v; select.append(option);} select.value = value === true ? 'true' : value === false ? 'false' : ''; controls.append(select);}
  else {
    const item = value || (field==='reminder_settings'?{vip:7,star:7,birthday:0,anniversary:0}:{});
    const fields = field === 'location' ? [['国家','country','text'],['城市','city','text'],['时区（如 Asia/Shanghai）','timezone','text']] : field === 'reminder_settings' ? [['VIP提前天数','vip','number'],['星守护提前天数','star','number'],['生日提前天数','birthday','number'],['周年提前天数','anniversary','number']] : field === 'birthday' ? [['月份','month','number'],['日期','day','number'],['原文依据','source_text','text']] : field === 'anniversary' ? [['人工确认起始日期','origin_date','date'],['原文依据','source_text','text']] : [['本期开始','start','date'],['本期结束（当天有效）','end','date'],['人工依据','source_text','text']];
    for (const [label,name,type] of fields) {
      if(name==='source_text'){const area=node('label',label);const quote=node('textarea');quote.name=name;quote.rows=2;quote.maxLength=10000;quote.value=item[name]||'';area.append(quote);controls.append(area);}else controls.append(input(label,name,item[name],type));
    }
    if (field === 'birthday') for (const [name,title,choices] of [['calendar','历法',[['unknown','未知，需核对'],['gregorian','公历'],['lunar','农历'],['lunar_leap','农历闰月，需核对']]],['subject','生日人物',[['unknown','人物未知'],['self','此联系人本人'],['other','其他人物']]]]) {const label = node('label',title); const select = node('select'); select.name = name; for (const [key,text] of choices) {const option = node('option',text); option.value = key; select.append(option);} select.value = item[name] || 'unknown'; label.append(select); controls.append(label);}
  }
  return controls;
}
function formValue(field,form) {
  return DSHCD.formValue(field,new FormData(form).entries());
}
function ownControls(root,callback) {for(const child of Array.from(root.children||[])){if(['INPUT','SELECT','TEXTAREA','BUTTON'].includes(String(child.tagName||child.tag||'').toUpperCase()))callback(child);ownControls(child,callback);}}
function editingAccess(account,field) {return typeof api.editingAccess==='function'?api.editingAccess(account,field):{allowed:false,reason:'unknown'};}
function addEditor(target,field,record,binding) {
  try {DSHCD.fieldType(field);} catch {return;}
  const key = `${binding.account_key}\u0000${binding.person_key}\u0000${field}`;
  let editor = editorBook.get(key); if (!editor || editor.state === 'saved') {editor = new DSHCD.FieldEditor(body => api.profileField(body),{...binding,field},record); editorBook.set(key,editor);}
  if(record&&record.state==='conflict'&&JSON.stringify(record.revision)!==JSON.stringify(editor.expectedRevision))editor.state='conflict';
  const details = node('details',undefined,'field-editor'); details.append(node('summary',DSHCD.fieldLabel(field) + '：' + DSHCD.fieldValue(field,record)));
  const state = node('p',desk&&editor.state==='edited'?'有未保存修改 · 输入保留在此联系人，需点击保存才会生效。':DSHCD.stateLabel(record && record.state) + ' · 修订依据已绑定当前字段。','muted'); details.append(state);
  if(desk&&editor.state==='edited')details.open=true;
  const form = node('form'); const controls = fieldForm(field,editor.value); form.append(controls);
  if(editor.rawDraft)ownControls(controls,control=>{if(control.name&&Object.prototype.hasOwnProperty.call(editor.rawDraft,control.name))control.value=editor.rawDraft[control.name];});
  const save = node('button','保存此字段'); save.type = 'submit'; const clear = node('button','明确清空此字段'); clear.type = 'button'; clear.className = 'secondary';
  const cancelClear=node('button','改为编辑具体值');cancelClear.type='button';cancelClear.className='secondary';
  const rebase = node('button','读取并核对最新字段版本'); rebase.type = 'button'; rebase.className = 'secondary'; rebase.hidden = editor.state !== 'conflict';form.append(save,clear,cancelClear,rebase);details.append(form);target.append(details);
  const access=editingAccess(binding.account_key,field);const accessNotice=node('p',access.allowed?'保存会产生此对象此字段的新修订，不改变身份或回复完成状态。':access.reason==='local_notes_only'?'当前仅开放备注，此字段暂不可保存。':access.reason==='read_only'?errors.read_only:access.reason==='forbidden'?errors.forbidden:errors.editing_unavailable,'muted');details.append(accessNotice);
  function applyControlState(){save.disabled=clear.disabled=cancelClear.disabled=!access.allowed;cancelClear.hidden=!editor.clear;ownControls(controls,control=>{control.disabled=!access.allowed||editor.clear;});}
  function choose(value,wantsClear=false){editor.rawDraft=null;editor.draft(value,wantsClear);controls.replaceChildren(...Array.from(fieldForm(field,value).children));applyControlState();state.textContent=wantsClear?'已选择明确清空；请确认版本后点击保存。':'已选择输入内容；不会自动保存，请确认版本后点击保存。';}
  cancelClear.addEventListener('click',()=>choose(record&&record.effective_value,false));
  function candidates(area,current){
    if(!current||!Array.isArray(current.revision)){area.append(node('p','字段版本格式不可用；不能确认保存。','notice'));return;}
    area.append(node('p','本次所列修订：'+(current.revision.length?current.revision.join('、'):'尚无字段修订')+'；共同版本：'+(current.common_revision||'无共同版本／未记录'),'muted'));
    for(const candidate of current.candidates||[]){const item=node('div',undefined,'conflict-candidate');appendLines(item,DSHCD.candidateLines(field,candidate));const pick=node('button',candidate.operation==='clear'?'选择此清空版本':'选择此版本作为输入');pick.type='button';pick.className='secondary';pick.disabled=!access.allowed;pick.addEventListener('click',()=>choose(candidate.operation==='clear'?'':candidate.value,candidate.operation==='clear'));item.append(pick);area.append(item);}
  }
  if(record&&record.state==='conflict'){const panel=node('div');panel.append(node('p','存在并发版本；共同有效值与各候选独立保留。选择只改变编辑输入，保存前仍需读取并确认最新版本。','notice'));candidates(panel,record);details.append(panel);}
  function draft(){editor.rawDraft=Object.fromEntries(new FormData(form).entries());try{editor.draft(formValue(field,form));}catch{if(editor.state!=='conflict'&&editor.state!=='saving')editor.state='edited';}if(desk&&editor.state==='edited')state.textContent='有未保存修改 · 输入保留在此联系人，需点击保存才会生效。';}
  controls.addEventListener('input',draft);controls.addEventListener('change',draft);
  if(field==='birthday')details.append(node('p','请明确人物与历法。2月29日、农历与闰月仍需核对提醒日期；不会从自由文本自动确认本人生日。','muted'));
  if(field==='anniversary')details.append(node('p','这里保存人工确认的起始日期；最早已保存聊天不能自动当作完整首聊。原文可留空表示尚无文字依据。','muted'));
  if(field==='reminder_settings')details.append(node('p','四类提醒独立，允许0至365天；未记录时显示7／7／0／0默认输入，点击保存后才写入。','muted'));
  if(field.startsWith('entitlement.')){
    const helper=node('details');helper.append(node('summary','由明确剩余天数核对结束日期'));const reference=node('input');reference.type='date';const count=node('input');count.type='number';count.min='0';count.max='3660';count.step='1';const basis=node('select');for(const [value,label] of [['','请选择天数口径'],['after_reference','距参照日还有N天（0为当天）'],['including_reference','含参照日共N天（至少1天）']]){const option=node('option',label);option.value=value;basis.append(option);}
    for(const [labelText,control] of [['明确参照日',reference],['来源所说的剩余天数',count],['剩余天数口径',basis]]){const label=node('label',labelText);label.append(control);helper.append(label);}
    const preview=node('p','先核对参照日与包含当天的口径；结果只填本表单结束日，仍需核对开始日和原文后保存。','muted');const convert=node('button','核对并采用结束日期');convert.type='button';convert.className='secondary';convert.disabled=!access.allowed;helper.append(preview,convert);details.append(helper);
    convert.addEventListener('click',()=>run(async()=>{if(!/^\d+$/.test(count.value))throw new Error('invalid_value');const end=DSHCD.remainingEnd(reference.value,Number(count.value),basis.value);ownControls(controls,control=>{if(control.name==='end')control.value=end;});editor.clear=false;applyControlState();draft();preview.textContent='结束日输入为'+end+'（当天有效），尚未保存；请核对本期开始及依据。';},preview));
  }
  applyControlState();
  async function saving(wantsClear) {
    const accountGeneration=controller.accountGeneration,personGeneration=controller.personGeneration;
    const stillSelected=()=>details.isConnected&&controller.accountGeneration===accountGeneration&&controller.personGeneration===personGeneration&&controller.account===binding.account_key&&controller.person===binding.person_key;
    if(!stillSelected())throw new Error('stale_session');
    const allowed=editingAccess(binding.account_key,field);if(!allowed.allowed)throw new Error(allowed.reason==='read_only'?'read_only':allowed.reason==='forbidden'?'forbidden':'editing_unavailable');
    if (editor.state === 'conflict') throw new Error('rebase_required');if(wantsClear)editor.draft('',true);else if(!editor.clear)editor.draft(formValue(field,form));save.disabled = clear.disabled = true; state.textContent = '正在保存此字段…';
    try {await editor.save(); state.textContent = '此字段已保存；资料保存不改变回复完成状态。'; if (stillSelected()) {await controller.refreshOverview(); if(stillSelected()) {if(groupBrowser)void groupBrowser.refresh();await controller.contactSelect(binding.person_key);}}}
    catch (error) {rebase.hidden = editor.state !== 'conflict'; throw error;} finally {applyControlState();}
  }
  form.addEventListener('submit',event => {event.preventDefault();run(() => saving(false),state);});clear.addEventListener('click',()=>{if(editor.state==='conflict'){choose('',true);return;}run(()=>saving(true),state);});
  let inspection=0;
  rebase.addEventListener('click',() => run(async () => {
    const stamp=++inspection;const latest=await api.sidebar(binding);if(stamp!==inspection||!details.isConnected)return;if(!DSHCD.sidebarMatches(latest,binding)||!latest.profile.fields)throw new Error('bad_response');
    const latestRecord=latest.profile.fields[field]||{state:'empty',value:null,effective_value:null,revision:[],candidates:[]};const panel=node('div');state.textContent='最新共同有效值：'+DSHCD.fieldValue(field,latestRecord)+'；输入继续保留。';candidates(panel,latestRecord);
    const confirm=node('button','保留我的输入，确认所列全部版本');confirm.type='button';confirm.className='secondary';confirm.disabled=!access.allowed;confirm.addEventListener('click',()=>{if(stamp!==inspection||!details.isConnected)return;editor.rebase(latestRecord);rebase.hidden=true;panel.remove();state.textContent='已确认所列字段版本；输入保持，请再点击保存完成CAS修订。';});panel.append(confirm);form.append(panel);
  },state));
  return {draft(value){choose(value);details.open=true;state.textContent='候选已填入，尚未保存；请核对人物、历法、原文和当前字段版本。';}};
}
let dailyTierGuard=null;
function renderProfile(value) {
  desk?.profile(value);
  if(desk)groupBrowser?.select?.(value?.person_key||null);
  dailyTierGuard=null;
  if(activeOpportunity){activeOpportunity.dispose();activeOpportunity=null;}
  if(activeBirthdays){activeBirthdays.dispose();activeBirthdays=null;}
  const previousReference=activeReference;activeReference=null;if(previousReference)previousReference.dispose();
  const target = document.getElementById('profile-body'); const status = document.getElementById('profile-status'); target.replaceChildren();
  if (!value) {status.textContent = '选择已保存联系人后查看；手动查询不核验当前抖音页面。'; return;}
  status.textContent = value.state === 'loading' ? '正在读取已保存档案…' : value.state === 'unavailable' ? '档案暂不可用，未保存输入仍保留在原对象。' : DSHCD.contactLabel(value.contact) + '。此处仅为人工查询。'; if (value.state !== 'ready') return;
  const data = value.data; const fields = data.profile && data.profile.fields || data.fields || {}; const binding = {account_key:value.account_key,person_key:value.person_key};
  const groupArea=section('共享分组','同人可属于多个自定义组；移出或删除分组保留联系人及历史。'),systemGroups=node('p');groupArea.append(systemGroups);target.append(groupArea);
  const accountGeneration=controller.accountGeneration,personGeneration=controller.personGeneration;
  const currentGroup=()=>groupArea.isConnected&&controller.accountGeneration===accountGeneration&&controller.personGeneration===personGeneration&&controller.account===binding.account_key&&controller.person===binding.person_key;
  DSHGroups.render(groupArea,api,binding,fields,currentGroup,async () => {if(currentGroup()){if(groupBrowser)void groupBrowser.refresh();await controller.contactSelect(binding.person_key);}});
  const grade=section('等级与来源'),gradeText=node('p'),freshText=node('p');grade.append(gradeText,freshText);
  dailyTierGuard=()=>{
    const fresh=DSHCD.tierFreshness(data.tier_freshness,Date.now()),known=['S','A','B','C','D','E'].includes(data.grade);
    const current=fresh.state==='ready';
    desk?.grade(data,fresh,current&&known?data.grade:null);
    gradeText.textContent='服务端最终等级：'+(current&&known?data.grade:'未知／待核验')+(fresh.day?'（读取日 '+fresh.day+'）':'');
    freshText.textContent=fresh.label+(!current&&known?'；上次记录等级 '+data.grade+' 仅作历史参考':'');
    const groups=(data.groups&&data.groups.system||[]).filter(key=>Object.hasOwn(DSHGroups.SYSTEM||{},key)&&(!'SABCDE'.split('').includes(key)||current));
    systemGroups.textContent='系统组：'+(groups.map(key=>DSHGroups.SYSTEM[key]).join('、')||'暂无已确认分组')+(current?'。':'；今日等级待更新。');
  };
  dailyTierGuard();appendLines(grade,DSHCD.tierLines(data.tier_explanation,data.grade));appendLines(grade,DSHCD.ruleLines(data));target.append(grade);
  const prior=data.saved_tier_reference;
  if(prior&&prior.scope==='saved_snapshot'&&prior.explanation&&Array.isArray(prior.rankings)&&prior.rankings.length){
    const saved=section('已保存30天分层参考','仅对应下列来源榜期，不用于当前处理队列。正式切片资格按当前登记。');
    saved.append(node('p','参考等级：'+(['S','A','B','C','D','E'].includes(prior.explanation.final_grade)?prior.explanation.final_grade:'未知／资料不足')));
    appendLines(saved,DSHCD.tierLines(prior.explanation,prior.explanation.final_grade));
    for(const rank of prior.rankings){const entry=node('div');appendLines(entry,DSHCD.rankLines(rank));saved.append(entry);}target.append(saved);
  }
  const live=section('直播数据 · 已保存场次');const liveValues=node('div');live.append(liveValues);DSHLive.render(liveValues,data.live,binding);target.append(live);
  const ranks = section('原榜位置','保留原榜、原单位、显示精度和来源窗口；未知不转换为0。'); const rankingItems = data.rankings; if (Array.isArray(rankingItems) && rankingItems.length) for (const rank of rankingItems) {const entry=node('div');appendLines(entry,DSHCD.rankLines(rank));ranks.append(entry);} else ranks.append(node('p','暂无可靠原榜来源。','muted')); target.append(ranks);
  const formal = section('正式资料','正式微信、维护、火花、会员及当地时间独立保存。自由词条不触发这些规则。');
  let birthdayEditor=null;
  for (const field of ['display_name',...Object.keys(DSHCD.TAGS).map(tag => 'formal_tag.' + tag),'location','birthday','anniversary','reminder_settings']) {const editor=addEditor(formal,field,fields[field],binding);if(field==='birthday')birthdayEditor=editor;}
  for (const field of Object.keys(fields).filter(key => key.startsWith('entitlement.'))) addEditor(formal,field,fields[field],binding);
  for (const type of ['vip','star']) {const add = node('button',type === 'vip' ? '登记独立VIP新一期' : '登记独立星守护新一期'); add.type = 'button'; add.className = 'secondary'; add.addEventListener('click',() => addEditor(formal,`entitlement.${type}.${crypto.randomUUID()}`,null,binding)); formal.append(add);} const localTime = data.dates && data.dates.local_time || data.local_time; if (localTime) summaryFacts(formal,localTime,[['time','当地时间'],['timezone','时区']]); target.append(formal);
  const week = section('近七天与日期预留','近七天使用已保存采集窗口；日期预留使用独立期限与原文依据。未知不转换为0。');appendLines(week,DSHCD.recentLines(data.recent));appendLines(week,DSHCD.dateLines(data.dates));appendLines(week,DSHCD.activityLines(data.slots,data.opportunity));target.append(week);
  const birthdays=section('生日候选核对','只读取已保存个人来信；旧历史未含最近对话，候选不覆盖人工资料。');target.append(birthdays);
  activeBirthdays=DSHBirthdays.mount(birthdays,api,binding,()=>controller.account===binding.account_key&&controller.person===binding.person_key,value=>{birthdayEditor?.draft(value);desk?.showBirthday();},document);
  const opportunity=section('定时聊天机会','默认18:30，机会独立于待回；取消仅当前轮，不代表已聊天。');target.append(opportunity);
  activeOpportunity=DSHOperations.opportunity(opportunity,api,binding,data.opportunity,()=>controller.account===binding.account_key&&controller.person===binding.person_key,()=>void controller.refreshOverview(),document);
  const notes = section('共享备注与自由词条','“微信回复”“维护”“XXX小号”等自由词条仅为文字，不建立正式标签、跨账号合档或贡献合并。'); addEditor(notes,'note',fields.note,binding); for (const field of Object.keys(fields).filter(key => key.startsWith('free_term.'))) addEditor(notes,field,fields[field],binding);
  const addTerm = node('button','添加独立自由词条'); addTerm.type = 'button'; addTerm.className = 'secondary'; addTerm.addEventListener('click',() => addEditor(notes,'free_term.' + crypto.randomUUID(),null,binding)); notes.append(addTerm); target.append(notes);
  const relations = section('可靠关系与变化','两个关注方向独立；未知不等于未关注，发现时间不冒充实际变化时间。');relations.append(node('p','当前接口仅支持可靠来源观测，尚无工作台关系人工确认表单；正式关系保持只读。备注或自由词条不能改变关注关系。','muted')); if (data.relations && data.relations.directions) for (const [direction,label] of [['fan_follows_me','对方关注我'],['i_follow_fan','我关注对方']]) relations.append(node('p',label + '：' + ({known_following:'可靠已关注',known_not_following:'可靠未关注',unknown:'未知'}[data.relations.directions[direction]] || '未知'))); summaryFacts(relations,data.relations,[['mutual','互关']]); if (data.relations && Array.isArray(data.relations.changes)) for (const change of data.relations.changes) {relations.append(node('p',({fan_follows_me:'对方取关我',i_follow_fan:'我取关对方'}[change.direction] || '方向待核对') + ' · 实际取关时间：' + (change.actual_at_utc || '未知') + '；发现时间：' + (change.discovered_at_utc || '未知')));} target.append(relations);
  const ai = section('AI只读参考','模型不可用时保持未知；参考仅供人工判断，引用可展开核对。');target.append(ai);
  activeReference=DSHAI.render(ai,api,binding,() => controller.account===binding.account_key && controller.person===binding.person_key && ai.isConnected);
  desk?.arrange(target);
}
const controller = new DSHCD.WorkbenchController(api,{
  contacts(state) {if(groupBrowser){if(state.state==='loading'||state.state==='empty')void groupBrowser.clear();else if(state.state==='ready')void groupBrowser.load(controller.account);}contactSelect.replaceChildren(); const all = node('option','此账号全部已保存消息'); all.value = ''; contactSelect.append(all); for (const item of state.items) {const option = node('option',DSHCD.contactLabel(item)); option.value = item.person_key; contactSelect.append(option);} contactRetry.hidden=state.state!=='unavailable';contactRetry.disabled=state.state!=='unavailable'||!controller.account;document.getElementById('contact-status').textContent = state.state === 'loading' ? '正在读取已保存联系人…' : state.state === 'unavailable' ? '联系人暂不可用，可点击“重新读取联系人”；无需重新登录。' : state.state === 'empty' ? '此账号没有可展示的已保存联系人；完整平台联系人覆盖仍未知。' : '仅从已保存联系人中查询，不自动切换抖音会话。';},
  contact:renderProfile,
  overview(value) {
    const data = value.data || DSHCD.overviewState(null); document.getElementById('coverage-status').textContent = value.state === 'unavailable' ? '概览暂不可用；S状态与团队覆盖未知。' : data.coverage_known ? '覆盖依据已核验；S待回始终固定可见，处理顺序仍由人决定。' : '消息覆盖或今日分层待核验，不能确认S清空或推荐下一件非S事项。';
    taskRows(document.getElementById('global-s'),data.global_s,data.coverage_known ? '已核验范围内当前没有可展示S待回；请核对覆盖窗口。' : '暂无可展示的可靠S条目；这不代表S清空。');
    document.getElementById('overview-status').textContent = value.state === 'loading' ? '正在读取独立事项…' : value.state === 'unavailable' ? '概览暂不可用。' : data.counts ? `已观察主清单 ${data.counts.observed_action} 人；同人多事项按一行显示。未知、等待、微信和机会各自独立。` : '人数未知；等待、微信、机会和未知单独保留。';
    const groups = document.getElementById('overview-groups'); groups.replaceChildren();
    for (const [label,items,empty] of [['抖音待回／维护／火花待发并集',data.union,'暂无已观察可执行项；不等于全团队完成。'],['已发待亮',data.waiting,'没有已观察的等待点亮项。'],['正式微信主联系',data.wechat,'没有已观察的正式微信项。'],['可选机会',data.optional,'暂无有依据的机会。'],['来源或状态未知',data.unknown,'没有可展示的未知条目；整体覆盖仍以核验状态为准。']]) {const group = section(label); const list = node('ul',undefined,'task-list'); taskRows(list,items,empty); group.append(list); groups.append(group);}
  }
});
Object.assign(errors,{invalid_login_code:'请粘贴完整的一次性工作台登录码。',web_login_expired:'本次登录码已使用或过期，请重新打开本机工作台登录窗口。',web_login_unavailable:'本机工作台登录窗口尚未启用，请由工程负责人检查本机服务。'});
function showLoginForms(visible) {loginForm.hidden=!visible;if(webLoginForm)webLoginForm.hidden=!visible;if(credentialEntry)credentialEntry.hidden=!visible;}
function setLoginPending(value) {
  loginPending=value;
  for(const form of [loginForm,webLoginForm])for(const button of form?.querySelectorAll?.('button[type="submit"]')||[])button.disabled=value;
}
function startLogin(login) {
  if(loginPending)return;
  const stamp=++loginLifecycle;setLoginPending(true);diagnosticsPanel.clear();importBinding.clear();contactStatistics.clear();
  authStatus.textContent='正在核对本机登记…';
  run(()=>completeLogin(login,stamp)).finally(()=>{if(stamp===loginLifecycle)setLoginPending(false);});
}
async function completeLogin(login,loginStamp) {
  const principal=await login();
  if(loginStamp!==loginLifecycle)return;
  const health=await api.health().catch(()=>null);
  if(loginStamp!==loginLifecycle)return;
  desk?.health(health);
  accountSelect.replaceChildren();
  for(const [index,key] of principal.account_keys.entries()){const option=node('option',desk?(health?.source_mode==='fictional'?'虚构主播 · 功能试用':'已授权账号 '+(index+1)+' · '+key.slice(-6)):key);option.value=key;accountSelect.append(option);}
  authStatus.textContent=desk?`已连接 · ${DSHCD.roleLabel(principal.role)} · 本机身份已核对`:`${principal.operator_key} · ${DSHCD.roleLabel(principal.role)} · ${principal.device_key}。真实页面来源仍待核验。`;
  showLoginForms(false);document.getElementById('logout').hidden=false;workspace.hidden=false;
  desk?.auth(true);
  activityPanel.login();diagnosticsPanel.login();contactStatistics.selectAccount(accountSelect.value);deletionPanel.selectAccount(accountSelect.value);
  if(!principal.account_keys.length){document.getElementById('contact-status').textContent='此会话没有已授权账号，请由本机管理员登记。';return;}
  await selectAndSearch(async()=>{const bindingSelection=importBinding.selectAccount(accountSelect.value);const importSelection=fileImport.selectAccount(accountSelect.value);await controller.accountSelect(accountSelect.value);await Promise.all([bindingSelection,importSelection]);});
}
loginForm.addEventListener('submit',event=>{
  event.preventDefault();if(loginPending)return;const values=new FormData(loginForm);const credential=String(values.get('credential'));loginForm.elements.credential.value='';
  startLogin(()=>api.login(String(values.get('operator')),String(values.get('device')),credential));
});
if(webLoginForm)webLoginForm.addEventListener('submit',event=>{
  event.preventDefault();if(loginPending)return;const code=String(new FormData(webLoginForm).get('code'));webLoginForm.elements.code.value='';
  startLogin(()=>api.loginWithCode(code));
});
document.getElementById('logout').addEventListener('click',() => {loginLifecycle++;setLoginPending(false); history.clear(); controller.clear(); fileImport.clear(); importBinding.clear(); activityPanel.clear(); diagnosticsPanel.clear(); contactStatistics.clear(); deletionPanel.clear(); editorBook.clear(); workspace.hidden = true; desk?.auth(false); showLoginForms(true); document.getElementById('logout').hidden = true; run(async () => {await api.logout(); authStatus.textContent = '已退出登录。工作台后台仍在运行；如需停止，请回到启动窗口按 Ctrl+C。';});});
contactRetry.addEventListener('click',()=>{
  if(workspace.hidden||contactRetry.hidden||contactRetry.disabled||!controller.account||accountSelect.value!==controller.account)return;
  return run(()=>selectAndSearch(()=>controller.accountSelect(accountSelect.value)),document.getElementById('contact-status'));
});
searchForm.addEventListener('submit',event => {event.preventDefault(); searchHistory();}); accountSelect.addEventListener('change',() => run(() => selectAndSearch(async () => {diagnosticsPanel.accountChanged(); contactStatistics.selectAccount(accountSelect.value); deletionPanel.selectAccount(accountSelect.value); const bindingSelection=importBinding.selectAccount(accountSelect.value); const importSelection=fileImport.selectAccount(accountSelect.value); await controller.accountSelect(accountSelect.value); await Promise.all([bindingSelection,importSelection]);}))); contactSelect.addEventListener('change',() => run(() => selectAndSearch(() => controller.contactSelect(contactSelect.value || null)))); more.addEventListener('click',() => history.load());
api.health().then(data => {document.getElementById('health').textContent = data.writes_enabled === true ? '本机服务在线；实际来源能力请分别核验。' : '本机服务在线；真实敏感事实写入尚未获准启用。';desk?.health(data);}).catch(() => {document.getElementById('health').textContent = '本机服务暂不可用。';desk?.health(null);});

// Recheck the business date without replacing unsaved profile editors.
let rankDisplayDay=new Date(Date.now()+8*3600000).toISOString().slice(0,10);
function checkRankDisplayDay(){
  const day=new Date(Date.now()+8*3600000).toISOString().slice(0,10);
  if(day===rankDisplayDay)return;
  rankDisplayDay=day;if(dailyTierGuard)dailyTierGuard();
  if(controller.account){controller.callbacks.overview({state:'loading',data:null});void controller.refreshOverview();if(groupBrowser)void groupBrowser.refresh();}
}
if(typeof window!=='undefined'&&typeof window.setInterval==='function'){
  const rankDayTimer=window.setInterval(checkRankDisplayDay,60000);
  window.addEventListener('pagehide',()=>window.clearInterval(rankDayTimer),{once:true});
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')checkRankDisplayDay();});
}
