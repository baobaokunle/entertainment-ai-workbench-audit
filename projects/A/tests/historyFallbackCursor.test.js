'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {HistoryController}=require('../web/js/history.js');
test('history rejects malformed scoped fallback cursor responses instead of forwarding another scope shape',async()=>{
  const valid={account_key:'fictional-account',message_key:'fictional-message',scope:'a'.repeat(64)};
  for(const cursor of [{...valid,scope:null},{...valid,scope:123},{...valid,scope:'bad'},
    {...valid,scope:'a'.repeat(65)},{...valid,account_key:'another-account'},{...valid,extra:true}]){
    let calls=0;const controller=new HistoryController(async()=>{calls++;return {items:[],next_cursor:cursor};},()=>{});
    await controller.select({account_key:'fictional-account',person_key:'fictional-person',day:'2026-10-14'});
    assert.equal(controller.state.state,'unavailable');assert.equal(controller.state.next_cursor,null);
    await controller.load(true);assert.equal(calls,1);
  }
});
