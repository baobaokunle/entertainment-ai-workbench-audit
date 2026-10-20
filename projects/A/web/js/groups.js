(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.DSHGroups=value;})(globalThis,function(){
  'use strict';
  function labels(items){return items.filter(item=>!item.deleted).map(item=>({...item,label:item.name+(item.state==='conflict'?'（冲突待核对）':'')}));}
  const SYSTEM={S:'S级',A:'A级',B:'B级',C:'C级',D:'D级',E:'E级',group_chat:'群聊',clip:'直播切片',vip:'VIP会员',star:'星守护'};
  async function render(area,api,binding,fields,isCurrent,refresh){
    const document=area.ownerDocument||globalThis.document;
    const create=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
    const state=create('p','正在读取共享分组…');area.append(state);
    const editable=[],writeOnly=[];let busy=false;
    const current=()=>isCurrent()&&area.isConnected;
    const access=()=>typeof api.editingAccess==='function'?api.editingAccess(binding.account_key):{allowed:false,reason:'unknown'};
    function updateControls(){
      const permission=access(),allowed=permission.allowed===true&&current();
      for(const control of editable)control.disabled=!allowed||busy;
      for(const control of writeOnly)control.hidden=!allowed;
      if(current()&&!permission.allowed)state.textContent=permission.reason==='local_notes_only'?'当前仅开放备注；共享分组只读。':'当前共享分组只读，未启用或尚未核实编辑权限。';
      return allowed&&!busy;
    }
    async function mutate(action,message,restore=()=>{}){
      if(!updateControls()){restore();return;}
      busy=true;updateControls();
      try{await action();if(current())await refresh();}
      catch{restore();if(current())state.textContent=message;}
      finally{busy=false;updateControls();}
    }
    try{
      const result=await api.groups({account_key:binding.account_key});if(!isCurrent()||!area.isConnected)return;
      state.textContent='可加入多个自定义组；共享修改按当前版本保存。';
      for(const group of labels(result.items||[])){
        const line=create('div');const label=create('label');const box=create('input');box.type='checkbox';
        const field=fields['group.'+group.group_id]||{effective_value:false,revision:[]};box.checked=field.effective_value===true;
        label.append(box,document.createTextNode(group.label));line.append(label);
        editable.push(box);
        box.addEventListener('change',()=>mutate(()=>api.groupMembership({...binding,group_id:group.group_id,member:box.checked,expected_revision:field.revision}),'分组未保存，请重新读取最新版本后核对。',()=>{box.checked=field.effective_value===true;}));
        const remove=create('button','删除此分组');remove.type='button';remove.className='secondary';line.append(remove);
        editable.push(remove);writeOnly.push(remove);
        remove.addEventListener('click',()=>mutate(()=>api.groupUpdate({account_key:binding.account_key,group_id:group.group_id,name:group.name,deleted:true,expected_revision:group.revision}),'删除未应用，请重新读取分组版本。'));
        const rename=create('details'),title=create('summary','修改组名'),input=create('input'),save=create('button','保存组名');
        input.value=group.name;input.maxLength=60;input.setAttribute?.('aria-label','修改 '+group.name+' 的组名');save.type='button';save.className='secondary';
        rename.append(title,input,save);line.append(rename);editable.push(input,save);writeOnly.push(rename);
        save.addEventListener('click',()=>{const name=input.value.trim();if(!name||name.length>60){if(current())state.textContent='组名须为1至60个字符。';return;}
          return mutate(()=>api.groupUpdate({account_key:binding.account_key,group_id:group.group_id,name,deleted:false,expected_revision:group.revision}),'改名未保存，输入已保留；请重新读取并核对分组版本。');});
        if(field.state==='conflict')line.append(create('p','成员归属存在不同版本，请核对后明确勾选。'));
        area.append(line);
      }
      const form=create('form');const name=create('input');name.required=true;name.maxLength=60;name.placeholder='新分组名称';const add=create('button','创建共享分组');add.type='submit';form.append(name,add);area.append(form);
      editable.push(name,add);writeOnly.push(form);updateControls();
      form.addEventListener('submit',e=>{e.preventDefault();return mutate(()=>api.groupCreate({account_key:binding.account_key,name:name.value}),'分组创建未保存，请重试。');});
    }catch{if(isCurrent())state.textContent='共享分组暂不可用，现有联系人和历史保留。';}
  }
  function mountDirectory(area,api,selectContact,optionsConfig={}){
    const document=area.ownerDocument,make=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
    const filter=make('select'),label=make('label','分组'),refresh=make('button','更新分组列表'),status=make('p'),list=make('ul');
    filter.setAttribute('aria-label','按分组查看联系人');refresh.type='button';refresh.className='secondary';list.className='task-list group-browser-list';
    label.append(filter);area.append(label,refresh,status,list);
    let account=null,serial=0,data=null,selected='all',person=null,keyword='';
    if(optionsConfig.workbench){const searchLabel=make('label','查找联系人'),search=make('input');searchLabel.className='contact-search';search.type='search';search.maxLength=100;search.placeholder='按已保存称呼查找';search.setAttribute('aria-label','查找联系人');search.addEventListener('input',()=>{keyword=search.value.trim().toLocaleLowerCase();render();});searchLabel.append(search);area.prepend(searchLabel);}
    function options(){filter.replaceChildren();const entries=[['all','全部已保存联系人'],...Object.entries(SYSTEM).map(([k,v])=>['system:'+k,v]),
      ...(data?.definitions||[]).filter(g=>!g.deleted).map(g=>['custom:'+g.group_id,g.name+(g.state==='conflict'?'（名称待核对）':'')])];
      if(!entries.some(([key])=>key===selected))selected='all';for(const [key,text]of entries){const option=make('option',text);option.value=key;filter.append(option);}filter.value=selected;}
    function render(){options();list.replaceChildren();filter.disabled=!data;refresh.disabled=!account;
      if(!data)return;const [kind,key]=selected.split(':');const rows=data.items.filter(item=>(selected==='all'||(kind==='system'?item.system:item.custom).includes(key))&&(!keyword||String(item.display_name||'').toLocaleLowerCase().includes(keyword)));
      status.textContent='已保存联系人 '+rows.length+' 人／群；等级依据 '+data.as_of_day+'，未知等级不放入E组。';
      for(const item of rows){const li=make('li'),text=make('span',item.display_name||'未记录称呼'),button=make('button','查看此联系人');button.type='button';button.className='secondary';
        if(optionsConfig.workbench){button.textContent=item.display_name||'未记录称呼';button.setAttribute('aria-current',String(item.person_key===person));button.setAttribute('aria-label','查看 '+(item.display_name||'未记录称呼'));}
        const stamp=serial,owner=account;button.addEventListener('click',async()=>{if(!area.isConnected||!button.isConnected||stamp!==serial||owner!==account||!data)return;try{await selectContact(item.person_key);}catch{if(stamp===serial&&button.isConnected)status.textContent='联系人暂不可用，请更新联系人和分组列表。';}});
        li.append(text,button);list.append(li);}if(!rows.length)list.append(make('li','此分组暂无已保存联系人。'));}
    async function load(value){const changed=value!==account;if(changed){selected='all';person=null;keyword='';if(optionsConfig.workbench){const search=area.querySelector('input[type=search]');if(search)search.value='';}}account=value;const stamp=++serial;data=null;list.replaceChildren();if(changed||!value)options();filter.disabled=true;refresh.disabled=!account;
      status.textContent=value?'正在读取分组…':'选择账号后查看分组。';if(!value)return;
      try{const result=await api.groupDirectory({account_key:value});if(stamp!==serial||account!==value)return;
        if(!result||result.format!=='group-directory-1'||result.account_key!==value||!/^\d{4}-\d{2}-\d{2}$/.test(result.as_of_day)||!Array.isArray(result.items)||!Array.isArray(result.definitions)||
          result.items.some(item=>item.account_key!==value||typeof item.person_key!=='string'||!Array.isArray(item.system)||!Array.isArray(item.custom)))throw Error('bad_response');
        data=result;render();
      }catch{if(stamp===serial){data=null;render();status.textContent='分组暂不可用，请点击更新重试。';}}}
    filter.addEventListener('change',()=>{selected=filter.value;render();});refresh.addEventListener('click',()=>load(account));
    render();return {load,clear:()=>load(null),refresh:()=>load(account),select(value){person=value;if(data)render();}};
  }
  return {render,labels,SYSTEM,mountDirectory};
});
