export const REVIEW_CATEGORIES=['data','empty_response','no_fields','diagnostic','account'];
export const reviewCounts=()=>Object.fromEntries(REVIEW_CATEGORIES.map(k=>[k,0]));
export async function reviewRun(host,run,{progress=()=>{},cancelled=()=>false}={}){
 const result={counts:reviewCounts(),domains:{},total:null,processed:0,selected_domains:[],source_complete:false,formal_A_acceptance:false};
 do {
  if(cancelled())throw Error('REVIEW_CANCELLED');
  const p=await host.call('review_page',{run_id:run,offset:result.processed,total:result.total});
  if(cancelled())throw Error('REVIEW_CANCELLED');
  if(p.offset!==result.processed||!Number.isSafeInteger(p.total)||p.total<0||(result.total!==null&&p.total!==result.total)||!Number.isSafeInteger(p.next_offset)||p.next_offset<p.offset||p.next_offset>p.total||p.done!==(p.next_offset===p.total)||(!p.done&&p.next_offset===p.offset))throw Error('REVIEW_CHANGED');
  const check=c=>{if(!c||REVIEW_CATEGORIES.some(k=>!Number.isSafeInteger(c[k])||c[k]<0))throw Error('REVIEW_CHANGED');return REVIEW_CATEGORIES.reduce((s,k)=>s+c[k],0);};
  if(check(p.counts)!==p.next_offset-p.offset||Object.values(p.domains).reduce((s,d)=>s+check(d.counts),0)!==p.next_offset-p.offset)throw Error('REVIEW_CHANGED');
  result.total=p.total;result.processed=p.next_offset;result.selected_domains=p.selected_domains;result.state=p.state;
  for(const k of REVIEW_CATEGORIES)result.counts[k]+=p.counts[k];
  for(const [domain,row]of Object.entries(p.domains)){
   const out=result.domains[domain]??={counts:reviewCounts(),reasons:{}};
   for(const k of REVIEW_CATEGORIES)out.counts[k]+=row.counts[k];
   for(const [reason,count]of Object.entries(row.reasons))out.reasons[reason]=(out.reasons[reason]||0)+count;
  }
  progress({processed:result.processed,total:result.total});
  if(p.done)break;
 }while(true);
 return result;
}

export const REVIEW_REASONS={
 EMPTY_RESPONSE_NOT_COMPLETE:['来源返回空列表','本次范围未返回记录；是否确实无数据、是否覆盖全部仍待确认。'],
 NO_ALLOWLISTED_FIELDS:['返回记录未识别出业务字段','需要检查字段适配；不能作为无数据。'],
 SOURCE_READ_FAILED:['读取未完成','可能为网络、超时或适配异常；新版诊断记录含具体阶段，旧记录可能没有细分原因。'],
 SOURCE_PERMISSION:['请求被拒绝','核查登录和请求适配，不能直接认定账号无权限。'],
 SOURCE_HTTP:['HTTP 请求未成功','检查来源连接与返回状态。'],
 SOURCE_TIMEOUT:['读取超时','稍后对问题范围检查，不自动重新采集。'],
 SOURCE_REJECTED:['来源业务拒绝','需核对接口返回约定。'],
 SOURCE_SHAPE:['响应结构不匹配','需要修复来源字段适配。'],
 FIELDS_NOT_SCALAR_OR_TOO_LARGE:['部分字段未保存','部分字段类型或大小不符合当前保存规则。'],
 SESSION_BINDING_UNVERIFIED:['场次归属未确认','保留原记录，不能直接用于场次归属。'],
 MESSAGE_CONVERSATION_BINDING_UNVERIFIED:['消息会话对应未确认','不能直接归入正式聊天历史。'],
 SHORT_TEST_BROADCAST_SKIPPED:['已按试播规则跳过明细','这是主动筛选，不是读取失败。'],
 CHAT_CURSOR_NOT_ADVANCING:['聊天历史翻页未推进','已有记录保留，不能宣称历史完整。'],
 CHAT_HISTORY_BUDGET_REACHED:['聊天达到所设页数上限','本次为部分历史。'],
 CHAT_SDK_UNAVAILABLE:['聊天接口未就绪','检查聊天页登录与接口适配。'],
 CHAT_HISTORY_UNAVAILABLE:['聊天历史入口不可用','已有记录保留，历史覆盖未知。'],
 CHAT_LIST_END_UNCONFIRMED:['会话列表覆盖待确认','自动加载后列表暂时稳定，但来源没有提供列表完整性证明。'],
 CHAT_LIST_LOAD_STALLED:['会话列表加载停滞','未到可见底部且滚动持续无进展；已有会话保留，仍可能缺少联系人。'],
 CHAT_SCROLL_CONTAINER_UNAVAILABLE:['未可靠定位会话列表','保留已加载会话；自动加载未成功，不能宣称联系人已齐全。'],
 CHAT_HISTORY_END_UNKNOWN:['历史结束状态未知','来源未明确提供是否还有历史，保留已取得消息。'],
 CHAT_HISTORY_LOAD_STALLED:['历史持续没有新消息','连续空页或重复页仍未取得新消息，已保留缺口，未当作历史结束。'],
 CHAT_HISTORY_SOURCE_NO_MORE:['本会话收到历史结束标记','仅代表该接口本次没有更早消息，不证明平台保存的全部历史或联系人全集。'],
 CHAT_COVERAGE_PARTIAL:['聊天覆盖说明','本轮会话数、历史结束数与消息观察数单独记录；仍不作平台全量声明。'],
 SESSION_TIME_UNAVAILABLE:['场次时间不足','无法确定明细的读取时间范围。'],
 HTTP_401:['HTTP 401','来源身份验证未通过。'],HTTP_403:['HTTP 403','来源拒绝请求，具体原因未确定。'],
 BACKEND_STOPPED_AFTER_PERMISSION_FAILURE:['后台因拒绝而停止','停止后的日期或资料不代表无数据。'],
 OTHER_DIAGNOSTIC:['其他诊断','旧记录没有本审核器可解释的细分原因。']
};
