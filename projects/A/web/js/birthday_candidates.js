(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DSHBirthdays=api;})(globalThis,function(){
  'use strict';
  function mount(target,api,binding,isCurrent,adopt,document=globalThis.document){
    const n=(tag,text)=>{const value=document.createElement(tag);if(text!==undefined)value.textContent=text;return value;};
    const read=n('button','读取已保存生日候选'),more=n('button','读取下一页候选'),status=n('p','仅普通规则候选；来源不足、人物或历法不明时保持待核对。'),list=n('div');for(const b of [read,more])b.type='button';more.hidden=true;target.append(read,more,status,list);
    let generation=0,busy=false,disposed=false,cursor=null;
    const live=stamp=>!disposed&&generation===stamp&&target.isConnected&&isCurrent();
    const matches=data=>data?.format==='birthday-candidates-1'&&data.account_key===binding.account_key&&data.person_key===binding.person_key;
    function row(item){const area=n('div'),v=item.value||{},details=n('div'),inspect=n('button','核对这条原文'),use=n('button','采用为生日表单输入');inspect.type=use.type='button';use.disabled=true;let source=null;const stamp=generation;
      area.append(n('p',String(v.month)+'月'+String(v.day)+'日 · '+({gregorian:'公历',lunar:'农历',lunar_leap:'农历闰月',unknown:'历法未知'}[v.calendar]||'历法未知')+' · '+({self:'本人表达候选',other:'提及其他人物',unknown:'人物待核对'}[v.subject]||'人物待核对')),inspect,details,use);list.append(area);
      inspect.addEventListener('click',async()=>{if(!live(stamp)||inspect.disabled)return;inspect.disabled=true;use.disabled=true;source=null;details.replaceChildren();try{const data=await api.birthdaySource({...binding,message_key:item.source?.message_key,candidate_key:item.candidate_key});if(!live(stamp))return;if(!matches(data)||data.candidate_key!==item.candidate_key||data.source?.body_revision!==item.source?.body_revision||data.source?.body_sha256!==item.source?.body_sha256||typeof data.body!=='string')throw Error('bad_response');source=data;details.append(n('p','已保存原文（'+(data.observed_at||'消息时间未知')+'）：'),n('blockquote',data.body),n('p','请核对人物、历法和日期；采用只填表单，正式资料不会自动改变。'));use.disabled=(data.value?.flags||[]).includes('invalid_date')||!api.editingAccess(binding.account_key,'birthday').allowed;}
        catch{if(live(stamp))details.append(n('p','来源或正文修订已变化，请重新读取候选。'));}finally{if(live(stamp))inspect.disabled=false;}});
      use.addEventListener('click',async()=>{if(!live(stamp)||!source||use.disabled)return;use.disabled=true;try{const current=await api.birthdaySource({...binding,message_key:item.source.message_key,candidate_key:item.candidate_key});if(!live(stamp))return;if(!matches(current)||current.candidate_key!==source.candidate_key||current.source?.body_sha256!==source.source.body_sha256||!current.profile_value)throw Error('bad_response');adopt(current.profile_value);details.append(n('p','已填入生日表单，尚未保存；请核对后点击该字段的保存按钮。'));}catch{if(live(stamp))details.append(n('p','未采用：来源已变化或暂不可用。'));}finally{if(live(stamp))use.disabled=false;}});
    }
    async function load(next=false){if(busy||disposed||!isCurrent())return;const stamp=++generation;busy=true;read.disabled=more.disabled=true;list.replaceChildren();status.textContent='核对已保存且身份可靠的个人来信…';try{const data=await api.birthdayCandidates({...binding,...(next&&cursor?{cursor}:{})});if(!live(stamp))return;if(!matches(data)||!Array.isArray(data.items)||data.items.length>160)throw Error('bad_response');for(const item of data.items)row(item);cursor=data.next_cursor||null;more.hidden=!cursor;status.textContent=data.items.length?'仅当前页候选，需逐条人工核对。':'当前页没有可采用候选；不代表此人没有生日或平台历史完整。';}
      catch{if(live(stamp)){cursor=null;more.hidden=true;status.textContent='候选暂不可用，原正式生日保留。';}}
      finally{if(live(stamp)){busy=false;read.disabled=more.disabled=false;}}}
    read.addEventListener('click',()=>load());more.addEventListener('click',()=>load(true));
    return {dispose(){disposed=true;generation++;target.replaceChildren();}};
  }
  return {mount};
});
