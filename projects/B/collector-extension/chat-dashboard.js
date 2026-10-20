import {EnhancedCollector} from './chat-collector.js';
import {ChatBrowserSource} from './chat-source.js';
import {ChatNativeClient,bindIncremental,waitForChatReady} from './chat-client.js';
import {localDate,raw} from './core.js';
const $=id=>document.getElementById(id),host=new ChatNativeClient();let preparing=false,epoch=0,previewRun=null,previewOffset=0,previewEpoch=0;
const showError=e=>{$('state').textContent='本次操作未完成：'+(/^[A-Z_]{1,80}$/.test(e.message)?e.message:'CHAT_OPERATION_FAILED');};
function buttons(){$('start').disabled=preparing||!!collector.run;$('stop').disabled=!preparing&&!collector.run;}
const collector=new EnhancedCollector({host,source:new ChatBrowserSource(),notify:e=>{
 if(e.kind==='chat_progress')$('progress').textContent=`会话已发现 ${e.discovered} 个，已处理 ${e.processed} 个；本次消息观察 ${e.messageObservations} 条。`;
 if(e.kind==='finished'){$('state').textContent=e.reason==='completed_requested_scope'?'本次读取已结束；仍以缺口报告为准。':'本次采集已停止：'+e.reason;buttons();refresh().catch(showError);}
 if(['missing','error'].includes(e.kind)){const li=document.createElement('li');li.textContent=e.code;$('events').prepend(li);if($('events').children.length>30)$('events').lastChild.remove();}
}});
bindIncremental(collector,host,s=>{$('stats').textContent=`新增消息 ${s.messages_new}；跳过重复 ${s.duplicates_skipped}；保存图片/表情 ${s.media_saved}；媒体失败 ${s.media_failed}；缺下载地址 ${s.media_missing_url}。`;});
host.onDisconnect=()=>{if(collector.run)collector.stop().catch(()=>{});showError(Error('NATIVE_DISCONNECTED'));buttons();};
async function refresh(){await host.connect();const {runs}=await host.call('list_runs',{});$('runs').replaceChildren();for(const r of runs){const line=document.createElement('p');line.textContent=`${r.started_at} · ${r.state} · ${r.count} 条观察 `;for(const [label,act]of [['查看',()=>preview(r.run_id)],['导出加密包',async()=>{$('state').textContent=(await host.call('export',{run_id:r.run_id})).path;}]]){const b=document.createElement('button');b.textContent=label;b.disabled=label!=='查看'&&r.state==='running';b.onclick=()=>act().catch(showError);line.append(b);}$('runs').append(line);}}
function messageText(fields){const content=raw(fields.content);if(typeof content!=='string')return raw(fields['b_chat.source_description_text'])||raw(fields['b_chat.source_transcript_text'])||'';try{const p=JSON.parse(content);return typeof p.text==='string'?p.text:raw(fields['b_chat.source_description_text'])||raw(fields['b_chat.source_transcript_text'])||'';}catch{return content;}}
async function preview(run,more=false){
 if(!more){previewRun=run;previewOffset=0;previewEpoch++;$('preview').replaceChildren();}
 const ticket=previewEpoch;$('nextPreview').disabled=true;
 const r=await host.call('preview',{run_id:run,offset:previewOffset});if(ticket!==previewEpoch||run!==previewRun)return;
 previewOffset=r.next_offset;$('previewState').textContent=`已查看 ${previewOffset} / ${r.total} 条保存记录（包含会话信息和覆盖说明）。`;
 for(const entry of r.records){
  const o=entry.observation,p=document.createElement('p');
  if(o.domain==='capability'){p.textContent='覆盖说明：'+(raw(o.fields.status)||o.issues.join('、'));$('preview').append(p);continue;}
  if(o.domain!=='chat')continue;
  const f=o.fields,kind=raw(f['b_chat.media_kind']);
  p.textContent=[raw(f['b_chat.created_at_iso'])||'',raw(f['b_chat.media_label'])||'会话记录',messageText(f)].join(' ');$('preview').append(p);
  if(['image','sticker'].includes(kind)&&raw(f['b_chat.media_file_saved'])!==true){const note=document.createElement('span');note.textContent='（文件尚未保存）';p.append(note);}
  const id=raw(f['b_chat.media_asset_id']);if(!id)continue;
  const b=document.createElement('button');b.textContent='查看已保存图片/表情';b.onclick=async()=>{
   b.disabled=true;
   try{let offset=0,total=1,mime,parts=[];
    while(offset<total){const a=await host.call('media_preview',{run_id:run,asset_id:id,offset});if(ticket!==previewEpoch)return;if(a.next_offset<=offset)throw Error('MEDIA_INTEGRITY');parts.push(Uint8Array.from(atob(a.data),c=>c.charCodeAt(0)));offset=a.next_offset;total=a.total;mime=a.mime;}
    const img=document.createElement('img'),url=URL.createObjectURL(new Blob(parts,{type:mime}));
    img.onload=()=>URL.revokeObjectURL(url);img.onerror=()=>{URL.revokeObjectURL(url);showError(Error('MEDIA_PREVIEW_FAILED'));};img.style.maxWidth='240px';img.src=url;p.append(img);b.remove();
   }catch(e){showError(e);b.disabled=false;}
  };p.append(b);
 }
 $('nextPreview').hidden=previewOffset>=r.total;$('nextPreview').disabled=false;
}
$('nextPreview').onclick=()=>preview(previewRun,true).catch(showError);
$('refresh').onclick=()=>refresh().catch(showError);
$('stop').onclick=async()=>{epoch++;preparing=false;await collector.stop().catch(showError);buttons();};
$('start').onclick=async()=>{preparing=true;const current=++epoch;buttons();try{await host.connect();if(current!==epoch)return;const tab=await chrome.tabs.create({url:'https://www.douyin.com/chat',active:false});for(let i=0;i<60;i++){if(current!==epoch)return;const t=await chrome.tabs.get(tab.id);if(t.status==='complete')break;await new Promise(r=>setTimeout(r,500));}if(current!==epoch)return;$('state').textContent='正在等待聊天页面准备完成…';await waitForChatReady(collector.source,tab.id,()=>{if(current!==epoch)throw Error('CANCELLED');});const day=localDate(new Date());const e=await collector.start({domains:['account','capability','chat'],start_date:day,end_date:day,include_body:true,retention_days:7},{chatTab:tab.id,readAll:true});preparing=false;buttons();$('state').textContent='正在读取聊天并保存新内容…';await collector.capture(e);}catch(e){showError(e);}finally{preparing=false;buttons();}};
