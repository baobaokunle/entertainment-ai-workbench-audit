(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.DSHImportBinding=value;})(globalThis,function(){
  'use strict';
  const fields=['account_key','source_id','source_version','account_namespace'];
  function bounded(value,max){return typeof value==='string'&&!!value.trim()&&value.length<=max&&!value.includes('\u0000');}
  function same(a,b){return a&&b&&fields.every(key=>a[key]===b[key]);}
  function validateOptions(value,account){
    if(!value||value.format!=='dfa-import-binding-options-1'||!value.account||value.account.account_key!==account||!bounded(value.account.platform_uid,200)||![0,1,false,true].includes(value.account.verified)||value.complete_registry_choices!==true||value.persisted!==false||value.real_write_enabled!==false||!Array.isArray(value.sources)||value.sources.length>100)throw Error('bad_response');
    const seen=new Set();for(const source of value.sources){if(!source||!bounded(source.source_id,200)||!bounded(source.source_version,80)||source.source_kind!=='unverified_export'||![0,false].includes(source.verified))throw Error('bad_response');const key=JSON.stringify([source.source_id,source.source_version]);if(seen.has(key))throw Error('bad_response');seen.add(key);}return value;
  }
  class BindingController{
    constructor(api,changed=()=>{}){this.api=api;this.changed=changed;this.generation=0;this.clear();}
    emit(){this.changed(this.state);}
    clear(){this.generation++;this.state={account:null,options:null,binding:null,plan:null,busy:false,status:'idle',error:null};this.emit();}
    async selectAccount(account){
      this.clear();if(!account)return;this.state.account=account;
      if(!bounded(account,200)||!this.api.importBindingAccess(account).allowed){this.state.status='forbidden';this.emit();return;}
      const stamp=++this.generation;this.state.busy=true;this.state.status='loading';this.emit();
      try{const options=validateOptions(await this.api.importBindingOptions({account_key:account}),account);if(stamp!==this.generation)return;this.state.options=options;this.state.binding={account_key:account,source_id:'',source_version:'',account_namespace:''};this.state.status='select';}
      catch(error){if(stamp===this.generation){this.state.status='error';this.state.error=error.message;}}
      finally{if(stamp===this.generation){this.state.busy=false;this.emit();}}
    }
    draft(source,namespace){
      if(!this.state.options)return;this.generation++;this.state.plan=null;this.state.busy=false;this.state.error=null;this.state.status='select';
      const selected=this.state.options.sources.find(row=>source&&row.source_id===source.source_id&&row.source_version===source.source_version);
      this.state.binding={account_key:this.state.account,source_id:selected?selected.source_id:'',source_version:selected?selected.source_version:'',account_namespace:namespace};this.emit();
    }
    async inspect(){
      const binding=this.state.binding;if(this.state.busy||!binding||!this.api.importBindingAccess(this.state.account).allowed||!fields.every(key=>bounded(binding[key],key==='source_version'?80:key==='account_namespace'?256:200)))throw Error('selection_required');
      const selected=this.state.options.sources.some(row=>row.source_id===binding.source_id&&row.source_version===binding.source_version);if(!selected)throw Error('selection_required');
      const captured={...binding},stamp=++this.generation;this.state.busy=true;this.state.plan=null;this.state.status='checking';this.emit();
      try{const value=await this.api.importBindingInspect({binding:captured});if(stamp!==this.generation)return;
        if(!value||value.format!=='dfa-import-registration-plan-1'||!same(value.binding,captured)||!value.account||value.account.account_key!==captured.account_key||!value.source||value.source.source_id!==captured.source_id||value.source.source_version!==captured.source_version||value.source.source_kind!=='unverified_export'||value.persisted!==false||value.business_import_committed!==false||value.commit_authorized!==false||value.real_write_enabled!==false||value.source_revalidated!==false||value.authority!=='pending_only'||!/^[0-9a-f]{64}$/.test(value.registration_quote))throw Error('bad_response');
        this.state.plan=value;this.state.status='reviewed';
      }catch(error){if(stamp===this.generation){this.state.status='error';this.state.error=error.message;}}
      finally{if(stamp===this.generation){this.state.busy=false;this.emit();}}
    }
  }
  function mount(root,api,doc){
    const make=(tag,text)=>{const node=doc.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
    const status=make('p'),account=make('p'),sourceLabel=make('label','已登记来源（仍待校验）'),source=make('select'),namespaceLabel=make('label','本批数据分区'),namespace=make('input'),check=make('button','核对账号与来源'),result=make('div');status.setAttribute('role','status');namespace.maxLength=256;namespace.autocomplete='off';check.type='button';sourceLabel.append(source);namespaceLabel.append(namespace);root.replaceChildren(status,account,sourceLabel,namespaceLabel,check,result);
    let previous=null;const controller=new BindingController(api,state=>{
      const labels={idle:'选择下方已授权账号后核对导入登记。',forbidden:'当前会话没有导入核对权限。',loading:'正在读取此账号的来源登记…',select:'明确选择来源并填写本批数据分区，再核对登记。',checking:'正在核对账号与来源…',reviewed:'登记核对通过；聊天记录尚未保存，来源真实性仍待核验。',error:'核对暂不可用，请检查登记、权限与来源范围后重试。'};status.textContent=labels[state.status];account.textContent=state.options?'账号 '+state.account+' · 抖音账号标识 '+state.options.account.platform_uid+' · '+(state.options.account.verified?'登记已核验':'登记待核验'):'';
      if(previous!==state.options){previous=state.options;source.replaceChildren();const blank=make('option','请选择来源');blank.value='';source.append(blank);for(const row of state.options?state.options.sources:[]){const option=make('option',row.source_id+' / '+row.source_version);option.value=JSON.stringify([row.source_id,row.source_version]);source.append(option);}}
      source.value=state.binding&&state.binding.source_id?JSON.stringify([state.binding.source_id,state.binding.source_version]):'';namespace.value=state.binding?state.binding.account_namespace:'';source.disabled=namespace.disabled=!state.options||state.busy;check.disabled=!state.options||state.busy||!state.binding||!state.binding.source_id||!bounded(state.binding.account_namespace,256);result.replaceChildren();
      if(state.plan){result.append(make('p','已核对：'+state.plan.binding.account_key+' / '+state.plan.binding.source_id+' / '+state.plan.binding.source_version),make('p','数据分区：'+state.plan.binding.account_namespace),make('p','本次仅核对已登记信息；账号归属、原文与后续保存需按导入计划分别确认。'));}
    });
    let candidatePanel=null;
    if(globalThis.DSHCandidateReview){const candidateRoot=make('section');root.append(candidateRoot);candidatePanel=globalThis.DSHCandidateReview.mount(candidateRoot,api,doc);}
    function draft(){let choice=null;try{const keys=JSON.parse(source.value);choice={source_id:keys[0],source_version:keys[1]};}catch{}controller.draft(choice,namespace.value);if(candidatePanel)candidatePanel.invalidate();}
    source.addEventListener('change',draft);namespace.addEventListener('input',draft);check.addEventListener('click',()=>controller.inspect().catch(()=>{status.textContent='请完整选择来源并填写数据分区。';}));
    return {controller,selectAccount:account=>Promise.all([controller.selectAccount(account),candidatePanel?candidatePanel.selectAccount(account):Promise.resolve()]),clear:()=>{controller.clear();if(candidatePanel)candidatePanel.clear();}};
  }
  return {BindingController,mount};
});
