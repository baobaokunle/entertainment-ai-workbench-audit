(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.DSHCandidateReview=value;})(globalThis,function(){
  'use strict';
  const sha=value=>typeof value==='string'&&/^[0-9a-f]{64}$/.test(value);
  const fields=['account_key','source_id','source_version','account_namespace'];
  const exact=(value,keys)=>value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).sort().join()===keys.slice().sort().join();
  function binding(value,account){return exact(value,fields)&&value.account_key===account&&fields.every(key=>typeof value[key]==='string'&&!!value[key].trim()&&!value[key].includes('\u0000')&&value[key].length<=(key==='source_version'?80:key==='account_namespace'?256:200));}
  function context(value,account){
    const common=['format','configured','account_key','persisted','real_write_enabled'];
    if(!value||value.format!=='dfa-candidate-review-context-1'||value.account_key!==account||value.persisted!==false||value.real_write_enabled!==false)throw Error('bad_response');
    if(value.configured===false&&exact(value,common))return value;
    if(value.configured!==true||!exact(value,common.concat(['binding','binding_scope_digest','review_key','page_count_limit','source_revalidated','commit_authorized']))||!binding(value.binding,account)||!sha(value.binding_scope_digest)||!sha(value.review_key)||value.page_count_limit!==20||value.source_revalidated!==false||value.commit_authorized!==false)throw Error('bad_response');
    return value;
  }
  const countKeys=['records','versions','conflicts','pending'];
  const counts=value=>exact(value,countKeys)&&countKeys.every(key=>Number.isSafeInteger(value[key])&&value[key]>=0);
  const pageKeys=['page_index','counts','page_quote_hash','business_plan_hash'];
  const commonKeys=['snapshot_sha256','profile_digest','binding_scope_digest','binding_quote','authority','record_identity_mapping','source_revalidated','candidate_database_read_only','business_database_read_only','source_approved','account_ownership','private_chat_scope','persisted','business_import_committed','commit_authorized','real_write_enabled','two_database_atomicity_claim','review_key'];
  function plan(value,selected,first,length){
    const single=length===1;
    if(!value||!exact(value,commonKeys.concat(single?['format',...pageKeys]:['format','first_page_index','page_count','pages','counts','counts_scope']))||value.format!==(single?'dfa-candidate-page-plan-cli-1':'dfa-candidate-page-range-plan-cli-1')||value.review_key!==selected.review_key||value.binding_scope_digest!==selected.binding_scope_digest||!['snapshot_sha256','profile_digest','binding_scope_digest','binding_quote','review_key'].every(key=>sha(value[key]))||value.authority!=='pending_only'||value.record_identity_mapping!=='explicit_unknown'||value.account_ownership!=='unknown'||value.private_chat_scope!=='unknown'||!['source_revalidated','candidate_database_read_only','business_database_read_only'].every(key=>value[key]===true)||!['source_approved','persisted','business_import_committed','commit_authorized','real_write_enabled','two_database_atomicity_claim'].every(key=>value[key]===false)||!counts(value.counts))throw Error('bad_response');
    const pages=single?[{page_index:value.page_index,counts:value.counts,page_quote_hash:value.page_quote_hash,business_plan_hash:value.business_plan_hash}]:value.pages;
    if(!Array.isArray(pages)||pages.length!==length||(!single&&(value.first_page_index!==first||value.page_count!==length||value.counts_scope!=='selected_pages_only')))throw Error('bad_response');
    for(let i=0;i<length;i++){const page=pages[i];if(!exact(page,pageKeys)||page.page_index!==first+i||!counts(page.counts)||!sha(page.page_quote_hash)||!sha(page.business_plan_hash))throw Error('bad_response');}
    for(const key of countKeys)if(pages.reduce((total,page)=>total+page.counts[key],0)!==value.counts[key])throw Error('bad_response');
    return value;
  }
  class ReviewController{
    constructor(api,changed=()=>{}){this.api=api;this.changed=changed;this.generation=0;this.pending=0;this.clear();}
    emit(){this.changed(this.state);}
    clear(){this.generation++;this.state={account:null,context:null,plan:null,first:1,length:1,busy:this.pending>0,status:'idle'};this.emit();}
    invalidate(){this.generation++;this.state.plan=null;this.state.status=this.state.context&&this.state.context.configured?'select':this.state.status;this.emit();}
    async selectAccount(account){
      this.clear();if(!account)return;this.state.account=account;
      if(!this.api.importBindingAccess(account).allowed){this.state.status='forbidden';this.emit();return;}
      const stamp=++this.generation;this.state.busy=true;this.state.status='loading';this.emit();
      try{const value=context(await this.api.candidateReviewContext({account_key:account}),account);if(stamp!==this.generation)return;this.state.context=value;this.state.status=value.configured?'select':'unconfigured';}
      catch(error){if(stamp===this.generation){this.state.context=null;this.state.status='error';}}
      finally{if(stamp===this.generation){this.state.busy=this.pending>0;this.emit();}}
    }
    draft(first,length){this.invalidate();this.state.first=first;this.state.length=length;this.emit();}
    async review(){
      const selected=this.state.context,first=this.state.first,length=this.state.length;
      if(this.pending||!selected||!selected.configured||!this.api.importBindingAccess(this.state.account).allowed||!Number.isSafeInteger(first)||first<1||!Number.isInteger(length)||length<1||length>20)throw Error('selection_required');
      const stamp=++this.generation;this.pending++;this.state.busy=true;this.state.plan=null;this.state.status='reviewing';this.emit();
      try{const value=await this.api.candidateReviewPlan({binding:{...selected.binding},review_key:selected.review_key,page_index:first,page_count:length,unknown_records:true});if(stamp!==this.generation)return;if(!this.api.importBindingAccess(this.state.account).allowed)throw Error('forbidden');this.state.plan=plan(value,selected,first,length);this.state.status='reviewed';}
      catch(error){if(stamp===this.generation){this.state.plan=null;this.state.status=error.message==='candidate_review_timeout'?'timeout':'error';}}
      finally{this.pending--;this.state.busy=this.pending>0;this.emit();}
    }
  }
  function mount(root,api,doc){
    const make=(tag,text)=>{const node=doc.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
    const title=make('h3','大文件候选核对'),status=make('p'),scope=make('p'),firstLabel=make('label','开始页'),first=make('input'),lengthLabel=make('label','连续页数'),length=make('input'),review=make('button','只核对这几页 · 身份保留未知'),result=make('div');
    status.setAttribute('role','status');first.type=length.type='number';first.min=length.min='1';length.max='20';first.step=length.step='1';review.type='button';firstLabel.append(first);lengthLabel.append(length);root.replaceChildren(title,status,scope,firstLabel,lengthLabel,review,result);
    const controller=new ReviewController(api,state=>{
      const labels={idle:'选择已授权账号后可查看本机已配置的核对范围。',forbidden:'当前会话没有候选核对权限。',loading:'正在读取核对范围，不扫描聊天记录…',unconfigured:'此账号尚无已配置的大文件候选；已保存历史仍可正常查询。',select:'选择页范围后核对；不会正式导入聊天或合并身份。',reviewing:'正在核对选定资料的一致性，可能需要一些时间；不会重复启动。',reviewed:'选定页的一致性复核通过；身份、账号归属和私聊范围仍未知。',timeout:'界面等待已到期；本机核对可能仍在收尾。没有自动重试，也未正式导入。',error:'本次未取得有效计划。资料保持原状；没有自动重试。'};
      status.textContent=labels[state.status];const selected=state.context&&state.context.configured;
      scope.textContent=selected?'已配置来源：'+state.context.binding.source_id+' / '+state.context.binding.source_version+'；仅核对当前授权账号。':'';
      first.value=String(state.first);length.value=String(state.length);first.disabled=length.disabled=review.disabled=!selected||state.busy;
      result.replaceChildren();if(state.plan){const value=state.plan;result.append(make('p','仅所选页：候选记录 '+value.counts.records+' 条，待核验 '+value.counts.pending+' 条，冲突 '+value.counts.conflicts+' 条。'),make('p','未保存聊天记录，未正式导入，未确认身份。完整覆盖仍未知。'));const list=make('ul'),pages=value.pages||[{page_index:value.page_index,counts:value.counts}];for(const page of pages)list.append(make('li','第 '+page.page_index+' 页：待核验 '+page.counts.pending+' 条'));result.append(list);}
    });
    function draft(){controller.draft(Number(first.value),Number(length.value));}
    first.addEventListener('input',draft);length.addEventListener('input',draft);review.addEventListener('click',()=>controller.review().catch(()=>{status.textContent='请选择正整数页号与1至20页的范围。';}));
    return {controller,selectAccount:account=>controller.selectAccount(account),clear:()=>controller.clear(),invalidate:()=>controller.invalidate()};
  }
  return {ReviewController,mount,validateContext:context,validatePlan:plan};
});
