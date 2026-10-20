(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DSHProfileEditorProtocol=api;})(globalThis,function(){
  'use strict';
  const tags=['wechat','maintenance','spark','clip','vip','star','local_time'];
  const structured=['location','birthday','anniversary','reminder_settings'];
  function field(name){return typeof name==='string'&&(['display_name','note',...structured,...tags.map(t=>'formal_tag.'+t)].includes(name)||/^(free_term|entitlement\.(?:vip|star))\.[A-Za-z0-9_-]{1,100}$/.test(name));}
  function exact(value,keys){const d=value&&typeof value==='object'&&!Array.isArray(value)&&Object.getOwnPropertyDescriptors(value);if(!d||Reflect.ownKeys(d).length!==keys.length||keys.some(k=>!d[k]||!Object.hasOwn(d[k],'value')))throw Error('invalid_request');}
  function revision(value){if(!Array.isArray(value)||value.length>128||value.some(v=>typeof v!=='string'||!v||v.length>256)||new Set(value).size!==value.length)throw Error('invalid_request');return [...value];}
  function isoDay(value){if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value)||value.startsWith('0000'))throw Error('invalid_request');const d=new Date(value+'T00:00:00Z');if(!Number.isFinite(d.getTime())||d.toISOString().slice(0,10)!==value)throw Error('invalid_request');return value;}
  function structuredKeys(name){return name==='location'?['country','city','timezone']:name==='birthday'?['month','day','calendar','subject','source_text']:name==='anniversary'?['origin_date','source_text','manual']:name==='reminder_settings'?['vip','star','birthday','anniversary']:name.startsWith('entitlement.')?['start','end','source_text']:null;}
  function fieldValue(name,value,clear=false){
    if(!field(name)||clear&&value!=='')throw Error('invalid_request');if(clear)return '';
    const fail=()=>{throw Error('invalid_request');};
    if(name.startsWith('formal_tag.')){if(typeof value!=='boolean')fail();return value;}
    const keys=structuredKeys(name);
    if(!keys){if(typeof value!=='string'||value.length>10000||value.includes('\0'))fail();return value;}
    exact(value,keys);
    if('source_text'in value&&(typeof value.source_text!=='string'||value.source_text.length>10000||value.source_text.includes('\0')))fail();
    if(name==='location'){if(Object.values(value).some(v=>typeof v!=='string'||!v.trim()||v.length>200)||!/^[A-Za-z0-9_+\-/]+$/.test(value.timezone)||value.timezone.includes('..')||value.timezone.startsWith('/'))fail();}
    else if(name==='birthday'){
      if(!Number.isInteger(value.month)||value.month<1||value.month>12||!Number.isInteger(value.day)||value.day<1||value.day>31||!['gregorian','lunar','lunar_leap','unknown'].includes(value.calendar)||!['self','other','unknown'].includes(value.subject))fail();
      if(value.calendar==='gregorian')isoDay('2000-'+String(value.month).padStart(2,'0')+'-'+String(value.day).padStart(2,'0'));
      if(['lunar','lunar_leap'].includes(value.calendar)&&value.day>30)fail();
    }else if(name==='anniversary'){isoDay(value.origin_date);if(typeof value.manual!=='boolean')fail();}
    else if(name==='reminder_settings'){if(Object.values(value).some(v=>!Number.isInteger(v)||v<0||v>365))fail();}
    else{isoDay(value.start);isoDay(value.end);if(value.end<value.start)fail();}
    return Object.fromEntries(keys.map(k=>[k,value[k]]));
  }
  function command(value){
    const action=value&&Object.getOwnPropertyDescriptor(value,'action')?.value;
    if(action==='read'){exact(value,['action']);return {action};}
    if(action==='field'){
      exact(value,['action','field','value','clear','expected_revision']);
      if(typeof value.clear!=='boolean')throw Error('invalid_request');
      return {...value,value:fieldValue(value.field,value.value,value.clear),expected_revision:revision(value.expected_revision)};
    }
    if(action==='membership'){
      exact(value,['action','group_id','member','expected_revision']);if(typeof value.group_id!=='string'||!/^[a-f0-9]{32}$/.test(value.group_id)||typeof value.member!=='boolean')throw Error('invalid_request');
      return {...value,expected_revision:revision(value.expected_revision)};
    }
    throw Error('invalid_request');
  }
  function snapshot(value,binding){
    if(!value||value.format!=='native-profile-editor-1'||value.account_key!==binding.account_key||value.person_key!==binding.person_key||!value.fields||!Array.isArray(value.groups)||value.groups.length>500||!['all','note','none'].includes(value.access?.fields)||typeof value.access.membership!=='boolean'||value.access.membership&&value.access.fields!=='all')throw Error('bad_response');
    const fields={};if(Object.keys(value.fields).length>1000)throw Error('bad_response');
    for(const [name,row]of Object.entries(value.fields)){
      const group=/^group\.[a-f0-9]{32}$/.test(name);if(!field(name)&&!group)throw Error('bad_response');
      if(!row||!['effective','conflict','empty'].includes(row.state))throw Error('bad_response');
      let val=row.effective_value;
      if(val!==null){if(group){if(typeof val!=='boolean')throw Error('bad_response');}else val=fieldValue(name,val);}
      fields[name]={state:row.state,effective_value:val,revision:revision(row.revision)};
    }
    const seen=new Set(),groups=value.groups.map(g=>{if(!g||typeof g.group_id!=='string'||!/^[a-f0-9]{32}$/.test(g.group_id)||seen.has(g.group_id)||typeof g.name!=='string'||g.name.length>60||typeof g.deleted!=='boolean'||!['effective','conflict','empty'].includes(g.state))throw Error('bad_response');seen.add(g.group_id);return {group_id:g.group_id,name:g.name,deleted:g.deleted,state:g.state};});
    return {format:value.format,fields,groups,access:{fields:value.access.fields,membership:value.access.membership}};
  }
  function allowed(access,cmd){return cmd.action==='read'||(cmd.action==='membership'?access.membership===true:access.fields==='all'||access.fields==='note'&&cmd.field==='note');}
  return {field,exact,revision,command,snapshot,allowed,tags,structuredKeys,fieldValue,isoDay};
});
