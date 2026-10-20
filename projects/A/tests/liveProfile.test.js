'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),Live=require('../web/js/live.js');
const binding={account_key:'fictional-account',person_key:'fictional-person'};
const metric={state:'known',value:'0',unit:'points',precision:'exact',coverage:'complete'};
const data={format:'v1-live-profile-1',...binding,state:'observed',scope:'saved_contact_observed_sessions',complete:false,tier_from_session_metrics:false,sessions:[{basis:'single_session',started_lower_utc:'2026-10-10T12:00:00Z',ended_upper_utc:'2026-10-10T13:00:00Z',captured_at_utc:'2026-10-10T14:00:00Z',source_id:'fictional-source',source_version:'1',metrics:{contribution:metric,watch_seconds:{...metric,value:'600',unit:'seconds'},comment_count:{...metric,value:'2',unit:'count'},pk_contribution:metric,attendance:{...metric,value:'1',unit:'boolean'}}}]};
test('live source zero stays known, missing stays unknown, original units and single-session scope remain visible',()=>{
 const result=Live.project(data,binding);assert.equal(result.state,'observed');const text=JSON.stringify(result);assert.ok(text.includes('0 points'));assert.ok(text.includes('600 seconds'));assert.ok(text.includes('单场数据不代替30日榜'));assert.ok(text.includes('不能以出席或观看替代'));
 for(const change of [{state:'unknown'},{value:null},{value:true},{value:''},{value:' '},{value:'0x10'},{value:'-1'},{value:'NaN'},{value:'1e31'},{precision:'unknown'}])assert.equal(Live.metricText({...metric,...change}),'未知');
});
test('different account person and unverified identity do not carry old live data',()=>{
 for(const change of [{account_key:'other'},{person_key:'other'},{format:'other'},{complete:true},{tier_from_session_metrics:true},{state:'identity_unverified'}])assert.equal(Live.project({...data,...change},binding).state,'unknown');
});
test('live rendering clears prior data without HTML injection or any control',()=>{
 const nodes=[];const create=tag=>{const n={tag,textContent:'',children:[],append(...items){this.children.push(...items);},replaceChildren(){this.children=[];}};nodes.push(n);return n;};const target=create('div');target.ownerDocument={createElement:create};
 Live.render(target,data,binding);assert.equal(target.children.length,2);Live.render(target,null,binding);assert.equal(target.children.length,1);assert.ok(target.children[0].textContent.includes('尚无'));assert.ok(!nodes.some(n=>['input','form','button'].includes(n.tag)));
});

test('direct saved likes show source integer zero and display precision only within their session',()=>{
 for(const [value,precision,coverage]of [['0','exact','complete'],['12000','display','partial']]){
  const copy=structuredClone(data);copy.sessions[0].metrics.like_count={...metric,value,unit:'count',precision,coverage};
  const view=Live.project(copy,binding),line=view.sessions[0].lines.find(row=>row[0]==='点赞')[1];
  assert.ok(line.startsWith(value+' count'));assert.ok(line.includes(precision==='display'?'显示精度':'精确值'));
  assert.ok(line.includes(coverage==='complete'?'覆盖完整':'覆盖不完整'));
  assert.ok(view.sessions[0].lines.find(row=>row[0]==='陪伴')[1].startsWith('未知'));assert.ok(view.note.includes('不推算累计值'));
 }
});

test('unsupported likes and foreign binding never inherit a saved count',()=>{
 for(const invalid of [{value:'1.5'},{value:'1e3'},{value:'01'},{value:'+1'},{value:'-1'},{value:'9'.repeat(31)},{value:'1'+'0'.repeat(29)+'1'},{unit:'seconds'},{state:'unknown',value:null,precision:'unknown'}]){
  const copy=structuredClone(data);copy.sessions[0].metrics.like_count={...metric,unit:'count',...invalid};
  assert.ok(Live.project(copy,binding).sessions[0].lines.find(row=>row[0]==='点赞')[1].startsWith('未知'));
 }
 const copy=structuredClone(data);copy.sessions[0].metrics.like_count={...metric,unit:'count',value:'42'};
 assert.equal(Live.project(copy,{...binding,person_key:'other'}).state,'unknown');
 assert.equal(Live.project(copy,{...binding,account_key:'other'}).state,'unknown');
});
