import {scalar,raw} from './core.js';

// Only the known attachment slots; never avatars, video posters or arbitrary URL search.
export function chatMediaUrls(fields) {
 const kind=raw(fields['b_chat.media_kind']);if(!['image','sticker'].includes(kind))return [];
 try {
  const p=JSON.parse(raw(fields.content));
  const lists=kind==='sticker'?[p.url?.url_list]:[
   p.resource_url?.origin_url_list,p.resource_url?.large_url_list,
   p.resource_url?.medium_url_list,p.resource_url?.thumb_url_list];
  return [...new Set(lists.flatMap(x=>Array.isArray(x)?x:[]).filter(x=>typeof x==='string'&&x.length<=8192))].slice(0,3);
 }catch{return [];}
}

// Candidate content labels only; never replace native message types or infer intent.
// Rules are sourced from the pinned public exporter, not a platform type contract.
export function annotateChatFields(fields,includeBody=true) {
 const out=Object.fromEntries(Object.entries(fields).filter(([k])=>includeBody||!/(?:^|[./_])(text|content|body)(?:$|[./_])/i.test(k)));
 let payload=null,kind='unknown',label=includeBody?'未识别消息':'未读取正文，媒体类型未知',basis='unavailable';
 const body=raw(out.content);
 if(typeof body==='string')try{const p=JSON.parse(body);if(p&&typeof p==='object'&&!Array.isArray(p))payload=p;}catch{}
 if(payload){
  const t=payload.aweType,object=v=>v&&typeof v==='object'&&!Array.isArray(v);
  const duration=payload.duration??payload.resource_url?.duration;
  const voice=object(payload.resource_url)&&((typeof duration==='number'&&Number.isFinite(duration)&&duration>=0)||(typeof duration==='string'&&/^\d+(?:\.\d+)?$/.test(duration)));
  if(object(payload.video)||[11054,11055,11063,11066,11067,11069,11070].includes(t)){kind='video';label='视频或转发内容（不处理）';}
  else if([2702,2703,2704].includes(t)){kind='image';label='图片';}
  else if([500,501,507,508,510,514,516].includes(t)){kind='sticker';label='表情包';}
  else if(voice){kind='voice';label='语音';}
  else if([0,700,701,703].includes(t)&&typeof payload.text==='string'){kind='text';label='文字';}
  basis=kind==='unknown'?'unrecognized_payload':'pinned_exporter_payload_rules';
  if(['sticker','image'].includes(kind)){
   const description=kind==='sticker'?payload.display_name:payload.description;
   if(typeof description==='string'&&description.length<=32768)out['b_chat.source_description_text']=scalar(description);
  }
 }
 const transcript=raw(out['b_chat.source_transcript_text']);
 if(kind==='voice'||typeof transcript==='string')out['b_chat.transcript_status']=scalar(typeof transcript==='string'&&transcript.trim()?'source_text_unreviewed':'pending');
 out['b_chat.media_kind']=scalar(kind);out['b_chat.media_label']=scalar(label);
 out['b_chat.media_basis']=scalar(basis);out['b_chat.media_verified']=scalar(false);
 out['b_chat.media_file_saved']=scalar(false);out['b_chat.intent_status']=scalar('not_inferred');
 return out;
}
