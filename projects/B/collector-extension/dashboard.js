import {NativeClient} from './engine.js';
import {EnhancedCollector as Collector,ChatBrowserSource as BrowserSource} from './chat-collector.js';
import {localDate,dateDays} from './core.js';
import {SourcePages} from './sources.js';
import {reviewRun,REVIEW_REASONS} from './review.js';
import {prepareBaseRepair} from './repair.js';
import {GapCollector,GAP_CLIENT} from './gaps.js';
let reviewing=false,reviewCancelled=false,reviewSupported=false,repairSupported=false,gapSupported=false,gapSelection=null,gapReport=null;
const $=id=>document.getElementById(id),host=new NativeClient();let connected=false,previewRun=null,previewOffset=0,preparing=false,prepareEpoch=0,lastFailure='',currentRun=null,previewTicket=0;
const names={account:'账号',session_list:'场次列表',session:'场次',summary:'场次指标',rank:'榜单',profile:'观众互动卡片',comment:'评论',gift:'礼物',speech:'讲话文字',trend:'趋势',chat:'聊天',legacy_chat:'历史文件',backend:'后台'};
const reasons={completed_requested_scope:'所选读取操作已结束（不代表全量）',cancelled:'已停止',budget:'已到读取预算，保留部分资料',source_changed:'账号或页面变化，已停止',disconnected:'连接中断',failed:'读取未全部成功，请查看失败原因'};
const notice=text=>{$('notice').textContent=text;};
function event(text){const li=document.createElement('li');li.textContent=text;$('events').prepend(li);while($('events').children.length>80)$('events').lastChild.remove();}
const explanations={BUDGET:'已达到本次读取上限，已有资料已保存。',SELECT_DATA_DOMAIN:'请至少选择一类要采集的资料。',NATIVE_DISCONNECTED:'本机存储连接已断开，请重新连接。',ACCOUNT_CHANGED:'登录账号或账号上下文发生变化，已停止，避免混入其它账号资料。',PAGE_DOCUMENT_CHANGED:'来源页面已刷新或重新加载，已停止。',PAGE_ROUTE_CHANGED:'来源页面已离开原页面，已停止。',SESSION_OWNER_CHANGED:'返回的场次不属于本次账号，已停止。',SESSION_CHANGED:'返回的场次与请求场次不同，已停止。',CONVERSATION_CHANGED:'会话信息发生变化，已停止。',MESSAGE_CONVERSATION_CHANGED:'消息所带会话键与请求会话不一致，已停止。',SOURCE_CHANGED:'来源一致性检查未通过，已停止。',BACKEND_LOGIN_REQUIRED:'主播后台跳转到了登录或其它页面，请点“主播后台 → 打开 / 登录”。',CHAT_LOGIN_REQUIRED:'抖音聊天跳转到了登录或其它页面，请点“抖音聊天 → 打开 / 登录”。',SOURCE_PAGE_LOADING:'来源页面还未加载完成，稍后再点开始。',OWNER_UNAVAILABLE:'尚未读到登录账号，请打开对应页面确认登录。',CHAT_SDK_UNAVAILABLE:'聊天页面尚未提供可用会话接口，请打开聊天页面检查登录；其它已保存资料保留。',DATE_RANGE_MAX_366:'请选择有效的直播日期，开始日期不晚于结束日期，最多 366 天。'};
Object.assign(explanations,{SOURCE_PERMISSION:'来源请求被拒绝（HTTP 401/403），具体登录、权限或适配原因尚未确定。',HTTP_401:'来源返回 HTTP 401：请求未通过身份验证，需检查登录状态及请求适配。',HTTP_403:'来源返回 HTTP 403：请求被拒绝，尚不能确定是权限还是请求适配。',BACKEND_ACCESS_BLOCKED:'后台读取被拒绝，已停止后续后台请求；如已勾选聊天，将独立继续。'});
Object.assign(explanations,{SOURCE_READ_FAILED:'此次读取失败，已有资料保留。',SOURCE_TIMEOUT:'此次读取超时。',SOURCE_SHAPE:'来源返回的资料结构尚未正确识别。',SOURCE_JSON:'来源响应未能解析。',SOURCE_HTTP:'来源未返回成功状态。',SOURCE_REJECTED:'来源接口拒绝了此次请求。',SOURCE_CONTENT_TYPE:'来源返回的内容格式不符合预期。'});
Object.assign(explanations,{REPAIR_ACCOUNT_CHANGED:'当前账号或应用范围与原任务不同，补查已停止。',REPAIR_ACCOUNT_UNKNOWN:'原任务没有足够的账号和应用证据，无法安全补查。',REPAIR_ACCOUNT_CONFLICT:'原任务存在账号范围冲突，无法自动补查。',REPAIR_NO_TARGETS:'没有可定位且尚未成功保存的失败场次；其它缺项仍需另查。',REPAIR_PARENT_UNAVAILABLE:'原任务已被删除，补查停止。',REPAIR_CURSOR:'补查清单不一致，已停止。',REPAIR_RECORD_MISMATCH:'原任务记录不连续或不一致，补查停止。',RETENTION_EXPIRED:'原任务已到访问期限，不能通过补查延长。'});
const describe=e=>{const text=explanations[e.detail]||explanations[e.code]||e.code;const d=e.diagnostic;if(!d)return text;const phases={fetch:'发送请求',redirect:'入口重定向',status:'响应状态',body:'读取响应',decode:'解析响应',projection:'识别字段'};return `${text}（${phases[d.phase]||'读取阶段未知'}${Number.isInteger(d.http_status)?`，HTTP ${d.http_status}`:''}）`;};
function buttons(){
 const active=!!collector.run||gaps.busy||preparing||reviewing;
 $('start').disabled=!connected||active;$('importFiles').disabled=!connected||active;$('stop').disabled=!(collector.run||gaps.busy||preparing);
 $('connect').disabled=active;$('openAnchor').disabled=active;$('openChat').disabled=active;
 $('refreshRuns').disabled=!connected||preparing||reviewing;
 $('viewCurrent').disabled=!connected||!currentRun||(collector.total===0&&!gaps.busy)||preparing||reviewing;
 $('gapContinue').disabled=!connected||active||!gapReport?.can_continue;
 $('gapStop').disabled=!gaps.busy;
 for(const el of document.querySelectorAll('#runs button'))el.disabled=!connected||preparing||reviewing||(el.dataset.action!=='view'&&(active||el.dataset.running==='true'));
 for(const el of document.querySelectorAll('#collect input,#collect select'))el.disabled=active;
 for(const el of document.querySelectorAll('.date-range input,#domains input'))el.disabled=active||!$('collectBackend').checked;
 $('readLimits').hidden=!$('limitReads').checked;for(const id of ['maxCalls','chatPages'])$(id).disabled=active||!$('limitReads').checked;
}
function renderCounts(e){const c=e.counts||{data:0,account:0,diagnostic:0,empty:0};$('recordCount').textContent=c.data;if(e.calls!==undefined)$('callCount').textContent=e.calls;$('missingCount').textContent=e.missing;$('storedBreakdown').textContent=`另存账号观察 ${c.account} 条、诊断说明 ${c.diagnostic} 条、空结果 ${c.empty} 条；上述项目不计入资料观察。资料观察不是去重人数或消息数。`;}
function notify(e){
 if(e.kind==='progress')renderCounts(e);
 if(e.kind==='chat_progress'){
  const title=e.stage==='loading'?'正在自动加载会话列表':e.stage==='finished'?'本轮聊天读取结束':'正在读取各会话历史';
  $('chatProgress').textContent=`${title}：已发现 ${e.discovered} 个会话，已处理 ${e.processed} 个，其中 ${e.sourceEnded} 个收到“没有更早消息”；新增消息观察 ${e.messageObservations} 条。列表全量仍未获来源证实。`;
 }
 if(e.kind==='stage'){$('taskState').textContent=({main:'正在读取主要资料',details:'主要资料读取结束，正在补充明细',chat:'正在读取聊天记录',base_repair:'正在补查指定失败场次的基础信息'})[e.stage]||'正在读取';}
 if(e.kind==='main_ready'){const text=`主要资料读取阶段已结束，已保存内容现在可查看；当前有 ${e.missing} 项读取失败或受限，不表示资料齐全。`;event(text);notice(text);}
 if(e.kind==='missing')event(`${names[e.domain]||e.domain}：${describe(e)}`);
 if(e.kind==='blocked'){event(explanations.BACKEND_ACCESS_BLOCKED);notice(explanations.BACKEND_ACCESS_BLOCKED);}
 if(e.kind==='sessions')event(`已发现 ${e.count} 个具有原生场次键的候选场次。`);
 if(e.kind==='skipped')event('已跳过一场符合所选条件的试播明细。');
 if(e.kind==='error'){lastFailure=describe(e);event(lastFailure);notice(`${lastFailure} 已保存资料保留在本机。`);}
 if(e.kind==='finished'){
  renderCounts(e);
  const outcome=e.reason==='failed'&&e.missing?(e.counts?.data?'已取得部分资料，仍有读取失败':'未取得所选业务资料，读取受阻'):(reasons[e.reason]||e.reason);
  $('taskState').textContent=outcome;notice(`${lastFailure||outcome} 已保存资料观察 ${e.counts?.data||0} 条；读取失败或受限 ${e.missing} 项。诊断说明不算业务资料，请查看下方原因。`);void runs().catch(()=>{});
 }
 buttons();
}
Object.assign(explanations,{
 CHAT_SCROLL_CONTAINER_UNAVAILABLE:'无法可靠定位聊天列表，已保留已加载会话；自动加载仍待修复。',
 CHAT_CONVERSATION_ID_MISSING:'部分会话没有可用的原生 ID，未按昵称归入聊天。',
 CHAT_HISTORY_END_UNKNOWN:'来源未明确说明历史是否结束，已保留缺口。',
 CHAT_HISTORY_LOAD_STALLED:'历史页持续没有新消息，已停止该会话并保留已有记录。',
 CHAT_CURSOR_NOT_ADVANCING:'历史翻页游标重复或缺失，已停止该会话，不能据此判断历史齐全。',
 CHAT_SHAPE:'聊天来源结构不匹配，已停止本轮聊天读取。'
});
const collector=new Collector({host,source:new BrowserSource(),notify});host.onDisconnect=()=>{connected=false;$('connection').textContent='连接已断开';$('connection').className='badge';collector.disconnected();gaps.disconnected();buttons();};
const gapPhaseNames={'-1':'核对原账号',0:'主要资料',1:'讲话文字',2:'观众互动卡片',3:'评论和礼物明细'};
const gaps=new GapCollector({host,source:new BrowserSource(),notify:e=>{
 if(e.kind==='phase')$('gapStatus').textContent=`正在补采：${gapPhaseNames[e.phase]||'原队列'}`;
 if(e.kind==='progress')$('gapProgress').textContent=`本次已处理 ${e.calls.toLocaleString()} 个请求，任务累计保存 ${e.records.toLocaleString()} 条观察（含账号、空返回及诊断）。`;
 if(e.kind==='error'){lastFailure=explanations[e.code]||e.code;notice(`补采停止：${lastFailure}。已保存进度保留。`);}
 if(e.kind==='finished'||e.kind==='stopped'){
  $('gapStatus').textContent=e.reason==='completed_requested_scope'?'本队列请求处理结束，空返回与失败仍保留为缺口':'补采已停止，未处理项可继续';
  gapReport=null;
  void runs().catch(()=>{});
  if(connected&&gapSelection)void inspectGap(gapSelection).catch(e=>notice(`核对未完成：${explanations[safeError(e)]||safeError(e)}`));
 }
 buttons();
}});
const safeError=e=>/^[A-Z_]{1,80}$/.test(e?.message)?e.message:'LOCAL_OPERATION_FAILED';
const guarded=fn=>async()=>{try{await fn();}catch(e){const code=safeError(e);notice(code==='CANCELLED'?'已取消准备，未启动读取。':`未完成：${explanations[code]||code}`);}finally{buttons();}};
async function connect(){await host.connect();const hello=await host.call('hello',{});reviewSupported=hello.review_supported===true;repairSupported=hello.base_repair_supported===true;gapSupported=hello.gap_repair_supported===true&&hello.gap_client===GAP_CLIENT;connected=true;$('connection').textContent='本机加密存储已连接';$('connection').className='badge ready';notice('存储已连接。选好日期后点“开始采集”，来源页面由工具自动准备。');await runs();}
$('connect').onclick=guarded(connect);
const pageStates={opening:'正在自动准备',ready:'页面已就绪，读取时核验账号',login:'需要登录或检查页面',loading:'仍在加载'};
const pages=new SourcePages(globalThis.chrome,{notify:({kind,state})=>{$(kind==='anchor'?'anchorState':'chatState').textContent=pageStates[state];}});
$('openAnchor').onclick=guarded(()=>pages.show('anchor'));$('openChat').onclick=guarded(()=>pages.show('chat'));
const scope=domains=>{const backend=domains.some(x=>!['chat','legacy_chat'].includes(x));return {domains:[...new Set(['account','capability',...domains,...(domains.some(x=>['session','summary','profile','comment','gift','speech','trend'].includes(x))?['session','session_list']:[])])],start_date:backend?$('startDate').value:localDate(),end_date:backend?$('endDate').value:localDate(),include_body:$('body').checked,retention_days:Number($('retention').value)};};
const options=()=>({anchorTab:null,chatTab:null,readAll:!$('limitReads').checked,maxCalls:Number($('maxCalls').value),chatPages:Number($('chatPages').value),skipTests:$('skipTests').checked});
$('start').onclick=guarded(async()=>{
 if(preparing||collector.run||gaps.busy||reviewing)return;
 const domains=[...($('collectBackend').checked?[...document.querySelectorAll('#domains input:checked')].map(x=>x.value):[]),...($('collectChat').checked?['chat']:[])];if(!domains.length)throw Error('SELECT_DATA_DOMAIN');
 const s=scope(domains),o=options();dateDays(s.start_date,s.end_date);
 $('chatProgress').textContent=domains.includes('chat')?'准备自动加载聊天列表，再读取各会话历史。':'本次未选择聊天记录。';
 if(!o.readAll&&(!Number.isInteger(o.chatPages)||o.chatPages<0||o.chatPages>2000))throw Error('CHAT_PAGE_BUDGET');
 if(!o.readAll&&(!Number.isInteger(o.maxCalls)||o.maxCalls<1||o.maxCalls>10000))throw Error('REQUEST_BUDGET');
 if(!Number.isInteger(s.retention_days)||s.retention_days<1||s.retention_days>30)throw Error('RETENTION_DAYS');
 const ticket=++prepareEpoch;preparing=true;lastFailure='';$('events').replaceChildren();$('taskState').textContent='正在准备来源页面';notice('正在自动打开或识别固定来源页面，无需选择标签页。');buttons();
 const check=()=>{if(ticket!==prepareEpoch)throw Error('CANCELLED');};
 try{
  Object.assign(o,await pages.prepare(domains,check));check();collector.source=new BrowserSource();
  const epoch=await collector.start(s,o);currentRun=collector.run.run_id;if(ticket!==prepareEpoch){await collector.stop();throw Error('CANCELLED');}
  preparing=false;$('taskState').textContent='正在读取与加密保存';notice('采集中。请保留此操作页与来源页，避免切换来源账号。');buttons();await collector.capture(epoch);
 }finally{preparing=false;if(!collector.run&&$('taskState').textContent==='正在准备来源页面')$('taskState').textContent='尚未开始采集';}
});
$('stop').onclick=guarded(async()=>{prepareEpoch++;if(gaps.busy)await gaps.stop();else if(collector.run)await collector.stop();else{$('taskState').textContent='已取消准备';notice('正在取消准备，不会自动开始采集。');}});
async function runs(){
 if(!connected)return;const data=await host.call('list_runs',{});$('runs').replaceChildren();$('runs').className='';
 if(!data.runs.length){$('runs').className='empty';$('runs').textContent='还没有本机任务。';return;}
 const table=document.createElement('table'),head=document.createElement('tr');
 for(const t of ['开始时间','状态','观察数（含诊断）','到期时间','操作']){const th=document.createElement('th');th.textContent=t;head.append(th);}table.append(head);
 for(const r of data.runs){
  const tr=document.createElement('tr');
  for(const t of [new Date(r.started_at).toLocaleString(),(r.gap_repair?'缺项补采 · ':r.base_repair?'基础补查 · ':'')+(reasons[r.state]||(r.state==='finished'?'读取操作结束（含诊断）':r.state)),String(r.count),new Date(r.expires_at).toLocaleString()]){const td=document.createElement('td');td.textContent=t;tr.append(td);}
  const cell=document.createElement('td');
  const actions=[['审核',async()=>audit(r.run_id)],['查看',async()=>{previewRun=r.run_id;previewOffset=0;await preview();}],['导出',async()=>{const out=await host.call('export',{run_id:r.run_id});notice(`已生成 ${out.record_count} 条观察的加密包：B/runtime/collector-private/${out.file_name}`);}],['删除',async()=>{if(confirm('删除本任务在 B 工具内的记录及已登记的导出包？不删除原 JSONL，也不影响 A。')){await host.call('delete_run',{run_id:r.run_id});if(previewRun===r.run_id)closePreview();await runs();}}]];
  if(gapSupported&&r.gap_repair)actions.splice(2,0,['核对缺项',()=>inspectGap(r.run_id)]);
  if(repairSupported&&!r.gap_repair&&r.domains?.includes('session'))actions.splice(2,0,[r.base_repair?'继续基础补查':'补查场次信息',()=>baseRepair(r.run_id)]);
  for(const [label,action]of actions){const b=document.createElement('button');b.className='secondary';b.textContent=label;b.onclick=guarded(action);b.dataset.running=String(r.state==='running');b.dataset.action=label==='查看'?'view':'other';b.disabled=!connected||reviewing||preparing||(label!=='查看'&&(r.state==='running'||!!collector.run||gaps.busy));cell.append(b);}
  tr.append(cell);table.append(tr);
 }$('runs').append(table);
}
async function baseRepair(parent){
 if(preparing||collector.run||gaps.busy||reviewing)return;
 const ticket=++prepareEpoch;preparing=true;lastFailure='';let plan=null;
 const check=()=>{if(ticket!==prepareEpoch)throw Error('CANCELLED');};
 $('events').replaceChildren();$('taskState').textContent='正在定位失败场次';buttons();
 try{
  const ready=await prepareBaseRepair(host,parent,{check,progress:p=>notice(`正在本机定位失败场次 ${p.processed.toLocaleString()} / ${p.total.toLocaleString()}；尚未访问抖音。`)});
  plan=ready.plan_id;check();if(!ready.target_count)throw Error('REPAIR_NO_TARGETS');
  event(`本次只补查 ${ready.target_count} 个场次的基础信息。${ready.unlocated_failures} 条场次失败没有定位，另有 ${ready.other_diagnostics} 条其它诊断，不在本次补查范围。`);
  const prepared=await pages.prepare(['session'],check);check();collector.source=new BrowserSource();
  const epoch=await collector.startBaseRepair(plan);plan=null;currentRun=collector.run.run_id;
  if(ticket!==prepareEpoch){await collector.stop();throw Error('CANCELLED');}
  preparing=false;notice('正在补查已定位的失败场次；原任务保留，期限不延长。不会重读榜单、聊天或内容明细。');buttons();
  await collector.captureBaseRepair(epoch,prepared.anchorTab);
 }finally{
  if(plan)await host.call('repair_discard',{plan_id:plan}).catch(()=>{});
  preparing=false;if(!collector.run&&$('taskState').textContent==='正在定位失败场次')$('taskState').textContent='基础补查未启动';
 }
}
$('refreshRuns').onclick=guarded(runs);
async function inspectGap(run){
 if(preparing||reviewing||collector.run||gaps.busy)return;
 preparing=true;gapSelection=run;gapReport=null;$('gapPanel').hidden=false;$('gapResult').replaceChildren();$('gapStatus').textContent='正在核对已有补采队列';buttons();
 try{
  const report=await host.call('gap_inspect',{run_id:run});gapReport=report;
  $('gapStatus').textContent=report.requests_finished?'队列中的请求已处理完；空返回和失败仍未取得资料。':'已有资料保留，可以继续尚未处理的请求。';
  $('gapResult').append(reviewTable(['资料范围','已取得','空返回','失败','尚未处理'],report.phases.map(p=>[gapPhaseNames[p.phase],p.positive,p.empty,p.failed,p.unattempted])));
  $('gapProgress').textContent=`已保存 ${report.records.toLocaleString()} 条观察（含账号、空返回及诊断）；沿用原到期时间 ${new Date(report.expires_at).toLocaleString()}。`;
  $('gapPanel').scrollIntoView({behavior:'smooth',block:'start'});
 }finally{preparing=false;buttons();}
}
$('gapContinue').onclick=guarded(async()=>{
 if(preparing||reviewing||collector.run||gaps.busy||!gapSelection||!gapReport?.can_continue)return;
 const selected=gapSelection;lastFailure='';gaps.source=new BrowserSource();currentRun=selected;
 const running=gaps.capture(selected,async check=>(await pages.prepare(['profile'],check)).anchorTab);
 $('gapStatus').textContent='正在恢复原队列并准备主播后台';buttons();await running;
});
$('gapStop').onclick=guarded(()=>gaps.stop());

function reviewTable(headers,rows){const table=document.createElement('table');const head=document.createElement('tr');for(const label of headers){const th=document.createElement('th');th.textContent=label;head.append(th);}table.append(head);for(const cells of rows){const tr=document.createElement('tr');for(const value of cells){const td=document.createElement('td');td.textContent=String(value);tr.append(td);}table.append(tr);}return table;}
async function audit(run){
 if(reviewing||collector.run||gaps.busy||preparing)return;
 if(!reviewSupported)throw Error('REVIEW_HOST_UPDATE_REQUIRED');
 reviewing=true;reviewCancelled=false;closePreview();$('reviewPanel').hidden=false;$('reviewResult').replaceChildren();$('reviewStatus').textContent='正在审核本机记录';$('cancelReview').disabled=false;buttons();
 try{
  const report=await reviewRun(host,run,{cancelled:()=>reviewCancelled,progress:p=>{$('reviewStatus').textContent=`正在审核 ${p.processed.toLocaleString()} / ${p.total.toLocaleString()} 条观察`;} });
  const c=report.counts;
  $('reviewStatus').textContent=`审核完成：有业务字段 ${c.data.toLocaleString()} 条，返回空列表 ${c.empty_response.toLocaleString()} 条，字段未识别 ${c.no_fields.toLocaleString()} 条，诊断 ${c.diagnostic.toLocaleString()} 条，账号 ${c.account.toLocaleString()} 条。`;
  const rows=[];const domains=new Set([...report.selected_domains.filter(d=>!['account','capability'].includes(d)),...Object.keys(report.domains)]);
  for(const domain of domains){const row=report.domains[domain];const n=row?.counts;rows.push([names[domain]||'其他',n?.data||0,n?.empty_response||0,n?.no_fields||0,n?.diagnostic||0,n?.account||0,row?'按记录审核，覆盖仍未知':'所选范围没有保存记录，不能判定无数据']);}
  $('reviewResult').append(reviewTable(['资料类型','有字段','空列表','未识别','诊断','账号','说明'],rows));
  const problems=[];
  for(const [domain,row]of Object.entries(report.domains))for(const [code,count]of Object.entries(row.reasons)){const reason=REVIEW_REASONS[code]||['其他读取问题','需要核对来源适配；不会自动重采。'];problems.push([names[domain]||'其他',reason[0],count,reason[1]]);}
  if(problems.length)$('reviewResult').append(reviewTable(['影响资料','原因','记录条数','判断与处理建议'],problems));
  const note=document.createElement('p');note.className='hint';note.textContent='原因可以重叠，不能相加当作失败次数。有业务字段只证明保存了字段，不证明身份、完整性或业务正确。未列出的限制仍可能存在。本审核不批准 A 接入，也不自动删除、修补或重新采集。';$('reviewResult').append(note);
 }catch(e){$('reviewStatus').textContent=e.message==='REVIEW_CANCELLED'?'审核已取消，原资料未改动。':`审核未完成：${explanations[safeError(e)]||'记录读取或结构检查未通过'}。不会显示部分汇总为完整报告。`;}
 finally{reviewing=false;$('cancelReview').disabled=true;buttons();}
}
$('cancelReview').onclick=()=>{reviewCancelled=true;$('cancelReview').disabled=true;};
const duration=ms=>{const seconds=Math.max(0,Math.round(ms/1000));return seconds>=3600?`${Math.floor(seconds/3600)}小时${Math.floor(seconds%3600/60)}分钟`:seconds>=60?`${Math.floor(seconds/60)}分钟${seconds%60}秒`:`${seconds}秒`;};
const phaseNames={account:'账号核验',history:'场次列表',base:'场次基础',summary:'场次指标',rank:'榜单',profile:'观众互动卡片',comment:'本场评论',gift:'本场礼物',speech:'本场讲话文字',trend:'趋势',chat_index:'已加载聊天列表',chat_load:'自动加载聊天列表',chat_cache:'已加载消息',chat_history:'较早聊天历史',chat_page:'聊天历史'};
setInterval(()=>{
 if(!collector.started)return;const e=collector.progressEstimate();
 $('elapsedEstimate').textContent=`${e.running?'已运行':'本次用时'} ${duration(e.elapsedMs)} · 平均每分钟 ${e.readsPerMinute.toFixed(1)} 次读取`;
 $('remainingEstimate').textContent=e.running?`${phaseNames[e.phase]||'当前步骤'}：已读取 ${e.done} 次${e.total===null?'，此步骤总量未知':` / 计划 ${e.total} 次`}。${e.remainingMs===null?'预计剩余时间待估算':`本步骤预计剩余约 ${duration(e.remainingMs)}，按当前均值预计再取得约 ${e.remainingData} 条资料观察`}。`:'本次操作已结束，资料完整性请查看审核。';
},1000);
Object.assign(explanations,{REVIEW_HOST_UPDATE_REQUIRED:'审核模块尚未载入，请在采集结束后关闭 B 工具页并重新打开。',REVIEW_CHANGED:'审核范围或返回结构发生变化，请重新审核',STOP_BEFORE_REVIEW:'请等待采集结束后再审核',REVIEW_RECORD_MISMATCH:'保存记录不连续或不一致，需要检查本机存储'});
async function preview(){const ticket=++previewTicket,run=previewRun;const data=await host.call('preview',{run_id:run,offset:previewOffset});if(ticket!==previewTicket||run!==previewRun)return;$('preview').hidden=false;$('previewText').textContent=JSON.stringify(data.records,null,2);previewOffset=data.next_offset;$('nextPreview').disabled=previewOffset>=data.total;}
function closePreview(){previewTicket++;$('preview').hidden=true;$('previewText').textContent='';previewRun=null;previewOffset=0;}
$('viewCurrent').onclick=guarded(async()=>{if(!currentRun)return;previewRun=currentRun;previewOffset=0;await preview();$('preview').scrollIntoView({behavior:'smooth',block:'start'});});
$('nextPreview').onclick=guarded(preview);$('closePreview').onclick=closePreview;
$('importFiles').onclick=guarded(async()=>{const files=[...$('files').files];if(!files.length)throw Error('SELECT_JSONL_FILES');lastFailure='';const epoch=await collector.start(scope(['legacy_chat']),options());currentRun=collector.run.run_id;$('taskState').textContent='正在本机加密导入';buttons();await collector.importFiles(files,epoch);$('files').value='';});
const today=new Date(),week=new Date(today);week.setDate(week.getDate()-6);$('startDate').value=localDate(week);$('endDate').value=localDate(today);
$('collectBackend').onchange=buttons;$('collectChat').onchange=buttons;$('limitReads').onchange=buttons;
window.addEventListener('beforeunload',e=>{if(collector.run||gaps.busy||preparing||reviewing){e.preventDefault();e.returnValue='';}});
if(!globalThis.chrome?.runtime?.id){$('connect').disabled=true;$('openAnchor').disabled=true;$('openChat').disabled=true;notice('界面预览：实际采集请从安装后的 Chrome 扩展打开。此页面不会读取真实来源。');}
else void guarded(connect)();

Object.assign(explanations,{GAP_NOT_REGISTERED:'此任务尚无已登记的补采队列，不能自动推定补采范围。',GAP_NO_UNATTEMPTED:'没有尚未处理的队列请求；已处理空返回和失败不会自动重试。',GAP_INCOMPLETE:'仍有未处理请求，不能标记完成。',VERSION_MISMATCH:'补采执行版本与原队列不兼容，已保留进度并停止。',HOST_ALREADY_OPEN:'已有 B 任务占用本机存储，请等待它结束。',ACCOUNT_CHECK_REQUIRED:'原账号核验尚未通过，补采停止。'});
