'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {NativeHost}=require('../extension/identity/native_host.js');

test('missing navigation permission gates metadata; a grant cannot retry a consumed context',async()=>{
 let permitted=false,resolves=0;const diagnostics=[],emitted=[];
 const id='a'.repeat(32),url='https://www.douyin.com/chat';
 const sender={id,url,frameId:0,documentId:'fictional-doc',tab:{id:1,url}};
 const context={context_id:'fictional-context',generation:1,head_token:'fictional-head',header_bound:true};
 const producer={enabled:true,onInvalidate(){},resolve:async()=>{resolves++;return null;},current:()=>false,release(){}};
 const host=new NativeHost({id,status:async()=>({format:'workbench-native-status-1',connection_epoch:0}),
  lookup:async()=>{throw Error('no lookup expected');},emit:(tab,doc,value)=>emitted.push(value),
  getFrame:async()=>{if(!permitted)throw Error('permission missing');return {documentId:sender.documentId,url};},
  savedProfileContext:producer,diagnostic:(entry,value)=>diagnostics.push(value)});
 await host.control('identity-pilot-context',context,sender);
 assert.equal(resolves,0);
 assert.ok(diagnostics.some(d=>d.reason==='frame-unavailable'));
 permitted=true;
 await host.control('identity-pilot-context',context,sender);
 assert.equal(resolves,0,'grant alone must not renew a consumed context');
 await host.control('identity-pilot-context',{...context,context_id:'fictional-new-context',generation:2,head_token:'fictional-new-head'},sender);
 assert.equal(resolves,1,'a separately established context can reach the metadata provider');
 assert.ok(emitted.every(e=>e.type!=='native-qualified-profile-view'));
});
