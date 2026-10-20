import test from 'node:test';import assert from 'node:assert/strict';
import {newChatTarget} from '../tools/chat_page_target.mjs';
const pages=a=>({structuredContent:{pages:a}});
test('only new page chosen even if previous chat remains selected',()=>{
 const before=pages([{id:1,url:'https://www.douyin.com/chat'}]);
 assert.equal(newChatTarget(before,pages([{id:1,url:'https://www.douyin.com/chat',selected:true},{id:2,url:'https://www.douyin.com/chat'}])),2);
});
test('text response no selected suffix, duplicates and redirects',()=>{
 const before={content:[{type:'text',text:'1: https://www.douyin.com/chat [selected]'}]};
 assert.equal(newChatTarget(before,{content:[{type:'text',text:'1: https://www.douyin.com/chat\n2: https://www.douyin.com/chat'}]}),2);
 assert.throws(()=>newChatTarget(before,before),/NOT_UNIQUE/);
 assert.throws(()=>newChatTarget(before,pages([{id:2,url:'https://www.douyin.com/passport'}])),/NOT_READY/);
});
