// Fixed normal web pages. No user-supplied URL, credentials, iframe or login automation.
export const SOURCE_PAGES=Object.freeze({
  anchor:'https://anchor.douyin.com/anchor/analyze/audience_rank?from=default',
  chat:'https://www.douyin.com/chat'
});
const matches=(url,kind)=>{try{const a=new URL(url),b=new URL(SOURCE_PAGES[kind]);return a.origin===b.origin&&a.pathname===b.pathname;}catch{return false;}};
export class SourcePages {
  constructor(api=chrome,{delay=ms=>new Promise(r=>setTimeout(r,ms)),notify=()=>{},attempts=100}={}){
    Object.assign(this,{api,delay,notify,attempts});this.ids=new Map();this.opening=new Map();
  }
  async findOrOpen(kind) {
    if(!Object.hasOwn(SOURCE_PAGES,kind))throw Error('SOURCE_KIND');
    // A help click and preparation may overlap; open at most one page per kind.
    if(this.opening.has(kind))return this.opening.get(kind);
    const work=(async()=>{
      let tab;
      if(this.ids.has(kind))try{const previous=await this.api.tabs.get(this.ids.get(kind));if(matches(previous.url,kind))tab=previous;}catch{}
      if(!tab){const all=await this.api.tabs.query({url:new URL(SOURCE_PAGES[kind]).origin+'/*'});tab=all.filter(t=>matches(t.url,kind)).sort((a,b)=>(b.lastAccessed||0)-(a.lastAccessed||0)||a.id-b.id)[0];}
      if(!tab)tab=await this.api.tabs.create({url:SOURCE_PAGES[kind],active:false});
      this.ids.set(kind,tab.id);return tab;
    })();
    this.opening.set(kind,work);
    try{return await work;}finally{this.opening.delete(kind);}
  }
  async show(kind){const tab=await this.findOrOpen(kind);await this.api.tabs.update(tab.id,{active:true});}
  async prepare(domains,check=()=>{}) {
    const kinds=[];if(domains.some(x=>!['chat','legacy_chat','account','capability'].includes(x)))kinds.push('anchor');
    if(domains.includes('chat'))kinds.push('chat');
    const result={anchorTab:null,chatTab:null};
    for(const kind of kinds){
      check();this.notify({kind,state:'opening'});let tab=await this.findOrOpen(kind);check();
      let ready=false;
      for(let i=0;i<this.attempts;i++){
        check();tab=await this.api.tabs.get(tab.id);check();
        if(tab.status==='complete'){
          if(!matches(tab.url,kind)){this.notify({kind,state:'login'});throw Error(kind==='anchor'?'BACKEND_LOGIN_REQUIRED':'CHAT_LOGIN_REQUIRED');}
          ready=true;break;
        }
        await this.delay(250);
      }
      check();if(!ready){this.notify({kind,state:'loading'});throw Error('SOURCE_PAGE_LOADING');}
      result[kind==='anchor'?'anchorTab':'chatTab']=tab.id;this.notify({kind,state:'ready'});
    }
    return result;
  }
}
