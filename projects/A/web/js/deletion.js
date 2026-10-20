(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.DSHDeletion=value;})(globalThis,function(){
  'use strict';
  function validManifest(value,binding){return !!value&&value.format==='dfa-deletion-manifest-1'&&value.mode==='read_only_manifest'&&value.account_key===binding.account_key&&value.target_kind===binding.target_kind&&value.target_key===binding.target_key&&value.execution_available===false&&value.deleted===false&&value.physical_purged===false&&value.body_exported===false&&value.complete_inventory===false&&value.copies_state==='copies_pending'&&typeof value.plan_hash==='string'&&/^[a-f0-9]{64}$/.test(value.plan_hash)&&Number.isInteger(value.identifier_count)&&value.identifier_count>=0&&value.identifier_count<=5000&&Array.isArray(value.targets)&&value.targets.length<=5000&&value.targets.every(target=>target.account_key===binding.account_key&&['message','observation'].includes(target.target_kind)&&typeof target.target_key==='string'&&target.target_key.length<=200)&&value.counts&&Object.values(value.counts).every(count=>Number.isInteger(count)&&count>=0&&count<=50000);}
  class DeletionController{
    constructor(api,notify=()=>{}){this.api=api;this.notify=notify;this.generation=0;this.lifecycle=0;this.account=null;this.state={state:'idle',account_key:null,binding:null,result:null,busy:false};}
    emit(patch){Object.assign(this.state,patch);this.notify(this.state);}
    clear(){this.generation++;this.lifecycle++;this.account=null;this.state={state:'idle',account_key:null,binding:null,result:null,busy:false};this.notify(this.state);}
    selectAccount(account){this.clear();this.account=account||null;this.emit({account_key:this.account});}
    access(account){return !!account&&typeof this.api.deletionAccess==='function'&&this.api.deletionAccess(account).allowed;}
    async inspect(binding){if(!binding||binding.account_key!==this.account||!this.access(binding.account_key)){this.generation++;this.emit({state:'error',error:'forbidden',binding:null,result:null,busy:false});return;}const stamp=++this.generation,body=structuredClone(binding);this.emit({state:'loading',binding:body,result:null,busy:true});try{const result=await this.api.deletionPlan(body);if(stamp!==this.generation)return;if(!validManifest(result,body))throw Error('bad_response');this.emit({state:'ready',result,busy:false});}catch(error){if(stamp===this.generation)this.emit({state:'error',error:error.message,busy:false,result:null});}}
  }
  function mount(root,api,document){
    const countLabels={message:'消息',message_revision:'消息正文版本',message_observation:'来源观察',message_observation_raw:'观察原文',domain_event:'本机事件',sync_received:'已接收事件',outbox:'待传事件',ai_job:'AI任务',ai_result:'AI结果',ai_revision:'AI修订',profile_unresolved:'需人工核对的资料摘录',legacy_unresolved:'需人工核对的旧记录'};
    const unresolvedLabels={encrypted_packages_and_journal_need_authenticated_record_scan:'加密包与日志尚需逐条认证扫描。',peer_business_copy_and_independent_deletion_receipt:'对端业务副本与独立删除回执仍待处理。',external_deletion_log_and_restore_gate:'外部删除日志与恢复拦截仍待核对。',sqlite_wal_free_pages_and_external_copies:'SQLite日志、空闲页及外部副本的物理清除尚未核实。',mixed_package_survivors_must_be_preserved:'混合包中的未选记录必须保留。',manual_profile_and_legacy_excerpts_require_explicit_review:'人工资料摘录与旧记录需明确人工核对，不能靠正文相似自动定位。'};
    function node(tag,text){const element=document.createElement(tag);if(text!==undefined)element.textContent=text;return element;}
    const detail=node('details'),summary=node('summary','必要删除范围清单 · 只读'),notice=node('p','管理可在已保存历史或文件观察旁点击“查看删除范围”。这里只盘点明确记录、别名与引用，无执行能力，不删除、不撤回来源、不清理任何副本。');
    const status=node('p'),area=node('div');status.setAttribute('role','status');detail.append(summary,notice,status,area);root.append(detail);
    const errors={forbidden:'当前会话或账号未授予本机管理 deletion:plan 只读权限。',unauthorized:'会话已失效，请重新登录。',conflict:'来源命名空间或登记映射冲突，不能生成可靠范围清单。',deletion_plan_limit:'完整清单超出预算；没有返回截断清单，也没有执行删除。',bad_response:'清单的对象、只读标记或范围无法核对，未展示为可用清单。'};
    const controller=new DeletionController(api,state=>{status.textContent=state.state==='idle'?'从已保存消息或文件观察选择对象；饱饱没有清单管理动作。':state.state==='loading'?'正在生成只读范围清单；没有执行删除…':state.state==='ready'?'已生成只读清单，未执行删除。副本仍待处理，完整物理清除未知。':errors[state.error]||'范围清单暂不可用；没有执行删除。';area.replaceChildren();if(state.result)render(state.result);});
    function list(title,rows,fields){const section=node('details');section.append(node('summary',title));const items=node('ul');for(const row of rows||[]){if(typeof row==='string')items.append(node('li',row));else if(row&&typeof row==='object')items.append(node('li',JSON.stringify(Object.fromEntries(fields.filter(key=>Object.prototype.hasOwnProperty.call(row,key)).map(key=>[key,row[key]])))));}if(!items.children.length)items.append(node('li','没有已定位条目；其他副本仍待核对。'));section.append(items);area.append(section);}
    function render(data){
      area.append(node('p',`选定账号：${data.account_key}；选定对象：${data.target_kind==='message'?'已保存消息':'文件/来源观察'} ${data.target_key}。明确闭包定位 ${data.identifier_count} 个标识；copies_pending / 未执行 / 物理清除未知。`));
      area.append(node('p','清单包含明确关联记录及依赖对象的版本摘要；计数不代表这些账号、联系人或来源登记将被删除。原文没有导出，人工备注/摘录与历史资料中的同文不能靠文字相似自动定位。'));
      const counts=node('table'),header=node('tr');header.append(node('th','关联存储类别（只读盘点）'),node('th','已定位行数'));counts.append(header);for(const [key,value] of Object.entries(data.counts)){const row=node('tr');row.append(node('td',countLabels[key]||key),node('td',String(value)));counts.append(row);}area.append(counts);
      list('明确消息/观察目标',data.targets,['account_key','target_kind','target_key','version_digest']);
      list('平台消息/文件记录别名范围',data.alias_selectors,['kind','account_key','platform_conversation_key','platform_message_key','account_namespace','source_key','source_id','source_version']);
      list('关联事件账本引用（原始事件仍保留）',data.event_refs,['event_id','event_digest','device_key','epoch','channel','seq','target_key','event_type','storage']);
      list('关联AI任务定位键（未导出结果或提示词）',data.ai_job_keys,[]);
      list('原文版本摘要（无正文）',data.revisions,['message_key','revision','body_sha256','semantic_digest','observation_key']);
      list('观察版本摘要（无正文）',data.observation_versions,['observation_key','source_id','source_version','observation_id','message_key','device_key','semantic_digest','body_sha256','version_digest']);
      area.append(node('p','需人工核对数量：'+JSON.stringify(data.review_counts||{})),node('p','副本类别：'+JSON.stringify(data.copy_classes||{})));
      list('加密包账本成员（包内容未复检）',Object.values(data.related_ids||{}).filter(Array.isArray).flatMap(rows=>rows.filter(row=>row&&typeof row==='object'&&typeof row.package_id==='string')),['package_id','related_events','total_events','classification','content_inspected']);
      list('关联人工动作定位键',data.related_ids&&data.related_ids.manual_action_keys,[]);
      list('关联文件导入批次（不代表来源整域删除）',data.related_ids&&data.related_ids.file_import_batch_ids,[]);
      list('已登记的删除状态',data.related_ids&&data.related_ids.existing_tombstones,['target_key','deletion_state','created_at_utc']);
      list('仍待处理/无法自动定位的范围',(data.unresolved||[]).map(value=>unresolvedLabels[value]||value),[]);
      area.append(node('p','清单版本：'+data.plan_hash+'；数据库变化标记：'+String(data.metadata&&data.metadata.data_version||'未知')),
        node('p','日志、备份、暂存、传输、对端和外部副本仍需独立核对；混合包中未选记录必须保留。当前产品入口没有删除执行、回滚或物理清理能力。'));
    }
    controller.notify(controller.state);
    return {selectAccount:account=>controller.selectAccount(account),clear:()=>controller.clear(),controller,
      button(binding){if(!controller.access(binding.account_key)||typeof binding.target_key!=='string'||!binding.target_key)return null;const captured=structuredClone(binding),lifecycle=controller.lifecycle;const button=node('button','查看删除范围（只读）');button.type='button';button.addEventListener('click',()=>{if(lifecycle!==controller.lifecycle)return;detail.open=true;return controller.inspect(captured);});return button;}};
  }
  return {validManifest,DeletionController,mount};
});
