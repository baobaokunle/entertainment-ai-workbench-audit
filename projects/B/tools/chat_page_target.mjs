export function pageList(result){
 const structured=result?.structuredContent?.pages;
 if(Array.isArray(structured))return structured.filter(p=>Number.isSafeInteger(p.id)&&typeof p.url==='string').map(p=>({id:p.id,url:p.url}));
 const text=(result?.content||[]).filter(c=>c.type==='text').map(c=>c.text).join('\n');
 return [...text.matchAll(/(?:^|\n)(\d+):\s+(\S+)/g)].map(m=>({id:Number(m[1]),url:m[2]}));
}
export function newChatTarget(before,after){
 const ids=new Set(pageList(before).map(p=>p.id)),added=pageList(after).filter(p=>!ids.has(p.id));
 if(added.length!==1)throw Error('NEW_CHAT_PAGE_NOT_UNIQUE');
 let u;try{u=new URL(added[0].url);}catch{throw Error('NEW_CHAT_PAGE_NOT_READY');}
 if(u.origin!=='https://www.douyin.com'||u.pathname!=='/chat')throw Error('NEW_CHAT_PAGE_NOT_READY');
 return added[0].id;
}
