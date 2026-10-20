(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DSHChatBootstrap=api;})(globalThis,function(){
  'use strict';
  const PERMISSION={permissions:['scripting'],origins:['https://www.douyin.com/*']};
  function chatURL(value){try{const u=new URL(value);return u.origin==='https://www.douyin.com'&&u.pathname==='/chat'&&!u.username&&!u.password;}catch{return false;}}
  // Isolated extension world: wait for invalidated old instances to clean up.
  // Never remove another instance or its layout by force. No identity fields.
  async function slot(){
    if(location.origin!=='https://www.douyin.com'||location.pathname!=='/chat'||window.top!==window)return 'other';
    if(globalThis.__DSH_CHAT_LIFECYCLE_V2__?.alive())return 'ready';
    for(let count=0;count<100;count++){
      if(!document.querySelector('aside[data-dsh-owned="true"]'))return 'empty';
      await new Promise(resolve=>setTimeout(resolve,250));
      if(location.pathname!=='/chat')return 'other';
      if(globalThis.__DSH_CHAT_LIFECYCLE_V2__?.alive())return 'ready';
    }
    return 'old_instance';
  }
  function create(chrome){
    const pending=new Map();let scanning=null;
    async function allowed(){return !!chrome.scripting&&await chrome.permissions.contains(PERMISSION)===true;}
    function mount(tab){
      if(!tab||!Number.isInteger(tab.id)||tab.id<0||!chatURL(tab.url))return Promise.resolve('other');
      if(pending.has(tab.id))return pending.get(tab.id);
      const task=(async()=>{
        if(!await allowed())return 'permission_required';
        const result=await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},world:'ISOLATED',func:slot});
        if(result.length!==1||result[0].frameId!==0||result[0].result!=='empty')return result[0]?.result||'unknown';
        const documentId=result[0].documentId;
        if(typeof documentId!=='string'||!documentId)return 'unknown';
        // Pin both worlds to this document. A navigation between calls cannot
        // inject into the next route or turn a load receipt into identity.
        if(!await allowed())return 'permission_required';
        const scripts=chrome.runtime.getManifest().content_scripts;
        const main=scripts.find(s=>s.world==='MAIN'),isolated=scripts.find(s=>!s.world||s.world==='ISOLATED');
        if(!main||!isolated)throw Error('invalid_manifest');
        await chrome.scripting.executeScript({target:{tabId:tab.id,documentIds:[documentId]},world:'MAIN',files:main.js});
        if(!await allowed())return 'permission_required';
        await chrome.scripting.executeScript({target:{tabId:tab.id,documentIds:[documentId]},world:'ISOLATED',files:isolated.js});
        return 'mounted';
      })();
      const shared=task.finally(()=>pending.delete(tab.id));pending.set(tab.id,shared);return shared;
    }
    function scan(){
      if(scanning)return scanning;
      const task=(async()=>{
        if(!await allowed())return [];
        const tabs=await chrome.tabs.query({url:['https://www.douyin.com/chat*']});
        return Promise.all(tabs.filter(tab=>chatURL(tab.url)).map(tab=>mount(tab).catch(()=> 'unavailable')));
      })();
      const shared=task.finally(()=>{if(scanning===shared)scanning=null;});scanning=shared;return shared;
    }
    return {mount,scan};
  }
  return {create,PERMISSION,slot};
});
