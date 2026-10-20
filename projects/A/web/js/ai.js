(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.DSHAI=value;})(globalThis,function(){
  'use strict';
  function label(state){return ({unavailable:'本地模型暂不可用，当前没有参考结果。',queued:'参考请求已排队；请稍后刷新此参考状态。',running:'本地模型正在生成参考。',ready:'仅供人工判断的参考；请核对原文引用。',unknown:'没有有依据的参考结果。',stale:'正文或上下文已变化，旧参考已隐藏。',hidden:'参考已隐藏。',canceled:'参考已取消。',disabled:'此类参考已关闭。',failed:'参考处理失败；基础消息与资料不受影响。',timed_out:'参考处理超时；基础消息与资料不受影响。',corrected:'人工纠正的参考。',correction_conflict:'纠正存在并发冲突，结果暂不显示。'})[state]||'参考状态未知。';}
  class ReferenceController {
    constructor(api,binding,current=()=>true){this.api=api;this.binding={...binding};this.current=current;this.generation=0;this.job=null;this.disposed=false;}
    async action(method,data){
      if(this.disposed||!this.current())return null;
      const metadata=method==='aiStatus',stamp=metadata?this.generation:++this.generation;
      const binding={...this.binding},purpose=method==='aiRequest'?data.purpose:this.job&&this.job.purpose;
      const query={...binding,...data};if(['aiJob','aiControl'].includes(method))delete query.person_key;
      const result=await this.api[method](query);
      if(this.disposed||(!metadata&&stamp!==this.generation)||!this.current())return null;
      if(['aiRequest','aiJob','aiControl'].includes(method)){
        // A disabled request creates no job; all other responses must identify
        // the requested person, purpose and (for reads/controls) existing job.
        const disabled=method==='aiRequest'&&result&&result.state==='disabled'&&result.job_key===null&&result.result===null&&
          Object.keys(result).sort().join()==='job_key,result,state';
        if(!disabled&&(!result||typeof result!=='object'||Array.isArray(result)||
          result.account_key!==binding.account_key||result.person_key!==binding.person_key||
          !['emotion','topics','reply'].includes(result.purpose)||result.purpose!==purpose||
          typeof result.job_key!=='string'||!result.job_key||(method!=='aiRequest'&&result.job_key!==query.job_key)))throw new Error('bad_response');
      }
      return result;
    }
    async request(purpose){const result=await this.action('aiRequest',{purpose,on_demand:true});if(result)this.job=result;return result;}
    async refresh(){if(!this.job||!this.job.job_key)return null;const result=await this.action('aiJob',{job_key:this.job.job_key});if(result)this.job=result;return result;}
    async hide(){if(!this.job||!this.job.job_key)return null;const result=await this.action('aiControl',{job_key:this.job.job_key,operation:'hide',value:null,expected_revision:[...this.job.revision]});if(result)this.job=result;return result;}
    dispose(){this.disposed=true;this.generation++;this.job=null;}
  }
  class ReferencePoller {
    constructor(controller,onResult,{schedule=fn=>setTimeout(fn,800),cancel=id=>clearTimeout(id),limit=120,onError=()=>{}}={}){this.controller=controller;this.onResult=onResult;this.schedule=schedule;this.cancel=cancel;this.limit=limit;this.onError=onError;this.epoch=0;this.timer=null;this.key=null;this.remaining=0;}
    stop(){this.epoch++;if(this.timer!==null)this.cancel(this.timer);this.timer=null;}
    start(job){this.stop();if(!job||!job.job_key||!['queued','running'].includes(job.state))return;this.key=job.job_key;this.remaining=this.limit;this.queue(this.epoch);}
    queue(epoch){if(this.remaining<=0)return;this.timer=this.schedule(()=>this.poll(epoch));}
    async poll(epoch){
      this.timer=null;const c=this.controller;
      if(epoch!==this.epoch||c.disposed||!c.current()||!c.job||c.job.job_key!==this.key||this.remaining--<=0)return;
      try{const job=await c.refresh();if(epoch!==this.epoch||!job||c.disposed||!c.current())return;this.onResult(job);if(['queued','running'].includes(job.state))this.queue(epoch);}
      catch(error){if(epoch!==this.epoch||c.disposed||!c.current())return;this.stop();this.onError(error);}
    }
  }
  function render(target,api,binding,current,polling={}){
    const doc=target.ownerDocument;const make=(tag,text)=>{const node=doc.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
    const state=make('p','正在读取本地模型状态；不启动推理。');const resultArea=make('div');target.append(state,resultArea);
    const controller=new ReferenceController(api,binding,current);let preference=null,preferencePending=false,workerActive=false,manualActive=0,actionsAllowed=false,replyOnly=false,statusPending=false,statusRetry=false;
    const poller=new ReferencePoller(controller,job=>show(job),{...polling,onError:error=>clearResult(error)});const dispose=controller.dispose.bind(controller);
    const toggle=make('input');toggle.type='checkbox';toggle.disabled=true;const toggleLabel=make('label','显示此人的情绪注释');toggleLabel.append(toggle);target.append(toggleLabel);
    const request=make('button','主动查看短接话参考');request.type='button';const topics=make('button','查看有引用的话题参考');topics.type='button';
    request.disabled=topics.disabled=true;
    const refresh=make('button','刷新此参考状态');refresh.type='button';const hide=make('button','隐藏此参考');hide.type='button';refresh.disabled=hide.disabled=true;target.append(request,topics,refresh,hide);
    const emotion=make('button','主动查看情绪参考');emotion.type='button';emotion.disabled=true;target.append(emotion);
    const active=()=>!controller.disposed&&current();
    function updateRequests(){
      request.disabled=!actionsAllowed||manualActive>0||preferencePending;
      topics.disabled=replyOnly||request.disabled;
      emotion.disabled=replyOnly||request.disabled||!preference||preference.enabled!==true||!toggle.checked;
      toggle.disabled=replyOnly||request.disabled||!preference;
    }
    controller.dispose=()=>{poller.stop();dispose();statusRetry=false;request.disabled=topics.disabled=emotion.disabled=refresh.disabled=hide.disabled=toggle.disabled=true;resultArea.replaceChildren();state.textContent=label('stale');};
    function clearResult(error){if(!active())return;controller.job=null;resultArea.replaceChildren();refresh.disabled=hide.disabled=true;state.textContent=error&&error.message==='ai_wait_timeout'?'等待超时，本次处理状态尚未确认；旧参考已隐藏，没有自动重发。':'本地参考暂不可用或依据已变化，旧参考已隐藏，请重新主动查询。';}
    function show(job){if(!job||!active())return;state.textContent=label(job.state);resultArea.replaceChildren();refresh.disabled=preferencePending||!job.job_key;hide.disabled=replyOnly||preferencePending||!job.job_key;
      if(!['ready','corrected'].includes(job.state)||!job.result)return;
      const showEmotion=!replyOnly&&preference&&preference.enabled===true&&toggle.checked;
      const entries=[...(showEmotion&&job.result.emotion?[{text:'可能情绪：'+job.result.emotion.label,refs:job.result.emotion.refs}]:[]),...(job.result.topics||[]).slice(0,2),...(job.result.replies||[]).slice(0,2)];
      for(const item of entries){resultArea.append(make('p',item.text));const details=make('details');details.append(make('summary','查看原文引用'));for(const ref of item.refs||[])details.append(make('blockquote',ref.quote));resultArea.append(details);}
    }
    async function perform(fn){if(!active()||!actionsAllowed||preferencePending)return;poller.stop();manualActive++;updateRequests();resultArea.replaceChildren();let stamp;try{const pending=fn();stamp=controller.generation;show(await pending);}catch(error){if(stamp===controller.generation)clearResult(error);}finally{manualActive--;if(active()&&manualActive===0){updateRequests();if(workerActive&&controller.job)poller.start(controller.job);}}}
    request.addEventListener('click',()=>perform(()=>controller.request('reply')));topics.addEventListener('click',()=>{if(!replyOnly)return perform(()=>controller.request('topics'));});refresh.addEventListener('click',()=>statusRetry?readStatus():perform(()=>controller.refresh()));hide.addEventListener('click',()=>{if(!replyOnly)return perform(()=>controller.hide());});
    emotion.addEventListener('click',()=>{if(!replyOnly&&!emotion.disabled)return perform(()=>controller.request('emotion'));});
    toggle.addEventListener('change',async()=>{
      if(replyOnly||!preference||!active()||!actionsAllowed||preferencePending||manualActive>0)return;
      const enabled=toggle.checked;let notice=null;
      preferencePending=true;poller.stop();updateRequests();show(controller.job);refresh.disabled=hide.disabled=true;
      try{
        const value=await controller.action('aiPreference',{purpose:'emotion',enabled,expected_revision:[...preference.revision]});
        if(value){preference=value;toggle.checked=value.enabled;notice=value.enabled?'已保存情绪注释开关；可主动查看参考。':'情绪注释已关闭。';}
      }catch{if(active()){toggle.checked=preference.enabled;notice='开关未保存，请重新查询最新版本。';}}
      finally{preferencePending=false;if(active()){updateRequests();show(controller.job);if(notice)state.textContent=notice;if(workerActive&&controller.job)poller.start(controller.job);}}
    });
    function readStatus(){
      if(!active()||statusPending||manualActive>0||controller.job)return Promise.resolve();
      statusPending=true;statusRetry=false;refresh.disabled=true;
      state.textContent='正在读取本地模型状态；不启动推理。';
      return controller.action('aiStatus',{}).then(value=>{
      if(!value||!active())return;
      refresh.textContent='刷新此参考状态';
      replyOnly=value.processing==='explicit_local_reply';
      actionsAllowed=value.request_enabled!==false;
      if(replyOnly)actionsAllowed=value.request_enabled===true&&Array.isArray(value.allowed_purposes)&&value.allowed_purposes.includes('reply');
      topics.hidden=hide.hidden=toggleLabel.hidden=emotion.hidden=replyOnly;
      request.disabled=!actionsAllowed||manualActive>0;topics.disabled=replyOnly||request.disabled;
      if(!actionsAllowed){
        workerActive=false;preference=null;poller.stop();controller.job=null;resultArea.replaceChildren();
        refresh.disabled=hide.disabled=toggle.disabled=emotion.disabled=true;
        state.textContent=value.processing==='local_notes_only'||value.reason==='reliable_chat_context_unavailable'?'可靠聊天上下文尚未接通，回复参考未启用。':'本次运行尚未开放参考处理。';
        return;
      }
      workerActive=value.available===true&&['explicit_fictional_worker','explicit_local_reply'].includes(value.scheduler);
      if(!controller.job&&manualActive===0)state.textContent=value.available?(workerActive?'本机参考处理已启用；真实资料质量仍待核验。':'本地登记存在；运行与质量仍待核验，参考处理尚未启用。'):'本地模型尚未核验；运行与质量未测试，基础功能可继续使用。';
      if(workerActive&&controller.job&&manualActive===0)poller.start(controller.job);
      preference=replyOnly?null:value.preferences&&value.preferences.emotion;if(preference)toggle.checked=preference.enabled;updateRequests();
      }).catch(error=>{if(active()&&!controller.job&&manualActive===0){statusRetry=true;refresh.disabled=false;refresh.textContent='重新检查参考状态';state.textContent=(error.message==='ai_wait_timeout'?'读取本机处理状态超时，尚未发起新回复。':'本地参考状态暂不可用或当前身份无权读取。')+'可点击“重新检查参考状态”，只查询状态，不生成回复。';}}).finally(()=>{statusPending=false;});
    }
    readStatus();
    return controller;
  }
  return {label,ReferenceController,ReferencePoller,render};
});
