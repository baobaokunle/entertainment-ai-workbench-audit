(function(root,factory){const node=typeof module==='object'&&module.exports;const api=factory(node?require('../lib/profile_editor.js'):root.DSHProfileEditorProtocol);if(node)module.exports=api;else root.DSHProfileEditorUI=api;})(globalThis,function(P){
  'use strict';
  const names={display_name:'称呼',note:'备注',wechat:'微信',maintenance:'维护',spark:'火花',clip:'直播切片协作者',vip:'VIP会员',star:'星守护',local_time:'当地时间'};
  function mount(document,request,onSaved=()=>{}){
    const make=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
    const element=make('div'),open=make('button','编辑资料与分组'),area=make('div'),status=make('p');open.type='button';area.hidden=true;element.hidden=true;element.className='profile-editor';element.append(open,status,area);
    let serial=0,enabled=false,busy=false,model=null,controls=[];
    function clear(){serial++;enabled=false;busy=false;model=null;controls=[];element.hidden=true;area.hidden=true;area.replaceChildren();status.textContent='';}
    const live=stamp=>enabled&&serial===stamp&&element.isConnected;
    function permission(cmd){return model&&P.allowed(model.access,cmd)&&(cmd.action!=='membership'||model.groups.some(g=>g.group_id===cmd.group_id&&!g.deleted&&g.state!=='conflict'));}
    function update(){for(const c of controls)c.node.disabled=busy||!permission(c.cmd)||c.locked();open.disabled=busy;}
    async function invoke(cmd,stamp){if(!live(stamp))throw Error('stale_session');const result=await request(P.command(cmd));if(!live(stamp))throw Error('stale_session');return result;}
    function structuredInput(field,label){
      const element=make('div'),inputs={},keys=P.structuredKeys(field);
      const labels={country:'国家',city:'城市',timezone:'时区（如 Asia/Shanghai）',month:'月份',day:'日期',calendar:'历法',subject:'生日人物',source_text:'原文或人工依据',origin_date:'人工确认起始日期',start:'本期开始',end:'本期结束（当天有效）',vip:'VIP提前天数',star:'星守护提前天数',birthday:'生日提前天数',anniversary:'周年提前天数'};
      for(const key of keys){
        if(key==='manual')continue;
        const choices=key==='calendar'?[['unknown','未知，需核对'],['gregorian','公历'],['lunar','农历'],['lunar_leap','农历闰月，需核对']]:key==='subject'?[['unknown','人物未知'],['self','此联系人本人'],['other','其他人物']]:null;
        const control=make(choices?'select':key==='source_text'?'textarea':'input'),title=make('label',labels[key]);
        control.setAttribute('aria-label',label+' · '+labels[key]);
        if(choices)for(const [value,text]of choices){const option=make('option',text);option.value=value;control.append(option);}
        else if(['month','day'].includes(key)||field==='reminder_settings'){control.type='number';control.min=field==='reminder_settings'?'0':'1';control.max=key==='month'?'12':key==='day'?'31':'365';control.step='1';}
        else if(['start','end','origin_date'].includes(key))control.type='date';
        else{control.type='text';control.maxLength=key==='source_text'?10000:200;}
        inputs[key]=control;title.append(control);element.append(title);
      }
      const defaults=field==='reminder_settings'?{vip:7,star:7,birthday:0,anniversary:0}:{calendar:'unknown',subject:'unknown'};
      return {element,nodes:Object.values(inputs),set(value){for(const [key,control]of Object.entries(inputs))control.value=String(value?.[key]??defaults[key]??'');},get(){
        const value={};for(const [key,control]of Object.entries(inputs)){
          if(control.type==='number'){if(!/^\d+$/.test(control.value))throw Error('invalid_request');value[key]=Number(control.value);}else value[key]=control.value;
        }if(field==='anniversary')value.manual=true;return P.fieldValue(field,value);
      }};
    }
    function line(field,label,record,membership=false,target=area){
      let original=record||{state:'empty',effective_value:null,revision:[]},revision=[...original.revision],conflict=original.state==='conflict';
      const row=make('div'),title=make('div',label),boolean=membership||field.startsWith('formal_tag.'),structured=P.structuredKeys(field)?structuredInput(field,label):null,input=structured?structured.element:make(boolean?'select':'textarea'),message=make('p'),save=make('button','保存'),cancel=make('button','取消修改'),rebase=make('button','核对最新值并保留输入'),clearBox=make('input'),clearLabel=make('label','清空此字段');
      if(boolean){for(const [value,text]of [['','未知／未记录'],['true','是'],['false','否']]){const option=make('option',text);option.value=value;input.append(option);}}
      else if(!structured){input.rows=field==='note'?3:2;input.maxLength=10000;}
      input.setAttribute('aria-label',label);clearBox.type='checkbox';clearLabel.prepend(clearBox);clearLabel.hidden=membership;
      const setOriginal=()=>{if(structured)structured.set(original.effective_value);else input.value=boolean?(original.effective_value===null?'':String(original.effective_value)):original.effective_value||'';clearBox.checked=false;};setOriginal();
      for(const b of [save,cancel,rebase])b.type='button';title.append(input);row.append(title,clearLabel,save,cancel,rebase,message);target.append(row);
      const cmd=membership?{action:'membership',group_id:field.slice(6)}:{action:'field',field};
      const stamp=serial;
      for(const node of [...(structured?structured.nodes:[input]),clearBox,save,cancel,rebase])controls.push({node,cmd,locked:()=>node===save&&conflict});
      if(conflict)message.textContent='存在不同版本；先核对最新值，再明确保存。';
      cancel.addEventListener('click',()=>{if(live(stamp)&&!busy){setOriginal();message.textContent='已取消未保存修改。';}});
      rebase.addEventListener('click',async()=>{
        if(!live(stamp)||busy)return;busy=true;update();
        try{const fresh=await invoke({action:'read'},stamp),record=fresh.fields[field]||{state:'empty',effective_value:null,revision:[]};
          model=fresh;original=record;conflict=record.state==='conflict';
          if(conflict)message.textContent='仍存在不同版本，请到工作台核对冲突版本；输入已保留，侧栏暂不能覆盖。';
          else{revision=[...record.revision];message.textContent='最新已存值：'+(record.effective_value===null?'未知':typeof record.effective_value==='object'?JSON.stringify(record.effective_value):String(record.effective_value))+'。你的输入已保留；核对后点保存。';}
        }catch{if(live(stamp))message.textContent='读取失败，输入保留；尚未保存。';}
        finally{if(live(stamp)){busy=false;update();}}
      });
      save.addEventListener('click',async()=>{
        if(!live(stamp)||busy||conflict||!permission(cmd))return;
        let value;try{value=clearBox.checked?'':structured?structured.get():boolean?(input.value==='true'?true:input.value==='false'?false:null):input.value;}catch{message.textContent='输入格式无效；请核对日期、时区、人物与天数，尚未提交。';return;}
        if(boolean&&value===null&&!clearBox.checked){message.textContent='请选择是／否，或明确清空字段。';return;}
        const change=membership?{...cmd,member:value,expected_revision:revision}:{...cmd,value:clearBox.checked?'':value,clear:clearBox.checked,expected_revision:revision};
        busy=true;update();message.textContent='正在保存…';
        try{const result=await invoke(change,stamp);if(result.status!=='saved')throw Error('bad_response');
          revision=P.revision(result.revision);original={state:'effective',effective_value:!membership&&clearBox.checked?null:value,revision};
          message.textContent='已保存到当前对象。';onSaved(result);}
        catch(error){if(live(stamp)){conflict=error.message==='conflict';message.textContent=conflict?'版本已变化，输入保留；先核对最新值。':'未确认保存结果，输入保留；请先核对最新值，勿重复提交。';}}
        finally{if(live(stamp)){busy=false;update();}}
      });
    }
    function render(){
      area.replaceChildren();controls=[];
      status.textContent=model.access.fields==='all'?'每项单独保存；分组可多选。':model.access.fields==='note'?'当前仅开放备注编辑；其他资料与分组只读。':'当前连接为只读，资料与分组暂不能修改。';
      const section=(label,expanded=false)=>{const d=make('details'),title=make('summary',label);d.className='profile-section';d.open=expanded;d.append(title);area.append(d);return d;};
      const basics=section('称呼与备注',true),groups=section('自定义分组',true),tags=section('正式标签'),dates=section('位置、日期与提醒'),entitlements=section('VIP与星守护期间'),terms=section('自由词条');
      for(const [field,label]of [['display_name','称呼'],['note','备注']])line(field,label,model.fields[field],false,basics);
      for(const tag of P.tags)line('formal_tag.'+tag,names[tag],model.fields['formal_tag.'+tag],false,tags);
      for(const [field,label]of [['location','位置与时区'],['birthday','生日'],['anniversary','周年起点'],['reminder_settings','提前提醒天数']])line(field,label,model.fields[field],false,dates);
      dates.append(make('p','生日需核对本人及历法；周年需人工确认起点。提醒天数未记录时显示默认输入，保存后才生效。'));
      for(const field of Object.keys(model.fields).filter(k=>k.startsWith('entitlement.')))line(field,field.startsWith('entitlement.vip.')?'VIP期间':'星守护期间',model.fields[field],false,entitlements);
      for(const [kind,label]of [['vip','VIP'],['star','星守护']]){const add=make('button','添加'+label+'期间');add.type='button';entitlements.append(add);const cmd={action:'field',field:'entitlement.'+kind+'.new'};controls.push({node:add,cmd,locked:()=>false});add.addEventListener('click',()=>{if(busy||!enabled||!permission(cmd))return;line('entitlement.'+kind+'.'+globalThis.crypto.randomUUID().replaceAll('-',''),label+'新期间',undefined,false,entitlements);update();});}
      for(const field of Object.keys(model.fields).filter(k=>k.startsWith('free_term.')))line(field,'自由词条',model.fields[field],false,terms);
      const add=make('button','添加自由词条');add.type='button';terms.append(add);controls.push({node:add,cmd:{action:'field',field:'free_term.new'},locked:()=>false});
      add.addEventListener('click',()=>{if(busy||!enabled||!permission({action:'field',field:'free_term.new'}))return;line('free_term.'+globalThis.crypto.randomUUID().replaceAll('-',''),'新自由词条',undefined,false,terms);update();});
      for(const group of model.groups.filter(g=>!g.deleted))line('group.'+group.group_id,group.name,model.fields['group.'+group.group_id]||{state:'empty',effective_value:false,revision:[]},true,groups);
      if(!model.groups.some(g=>!g.deleted))groups.append(make('p','暂无自定义组；可在工作台管理分组。'));
      update();
    }
    open.addEventListener('click',async()=>{
      if(!enabled||busy)return;if(!area.hidden){area.hidden=true;open.textContent='编辑资料与分组';return;}
      if(model){area.hidden=false;open.textContent='收起编辑';return;}
      const stamp=serial;busy=true;update();status.textContent='读取当前对象资料…';
      try{model=await invoke({action:'read'},stamp);area.hidden=false;open.textContent='收起编辑';render();}
      catch{if(live(stamp))status.textContent='资料暂不可用，请核对连接后重试。';}
      finally{if(live(stamp)){busy=false;update();}}
    });
    return {element,clear,activate(){clear();enabled=typeof request==='function';element.hidden=!enabled;open.disabled=false;open.textContent='编辑资料与分组';},dispose:clear};
  }
  return {mount};
});
