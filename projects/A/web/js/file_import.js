(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.DSHFileImport=value;})(globalThis,function(){
  'use strict';
  const MAX_BYTES=512*1024;
  function parsePreview(bytes){
    if(!(bytes instanceof Uint8Array)||bytes.byteLength>MAX_BYTES)throw Error('preview_too_large');
    let value;try{value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));}catch{throw Error('invalid_preview');}
    if(!value||value.status!=='offline_preview'||value.persisted!==false||!Array.isArray(value.records)||typeof value.account_namespace!=='string')throw Error('invalid_preview');
    return value;
  }
  function scopeText(scope){
    if(!scope||scope.kind!=='origin_source_version'||!Array.isArray(scope.batch_ids)||typeof scope.scope_hash!=='string'||!scope.scope_hash)return null;
    return `撤回范围：账号 ${scope.account_key} / 设备 ${scope.device_key} / 来源 ${scope.source_id} / 版本 ${scope.source_version} 的所有导入批次（${scope.batch_ids.length} 批、${scope.affected_import_observations} 条观察）。保留原文与事件，不撤回其他来源；同来源以后也不能恢复权威。${scope.local_source_verified_flag_disabled?"本机该来源版本的核验标记也会关闭。":""}`;
  }
  class ImportController{
    constructor(api,notify=()=>{}){this.api=api;this.notify=notify;this.generation=0;this.queryGeneration=0;this.account=null;this.state={state:'idle',account_key:null,context:null,can_query:false,preview:null,mapping:null,plan:null,result:null,items:[],batches:[],next_cursor:null,busy:false,query_busy:false,person_key:null,sender_scope:null,query_error:null};}
    emit(patch){Object.assign(this.state,patch);this.notify(this.state);}
    clear(){this.generation++;this.queryGeneration++;this.account=null;this.state={state:'idle',account_key:null,context:null,can_query:false,preview:null,mapping:null,plan:null,result:null,items:[],batches:[],next_cursor:null,busy:false,query_busy:false,person_key:null,sender_scope:null,query_error:null};this.notify(this.state);}
    async selectPerson(person){
      // Independent from account-context loading: an old person lookup cannot
      // cancel the new account registration response, or retain its old body.
      this.queryGeneration++;
      this.emit({person_key:person||null,items:[],batches:[],next_cursor:null,sender_scope:null,query_error:null,query_busy:false});
      if(person&&this.state.context&&this.state.context.pending_read_binding)return this.query();
    }
    async selectAccount(account){this.clear();if(!account)return;this.account=account;const stamp=this.generation;this.emit({account_key:account,can_query:typeof this.api.importReadAccess==='function'?this.api.importReadAccess(account).allowed:true,state:'loading',busy:true});try{const context=await this.api.importContext({account_key:account});if(stamp!==this.generation)return;this.emit({context,state:'ready',busy:false});if(this.state.person_key&&context.pending_read_binding)await this.query();}catch(error){if(stamp===this.generation)this.emit({state:'error',error:error.message,busy:false});}}
    async selectFile(file){if(this.state.query_busy)throw Error('busy');if(!this.account||!this.state.context)throw Error('invalid_request');const account=this.account,stamp=++this.generation;this.emit({state:'reading',preview:null,mapping:null,plan:null,result:null,busy:true});try{if(file&&file.size>MAX_BYTES)throw Error('preview_too_large');if(!file||!/\.json$/i.test(file.name))throw Error('invalid_preview');const preview=parsePreview(new Uint8Array(await file.arrayBuffer()));if(stamp!==this.generation||account!==this.account)return;this.emit({state:'mapped',preview,mapping:{account_key:'',source_id:'',source_version:'',account_namespace:'',records:Object.fromEntries(preview.records.map(row=>[row.source_key,{}]))},busy:false});}catch(error){if(stamp===this.generation)this.emit({state:'error',error:error.message,busy:false});}}
    draft(mapping){if(this.state.busy||this.state.query_busy)throw Error('busy');this.generation++;this.emit({mapping:structuredClone(mapping),plan:null,result:null,state:'mapped'});}
    async plan(){if(this.state.busy||this.state.query_busy||!this.state.preview||!this.state.mapping)throw Error('invalid_request');const stamp=++this.generation;const body=structuredClone({preview:this.state.preview,mapping:this.state.mapping});this.emit({state:'planning',plan:null,busy:true});try{const plan=await this.api.importPlan(body);if(stamp!==this.generation)return;if(typeof plan.plan_hash!=='string'||!scopeText(plan.withdrawal_scope))throw Error('bad_response');this.emit({state:'planned',plan,busy:false});}catch(error){if(stamp===this.generation)this.emit({state:'error',error:error.message,busy:false});}}
    async commit(){if(this.state.busy||this.state.query_busy||!this.state.plan)throw Error('plan_required');const stamp=++this.generation;const body=structuredClone({preview:this.state.preview,mapping:this.state.mapping,expected_plan_hash:this.state.plan.plan_hash});this.emit({state:'committing',busy:true});try{const result=await this.api.importCommit(body);if(stamp!==this.generation)return;this.emit({state:'committed',result,plan:null,busy:false});}catch(error){if(stamp===this.generation)this.emit({state:'error',error:error.message,plan:null,busy:false});}}
    async query(keyword='',more=false){
      if(this.state.busy||this.state.query_busy||!this.account||!this.state.can_query)return;
      const binding=this.state.context&&this.state.context.pending_read_binding,person=this.state.person_key;
      if(binding&&!person)return;
      const stamp=++this.queryGeneration,accountStamp=this.generation;
      const current=()=>stamp===this.queryGeneration&&accountStamp===this.generation;
      this.emit({query_busy:true,query_error:null,...(!more?{items:[],batches:[],next_cursor:null,sender_scope:null}:{})});
      try{
        const selected=binding?{person_key:person,source_id:binding.source_id,source_version:binding.source_version,account_namespace:binding.account_namespace}:{};
        if(binding&&(binding.account_key!==this.account||Object.keys(binding).length!==4||Object.values(binding).some(value=>typeof value!=='string'||!value)))throw Error('bad_response');
        const result=await this.api.importObservations({account_key:this.account,keyword,...selected,...(more&&this.state.next_cursor?{before:this.state.next_cursor}:{})});
        if(!current())return;
        if(!Array.isArray(result.items))throw Error('bad_response');
        if(binding){
          const scope=result.sender_scope;
          if(result.authority!=='pending_only'||result.coverage!=='unknown'||!scope||scope.person_key!==person||scope.kind!=='export_sender_claim'||scope.private_chat_scope!=='unknown'||scope.coverage!=='unknown'||!['ready','identity_unavailable'].includes(scope.state)||!scope.binding||Object.keys(scope.binding).length!==4||Object.keys(binding).some(key=>binding[key]!==scope.binding[key])||result.items.some(item=>item.authority!=='pending_only'||item.status!=='pending'||item.withdrawn!==false)||(scope.state==='identity_unavailable'&&(result.items.length||result.next_cursor)))throw Error('bad_response');
        }
        this.emit({items:more?[...this.state.items,...result.items]:result.items,batches:result.batches||[],next_cursor:result.next_cursor||null,sender_scope:result.sender_scope||null,query_busy:false});
      }catch(error){if(current())this.emit({query_error:error.message,items:[],batches:[],next_cursor:null,sender_scope:null,query_busy:false});}
    }
    async withdraw(batch){if(this.state.busy||this.state.query_busy||!scopeText(batch.withdrawal_scope))throw Error('invalid_scope');const stamp=++this.generation;this.emit({busy:true,plan:null});try{const result=await this.api.importWithdraw({account_key:this.account,batch_id:batch.batch_id,expected_scope_hash:batch.withdrawal_scope.scope_hash});if(stamp!==this.generation)return;this.emit({state:'withdrawn',result,busy:false});await this.query();}catch(error){if(stamp===this.generation)this.emit({state:'error',error:error.message,busy:false});}}
  }
  function mount(root,api,document,options={}){
    function node(tag,text){const result=document.createElement(tag);if(text!==undefined)result.textContent=text;if(tag==='option')result.value='';return result;}
    const notice=node('p','仅选择上一阶段CLI生成的预览JSON，最多512 KiB；请求还包括映射，超过预算会拒绝整份预览，不截断。原SQLite不放入浏览器。记录始终待核验，不证明人工发送。');
    const file=node('input');file.type='file';file.accept='.json,application/json';const fileLabel=node('label','1. 选择预览JSON');fileLabel.append(file);
    const mappingArea=node('div'),status=node('p'),planArea=node('div'),list=node('ol'),batches=node('div');status.setAttribute('role','status');
    const planButton=node('button','2. 核对映射并生成计划'),commitButton=node('button','3. 明确保存此计划');planButton.type=commitButton.type='button';
    const queryForm=node('form'),keyword=node('input'),queryButton=node('button','查询已保存待核验原文'),more=node('button','下一页待核验原文');keyword.maxLength=200;keyword.placeholder='正文关键词';queryButton.type='submit';more.type='button';queryForm.append(keyword,queryButton);
    root.append(notice,fileLabel,mappingArea,planButton,planArea,commitButton,status,queryForm,list,more,batches);
    let renderedPreview=null,renderedContext=null;
    const labels={idle:'先登录并选择已授权账号。',loading:'读取账号登记对象…',ready:'选择预览，然后明确来源、命名空间、人物与会话；未知映射保持待核验。',reading:'严格读取UTF-8预览…',mapped:'映射尚未保存；请选择每条记录的已登记人物和会话，或明确保持未知。',planning:'正在生成服务端计划…',planned:'请核对计划和整来源撤回范围，再点击明确保存。',committing:'正在保存文件观察…',committed:'已保存待核验。导入不证明人工发送、成功或完整历史覆盖。',withdrawn:'已撤回来源权威；原文与事件保留。'};
    const errors={preview_too_large:'预览或含映射请求超过512 KiB预算；未截断，请使用更小预览。',invalid_preview:'请选择CLI生成的有效UTF-8预览JSON。',forbidden:'此会话不是有该账号权限的本机管理工作台。',conflict:'计划或撤回范围已变化，请重新查询来源范围并核对计划。'};
    const controller=new ImportController(api,state=>{
      status.textContent=state.state==='error'?(state.error==='forbidden'&&state.can_query&&!state.context?'当前身份可查询已保存待核验原文；文件映射、提交和撤回仅允许管理工作台。':errors[state.error]||'本机导入接口暂不可用，请核对登记和显式schema安装后重试。'):labels[state.state]||'';
      const selected=!!(state.context&&state.context.pending_read_binding);
      for(const control of [notice,fileLabel,mappingArea,planButton,planArea,commitButton])control.hidden=selected;
      if(selected){
        status.textContent=state.query_error?'当前对象的待核验记录暂不可用，已清空旧结果；身份和私聊范围保持未知。':!state.person_key?'请选择当前对象；只在本次已导入范围内核对导出发送者声明。':state.query_busy?'正在核对当前对象的导出发送者声明…':state.sender_scope&&state.sender_scope.state==='identity_unavailable'?'当前对象尚无可用于核对的稳定 UID；资料保持未知。':state.sender_scope?`导出中声明由当前对象 UID 发送的待核验记录：${state.items.length} 条。可能包含群聊，私聊范围未知；不按会话补入主播回复，不用于 AI 上下文。${state.items.length?'':'本次已导入范围内无匹配，不代表没有聊天。'}`:'当前对象待核验；资料及私聊范围未知。';
      }else if(state.query_error)status.textContent='待核验记录查询失败，已清空旧结果。';
      file.disabled=state.busy||state.query_busy||!state.context;planButton.disabled=state.busy||state.query_busy||!state.preview;commitButton.disabled=state.busy||state.query_busy||!state.plan||!state.context||state.context.writes_enabled!==true;queryButton.disabled=more.disabled=state.busy||state.query_busy||!state.can_query||(selected&&!state.person_key);more.hidden=!state.next_cursor;
      if(state.preview!==renderedPreview||state.context!==renderedContext){renderedPreview=state.preview;renderedContext=state.context;mappingArea.replaceChildren();if(state.preview&&state.context)renderMapping(state);}
      for(const control of mappingArea.querySelectorAll('input,select'))control.disabled=state.busy||state.query_busy;
      planArea.replaceChildren();if(state.plan){planArea.append(node('p','计划：'+JSON.stringify(state.plan.counts)+'；权威：'+state.plan.authority+'；范围：仅选定账号的此文件观察。'),node('p',scopeText(state.plan.withdrawal_scope)));}
      list.replaceChildren();for(const item of state.items){const row=node('li');row.append(node('small',`${item.withdrawn?'来源已撤回':'已保存待核验'} · ${item.batch_id} · ${item.source_key}`),node('pre',typeof item.body==='string'?item.body:'正文不可用'));const details=node('details');details.append(node('summary','导出声明与质量（均不作为已核验发送证明）'),node('pre',JSON.stringify({export_candidate:item.export_candidate,quality:item.quality},null,2)));row.append(details);if(typeof options.deletionButton==='function'){const inspect=options.deletionButton({account_key:state.account_key,target_kind:'observation',target_key:item.observation_key});if(inspect)row.append(inspect);}list.append(row);}
      batches.replaceChildren();const batchRows=state.batches.length?state.batches:state.result&&state.result.batch_id&&state.result.withdrawal_scope?[state.result]:[];
      for(const batch of batchRows){const scope=scopeText(batch.withdrawal_scope);if(!scope)continue;const details=node('details');details.append(node('summary','审阅整来源撤回范围'),node('p',scope));const button=node('button','确认撤回此账号/设备/来源版本全部批次的权威');button.type='button';button.disabled=state.busy||state.query_busy||!state.context||state.context.writes_enabled!==true||batch.withdrawable_here===false||batch.withdrawn===true;button.addEventListener('click',()=>controller.withdraw(batch));details.append(button);batches.append(details);}
    });
    function renderMapping(state){
      const accountChoice=node('select');accountChoice.append(node('option','明确确认映射到当前已授权账号'));const accountOption=node('option',state.account_key);accountOption.value=state.account_key;accountChoice.append(accountOption);const accountLabel=node('label','目标账号');accountLabel.append(accountChoice);mappingArea.append(accountLabel);const source=node('select');source.append(node('option','明确选择专用文件来源'));for(const item of state.context.sources){const option=node('option',`${item.source_id} / ${item.source_version} · ${item.verified?'登记核验标记':'未核验'} · ${item.source_kind}`);option.value=JSON.stringify([item.source_id,item.source_version]);source.append(option);}const sourceLabel=node('label','专用文件来源（服务端会拒绝混用来源）');sourceLabel.append(source);mappingArea.append(sourceLabel);
      const namespace=node('input');namespace.maxLength=256;const namespaceLabel=node('label','明确输入文件命名空间：'+state.preview.account_namespace);namespaceLabel.append(namespace);mappingArea.append(namespaceLabel);
      function update(){const mapping=structuredClone(controller.state.mapping);mapping.account_key=accountChoice.value;if(source.value){[mapping.source_id,mapping.source_version]=JSON.parse(source.value);}else{mapping.source_id=mapping.source_version='';}mapping.account_namespace=namespace.value;controller.draft(mapping);}
      accountChoice.addEventListener('change',update);source.addEventListener('change',update);namespace.addEventListener('input',update);
      for(const record of state.preview.records){const details=node('details');details.append(node('summary','记录 '+record.source_key+' · 导出会话声明 '+(record.platform_conversation_id||record.conversation_id||'未知')));for(const version of record.versions||[])details.append(node('pre',version.original_body||''));const alias=node('select'),conversation=node('select');alias.append(node('option','明确保持人物与会话未知'));for(const item of state.context.identities){const option=node('option',`${item.person_key} · ${item.identity_kind}=${item.identity_value}`);option.value=JSON.stringify(item);alias.append(option);}conversation.append(node('option','明确保持会话未知'));function recordUpdate(){const mapping=structuredClone(controller.state.mapping);const identity=alias.value?JSON.parse(alias.value):null;mapping.records[record.source_key]=identity?{object_kind:identity.object_kind,identity_kind:identity.identity_kind,identity_value:identity.identity_value,...(conversation.value?{platform_conversation_key:conversation.value}:{})}:{};controller.draft(mapping);}alias.addEventListener('change',()=>{conversation.replaceChildren(node('option','明确保持会话未知'));if(alias.value){const identity=JSON.parse(alias.value);for(const item of state.context.conversations.filter(row=>row.person_key===identity.person_key)){const option=node('option',item.platform_conversation_key);option.value=item.platform_conversation_key;conversation.append(option);}}recordUpdate();});conversation.addEventListener('change',recordUpdate);details.append(alias,conversation);mappingArea.append(details);}
    }
    file.addEventListener('change',()=>controller.selectFile(file.files[0]));planButton.addEventListener('click',()=>controller.plan());commitButton.addEventListener('click',()=>controller.commit());queryForm.addEventListener('submit',event=>{event.preventDefault();controller.query(keyword.value);});more.addEventListener('click',()=>controller.query(keyword.value,true));
    return {selectAccount(account){file.value='';keyword.value='';return controller.selectAccount(account);},selectPerson(person){keyword.value='';return controller.selectPerson(person);},clear(){file.value='';keyword.value='';controller.clear();},controller};
  }
  return {MAX_BYTES,parsePreview,scopeText,ImportController,mount};
});
