(function(){
'use strict';
const statusNode = document.getElementById('status');
const loginForm = document.getElementById('login');
const workbenchConnectButton=document.getElementById('workbench-connect');
let connectionTarget='identity_pilot';
if(typeof chrome==='undefined'||!chrome.runtime||!(/^[a-p]{32}$/).test(chrome.runtime.id)||
  typeof chrome.runtime.sendMessage!=='function'||location.protocol!=='chrome-extension:'||location.hostname!==chrome.runtime.id){
  statusNode.textContent='请从Chrome工具栏的F盘扩展图标打开此窗口。';
  document.getElementById('connection-status').textContent='此页面尚未运行在Chrome扩展环境中。';
  document.getElementById('extension-identity').textContent='扩展初始化未完成。';
  document.getElementById('pilot-status').textContent='身份和私聊范围仍未知。';
  loginForm.hidden=true;document.getElementById('session').hidden=true;
  document.getElementById('pilot-navigation').hidden=true;
  document.getElementById('forget-device').hidden=true;
  return;
}
document.getElementById('extension-identity').textContent='F盘开发扩展 · '+chrome.runtime.id;
async function call(value) {
  const result = await chrome.runtime.sendMessage(value);
  if (!result || !result.ok) throw new Error((result && result.error) || 'unavailable');
  return result.data;
}
const errorText = {unauthorized:'本机会话尚未建立或已过期，配对记忆保持不变。',forbidden:'本机登记未授予此权限。',already_authenticated:'当前已有连接，请先断开。',outbox_full:'待提交队列已满，请保全队列并检查本机服务。',outbox_corrupt:'待提交队列无法读取，请保全原文件并检查。',stale_session:'连接已切换，请重新打开此窗口。'};
async function refreshNavigation() {
  const button=document.getElementById('pilot-navigation'),notice=document.getElementById('navigation-status');
  // The normal workbench also checks the live frame/document. Pairing does
  // not grant this optional browser permission, and opening the popup must
  // never request it automatically.
  button.hidden=false;
  notice.textContent='正在核对当前聊天所需的导航权限…';
  try{
    const granted=await chrome.permissions.contains({permissions:['webNavigation']});
    button.hidden=granted===true;
    notice.textContent=granted===true?'导航权限已具备；当前聊天身份仍需单独核验。':
      '缺少导航核对权限，当前聊天身份匹配暂不可用。本机配对不受影响。';
  }catch{notice.textContent='导航权限状态暂不可读取；当前聊天身份仍待核验。';}
}
const mountPermission={permissions:['scripting'],origins:['https://www.douyin.com/*']};
async function refreshMountPermission(){
  const button=document.getElementById('enable-chat-mount'),notice=document.getElementById('chat-mount-status');
  if(!button||!notice)return;
  try{
    const granted=await chrome.permissions.contains(mountPermission);
    button.hidden=granted===true;
    notice.textContent=granted?'已开启：启用扩展后会给已打开的聊天页加载侧栏。':'可一次授权“启用即显示”，以后无需刷新聊天或打开扩展弹窗。';
  }catch{notice.textContent='页面加载权限暂不可读取；原配对保持不变。';}
}
document.getElementById('enable-chat-mount')?.addEventListener('click',async()=>{
  // Called directly from this gesture, never from startup/status recovery.
  try{await chrome.permissions.request(mountPermission);}catch{}
  await refreshMountPermission();
});
async function refresh() {
  const data=await call({type:'status'});
  await refreshMountPermission();
  const workbench=data.session_kind==='workbench'||connectionTarget==='workbench';
  if(workbench){
    connectionTarget='workbench';
    loginForm.hidden=true;document.getElementById('forget-device').hidden=true;
    document.getElementById('queue').hidden=true;
    // Cached identity is not current authorization; keep it hidden while checking.
    document.getElementById('session').hidden=true;
    await refreshNavigation();
    document.getElementById('pilot-status').textContent='当前聊天身份、账号归属和私聊范围仍待核验。';
    if(!data.authenticated){
      statusNode.textContent=data.workbench_auto_connect==='paused'?'自动连接已暂停；点“恢复自动连接”即可继续。':'正在等待本机工作台自动连接。';
      document.getElementById('connection-status').textContent='工作台连接尚未建立，资料保持未知。';
      if(workbenchConnectButton)workbenchConnectButton.hidden=true;
      if(data.workbench_pairing_supported===true){
        try{
          const info=await call({type:'workbench-pairing-info'});
          loginForm.hidden=info.pairing_remembered===true||info.code_available!==true;
          document.getElementById('forget-device').hidden=info.pairing_remembered!==true;
          if(workbenchConnectButton){
            workbenchConnectButton.hidden=info.pairing_remembered!==true||data.workbench_auto_connect==='automatic';
            workbenchConnectButton.textContent='恢复自动连接';
          }
          statusNode.textContent=info.pairing_remembered?(data.workbench_auto_connect==='automatic'?'配对已记住，正在自动恢复；本机服务恢复后无需再次点击。':data.workbench_auto_connect==='paused'?'自动连接已暂停；点“恢复自动连接”即可继续。':'工作台配对已记住，可连接本机，无需记密码。'):
            info.code_available?'首次用本机窗口的临时码连接工作台，无需记密码。':'本次临时码已不可用，等待本机工作台启动。';
        }catch{statusNode.textContent='工作台服务或配对记忆暂不可用，已保留原记忆；资料仍未知。';}
      }
      return;
    }
    try{
      const capabilities=await call({type:'capabilities'}),p=capabilities&&capabilities.principal,expected=data.principal;
      if(!p||p.operator_key!==expected.operator_key||p.device_key!==expected.device_key||p.role!==expected.role||
         !Array.isArray(p.account_keys)||p.account_keys.length!==expected.account_keys.length||
         !p.account_keys.every(key=>expected.account_keys.includes(key)))throw Error('bad_response');
      document.getElementById('identity').textContent=`${p.operator_key} · ${p.role} · ${p.device_key}`;
      document.getElementById('session').hidden=false;
      statusNode.textContent='本机工作台授权已核对；当前平台身份仍待核验。';
      document.getElementById('connection-status').textContent='本机工作台已连接。日常启用扩展即可，无需重复连接。';
      if(workbenchConnectButton)workbenchConnectButton.hidden=true;
    }catch{
      document.getElementById('session').hidden=true;
      statusNode.textContent='工作台会话或当前权限未能核实，资料保持未知。';
      document.getElementById('connection-status').textContent='工作台连接待核对；不会自动恢复身份试点。';
    }
    return;
  }
  document.getElementById('forget-device').hidden=false;document.getElementById('queue').hidden=false;
  const connection=await call({type:'identity-pilot-connection'});
  document.getElementById('connection-status').textContent=connection.state==='connected'?'本机只读身份服务已连接。':connection.state==='different_service'?'此端口运行的是其他服务，身份试点未接通。':'本机身份服务未连接。';
  const remembered=data.pairing_remembered===true;
  const memoryUnavailable=data.pairing_memory_available===false;
  loginForm.hidden = data.authenticated||remembered||memoryUnavailable; document.getElementById('session').hidden = !data.authenticated;
  statusNode.textContent = data.authenticated ? '本机认证已建立，无需记密码；真实页面来源仍待核验。' : memoryUnavailable ?
    '本机配对记忆暂不可读取，等待工程检查。' : remembered ?
    (connection.state==='connected'?'本机配对已记住，身份服务连接尚未完成。':'本机配对已记住，等待本机身份服务启动。') :
    '尚未连接。无需设置密码，首次使用临时码完成本机配对。';
  if (data.authenticated) document.getElementById('identity').textContent = `${data.principal.operator_key} · ${data.principal.role} · ${data.principal.device_key}`;
  const q = data.queue; document.getElementById('queue').textContent = `待提交 ${q.waiting} · 待核验 ${q.pending} · 拒绝 ${q.rejected} · 其他会话保留 ${q.other_session}`;
  await refreshNavigation();
  const pilot=document.getElementById('pilot-status');
  if(!data.authenticated)pilot.textContent=memoryUnavailable?'本机配对记忆暂不可读取；资料和私聊范围仍未知。':remembered?'本机配对已记住；身份服务连接尚未建立，资料和私聊范围仍未知。':'身份核对需先完成本机配对；资料和私聊范围仍未知。';
  else{try{await call({type:'identity-pilot-info'});pilot.textContent='只读身份服务已连接；当前聊天来源仍需现场核实。';}catch{pilot.textContent='本机认证存在，身份核对服务尚未接通。';}}
}
async function run(task) { try { await task(); await refresh(); } catch (e) { statusNode.textContent = errorText[e.message] || '本机服务或存储暂不可用，请检查后重试。'; } }
loginForm.addEventListener('submit',event => { event.preventDefault(); const form = new FormData(loginForm); const code = String(form.get('pairing_code')); loginForm.elements.pairing_code.value = ''; run(async() => {
  if(connectionTarget==='workbench')return call({type:'workbench-pair',code});
  if((await call({type:'identity-pilot-connection'})).state!=='connected')throw Error('unavailable');
  return call({type:'identity-pilot-pair',code});
}); });
document.getElementById('logout').addEventListener('click',() => run(() => call({type:'logout'})));
document.getElementById('forget-device').addEventListener('click',() => run(() => call({type:connectionTarget==='workbench'?'workbench-forget':'identity-pilot-forget'})));
if(workbenchConnectButton)workbenchConnectButton.addEventListener('click',()=>run(()=>call({type:'workbench-connect'})));
document.getElementById('retry').hidden=true;
document.getElementById('pilot-navigation').addEventListener('click',async()=>{
  try{const ok=await chrome.permissions.request({permissions:['webNavigation']});document.getElementById('pilot-status').textContent=ok?'导航权限已允许；来源仍需核验。':'尚未允许导航核对。';await refresh();}
  catch{document.getElementById('pilot-status').textContent='导航权限未取得。';}
});
run(async() => {
  const initial=await call({type:'status'});
  if(initial.session_kind==='workbench'){connectionTarget='workbench';return;}
  if(initial.session_kind==='unknown')throw Error('forbidden');
  if(!initial.authenticated&&initial.workbench_pairing_supported===true){
    try{
      const info=await call({type:'workbench-pairing-info'});
      if(info.available===true){
        connectionTarget='workbench';
        if(info.pairing_remembered===true&&initial.workbench_auto_connect===undefined)await call({type:'workbench-connect'});
        return;
      }
    }catch(error){
      // A broken normal memory is not first use and cannot trigger pilot fallback.
      if(error.message==='storage_unavailable'){connectionTarget='workbench';throw error;}
    }
    if(connectionTarget==='workbench')return;
  }
  try{await call({type:'identity-pilot-connect'});}catch{/* Still show connection diagnostics; never guess authentication. */}
});
})();
