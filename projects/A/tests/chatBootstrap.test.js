'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {create,slot}=require('../extension/lib/chat_bootstrap.js');
const manifest=require('../extension/manifest.json');
function fixture({permission=true,state='empty',after,documentId='fixture-document'}={}){
 const calls=[],tabs=[{id:1,url:'https://www.douyin.com/chat'},{id:2,url:'https://www.douyin.com/chat-else'},{id:3,url:'https://anchor.douyin.com/chat'},{id:4,url:'https://www.douyin.com/user/fixture'}];
 let n=0;const chrome={runtime:{getManifest:()=>manifest},permissions:{contains:async()=>permission},tabs:{query:async q=>{calls.push({query:q});return tabs;}},
 scripting:{executeScript:async arg=>{calls.push(arg);n++;if(after)permission=await after(n,permission);
 return [{frameId:0,documentId,result:state}];}}};
 return {api:create(chrome),calls,chrome,tabs};
}
test('bootstrap: exact chat top document gets current scripts once; no navigation/read/send',async()=>{
 const f=fixture();await Promise.all([f.api.scan(),f.api.scan()]);
 const scripts=f.calls.filter(c=>c.world);assert.equal(scripts.length,3);
 assert.equal(scripts[0].func,slot);assert.deepEqual(scripts[0].target,{tabId:1,frameIds:[0]});
 assert.deepEqual(scripts[1].target,{tabId:1,documentIds:['fixture-document']});assert.equal(scripts[1].world,'MAIN');
 assert.equal(scripts[2].files.at(-1),'content.js');assert.ok(f.calls.every(c=>!c.url));
});
test('bootstrap: missing or revoked optional permission never navigates or injects past grant',async()=>{
 const f=fixture({permission:false});assert.deepEqual(await f.api.scan(),[]);assert.equal(f.calls.length,0);
 const g=fixture({after:async n=>n!==1});await g.api.scan();assert.equal(g.calls.filter(c=>c.world).length,1);
});
test('bootstrap: live/old sidebar or missing document ID cannot cause a second reader',async()=>{
 for(const options of [{state:'ready'},{state:'old_instance'},{documentId:undefined},{documentId:''}]){
  const f=fixture(options);if(options.documentId===undefined&&Object.hasOwn(options,'documentId'))continue;
  await f.api.scan();assert.equal(f.calls.filter(c=>c.files).length,0);
 }
});
test('bootstrap: MAIN reader entry is idempotent and does not request metadata during mount',()=>{
 const listeners=new Map(),doc={defaultView:{},addEventListener:(event,fn)=>{listeners.set(event,(listeners.get(event)||0)+1);},removeEventListener(){}};
 const context=vm.createContext({document:doc});const source=fs.readFileSync(require.resolve('../extension/identity/native_main_reader.js'),'utf8');
 vm.runInContext(source,context);vm.runInContext(source,context);
 assert.deepEqual([...listeners],[['dsh-native-read-v2',1],['dsh-native-metadata-read-v2',1]]);
 assert.ok(!listeners.has('dsh-native-read-v1'));
});
test('bootstrap: slot skips valid instance and will not mount over an old sidebar',async()=>{
 let reads=0;const context=vm.createContext({location:{origin:'https://www.douyin.com',pathname:'/chat'},window:{},
 document:{querySelector:()=>{reads++;return {};}},setTimeout:resolve=>resolve()});
 vm.runInContext('window.top=window;',context);
 assert.equal(await vm.runInContext('('+slot.toString()+')()',context),'old_instance');assert.equal(reads,100);
 vm.runInContext('globalThis.__DSH_CHAT_LIFECYCLE_V2__={alive:()=>true};',context);
 assert.equal(await vm.runInContext('('+slot.toString()+')()',context),'ready');assert.equal(reads,100);
});
