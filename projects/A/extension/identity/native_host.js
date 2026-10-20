(function(root,factory){
  const node=typeof module==='object'&&module.exports;
  const v=factory(node?require('./pilot.js'):root.DSHIdentityPilot,node?require('./profile_navigation.js'):root.DSHProfileNavigation,node?require('../lib/protocol.js'):root.DSHProtocol,node?require('./selection_budget.js'):root.DSHSelectionBudget,node?require('./history_summary.js'):root.DSHHistorySummary);
  if(node)module.exports=v;else root.DSHNativeHost=v;
})(globalThis,function(I,N,P,B,H){
  'use strict';
  const contextKeys=['context_id','generation','head_token','header_bound'];
  const field=(object,key)=>{const d=object&&Object.getOwnPropertyDescriptor(object,key);return d&&Object.hasOwn(d,'value')?d.value:undefined;};
  function pick(object,keys){
    const result={};for(const key of keys){const value=field(object,key);if(value===null||typeof value==='boolean'||typeof value==='number'&&Number.isFinite(value)||typeof value==='string'&&value.length<=256)result[key]=value;}return result;
  }
  function finiteItems(value,max,predicate){
    if(!Array.isArray(value)||value.length>max)return [];
    const result=[];for(let i=0;i<value.length;i++){const item=field(value,String(i));if(predicate(item))result.push(item);}return result;
  }
  function tierExplanation(value){
    const clean=pick(value,['format','rule_version','final_grade','combination','reason']);
    clean.original_qualifications=pick(field(value,'original_qualifications'),['S','A','B','C','D']);
    const missing=field(value,'missing_inputs');
    clean.missing_inputs=Array.isArray(missing)&&missing.length<=4?missing.filter(x=>['contribution','comment','watch','clip'].includes(x)):[];
    return clean;
  }
  function rankRows(ranks){
    const rows=[];
    if(Array.isArray(ranks)&&ranks.length<=3)for(let n=0;n<ranks.length;n++){
      const rank=field(ranks,String(n));
      rows.push({...pick(rank,['kind','position','period_start_utc','period_end_utc','tier_period_eligible','period_key','period_label','effective_cutoff','revision_id','coverage','outside_top100_verified','period_basis','rank_as_of_day','tier_day_eligible']),
        adoption:pick(field(rank,'adoption'),['format','time_basis','statistics_through_day','package_sha256','expires_at']),
        metric:pick(field(rank,'metric'),['state','value','unit','precision','raw_display'])});
    }
    return rows;
  }
  function profileFields(profile){
    const fields=field(profile,'fields'),clean={};
    for(const key of ['formal_tag.clip','formal_tag.wechat','formal_tag.maintenance','formal_tag.spark','formal_tag.vip','formal_tag.star','formal_tag.local_time','location']){
      const record=field(fields,key),value=field(record,'effective_value');
      clean[key]={...pick(record,['state']),effective_value:key==='location'?pick(value,['country','city','timezone']):typeof value==='boolean'?value:null};
    }
    return {fields:clean};
  }
  function recentSummary(recent){
    const clean=pick(recent,['state','reason','effective_collection_day','captured_at_utc','window_basis','support_unit']);
    for(const name of ['current','previous']){
      const value=field(recent,name),window=field(value,'window');
      clean[name]=pick(value,['broadcast_days','support','watch_seconds','comments','support_days','arrival_days','comment_days','arrival_ratio']);
      clean[name].complete=pick(field(value,'complete'),['support','arrival','comment']);
      clean[name].window=Array.isArray(window)&&window.length===2?[field(window,'0'),field(window,'1')].map(x=>typeof x==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(x)?x:null):[];
    }
    const decline=field(recent,'declines'),directions=field(decline,'directions');clean.declines={...pick(decline,['observation','proactive_suggestion']),directions:finiteItems(directions,3,x=>['support','arrival','comment'].includes(x))};
    return clean;
  }
  function dailyDetails(card){
    const dates=field(card,'dates'),terms=field(dates,'terms'),keys=['status','days','remind','next','basis'];
    const clean={dates:{...pick(dates,['as_of_day','calculated_at_utc']),birthday:pick(field(dates,'birthday'),keys),anniversary:pick(field(dates,'anniversary'),keys),terms:{},local_time:pick(field(dates,'local_time'),['time','timezone'])}};
    for(const kind of ['vip','star']){const rows=field(terms,kind);clean.dates.terms[kind]=[];if(Array.isArray(rows)&&rows.length<=500)for(let i=0;i<rows.length;i++)clean.dates.terms[kind].push(pick(field(rows,String(i)),keys));}
    const relations=field(card,'relations'),changes=field(relations,'changes');clean.relations={...pick(relations,['mutual']),directions:pick(field(relations,'directions'),['fan_follows_me','i_follow_fan']),changes:[]};
    if(Array.isArray(changes)&&changes.length<=20)for(let i=0;i<changes.length;i++)clean.relations.changes.push(pick(field(changes,String(i)),['direction','actual_at_utc','discovered_at_utc']));
    const slots=field(card,'live_slots'),sources=field(slots,'sources');clean.live_slots={sources:{},...pick(slots,['live_preferred'])};
    for(const name of ['entry','interaction']){const row=field(sources,name),selected=field(row,'selected');clean.live_slots.sources[name]={...pick(row,['days','threshold','dispersed']),selected:finiteItems(selected,2,x=>Number.isInteger(x)&&x>=0&&x<12)};}
    const opportunity=field(card,'opportunity'),reasons=field(opportunity,'reasons');clean.opportunity={...pick(opportunity,['status']),reasons:finiteItems(reasons,3,x=>['watch','comment','pk'].includes(x))};
    return clean;
  }
  function savedCard(result,binding,fixture=true){
    const card=field(result,'card'),saved=field(result,'binding');
    if(field(result,'format')!=='native-profile-lookup-1'||field(result,'state')!=='saved_binding_ready'||
       field(result,'authority_scope')!=='current_independent_approval'||field(result,'native_page_verification')!=='background_required'||
       field(result,'historical_device_provenance')!=='not_assessed'||field(result,'message_history')!=='not_loaded'||field(result,'AI_reference')!=='not_requested'||
       Object.keys(binding).some(key=>field(saved,key)!==binding[key])||field(card,'format')!=='native-profile-card-1'||
       field(card,'account_key')!==binding.account_key||field(card,'person_key')!==binding.person_key||field(card,'object_kind')!=='person'||field(card,'verified')!==true)return null;
    const clean=pick(card,['format','account_key','person_key','object_kind','verified','display_name','grade']);
    clean.tier_explanation=tierExplanation(field(card,'tier_explanation'));
    clean.tier_freshness=pick(field(card,'tier_freshness'),['format','required_day','state']);
    clean.profile=profileFields(field(card,'profile'));
    clean.recent=recentSummary(field(card,'recent'));
    Object.assign(clean,dailyDetails(card));
    const reference=field(card,'saved_tier_reference');
    if(field(reference,'scope')==='saved_snapshot')clean.saved_tier_reference={scope:'saved_snapshot',explanation:tierExplanation(field(reference,'explanation')),rankings:rankRows(field(reference,'rankings'))};
    const live=field(card,'live'),sessions=field(live,'sessions');
    clean.live=pick(live,['format','account_key','person_key','state','scope','complete','tier_from_session_metrics']);
    clean.live.sessions=[];
    // One observed session with its dates; never forward free text, notes,
    // raw observations, message bodies or surplus rows.
    if(Array.isArray(sessions)&&sessions.length>0&&sessions.length<=20){
      const first=field(sessions,'0'),metrics=field(first,'metrics'),item=pick(first,['basis','started_lower_utc','started_upper_utc','ended_lower_utc','ended_upper_utc','captured_at_utc','revision_id']);item.metrics={};
      for(const key of ['contribution','comment_count','watch_seconds','like_count'])item.metrics[key]=pick(field(metrics,key),['state','value','unit','precision','coverage','raw_display']);
      clean.live.sessions.push(item);
    }
    clean.rankings=rankRows(field(card,'rankings'));
    return {...pick(result,['format','state','authority_scope','native_page_verification','historical_device_provenance','message_history','AI_reference']),binding:{...binding},card:clean,fixture};
  }
  function exportStatement(config){
    const field=config&&Object.getOwnPropertyDescriptor(config,'historical_export_source_statement');
    return field&&Object.hasOwn(field,'value')&&field.value==='user_confirmed'?'user_confirmed':'unknown';
  }
  function valid(value,arm=false,recall=false){
    const keys=arm?[...contextKeys,'profile_url']:recall?[...contextKeys,'sec_uid']:contextKeys;
    if(!value||Object.getPrototypeOf(value)!==Object.prototype||Object.keys(value).length!==keys.length||Object.keys(value).some(k=>!keys.includes(k)))throw Error('invalid_request');
    if(!/^[A-Za-z0-9_-]{1,128}$/.test(value.context_id)||!Number.isSafeInteger(value.generation)||value.generation<0||typeof value.header_bound!=='boolean'||!/^[A-Za-z0-9_-]{1,128}$/.test(value.head_token))throw Error('invalid_request');
    if(arm)N.parseProfileURL(value.profile_url);
    if(recall&&(typeof value.sec_uid!=='string'||!/^[A-Za-z0-9_-]{1,256}$/.test(value.sec_uid)||value.sec_uid==='self'))throw Error('invalid_request');
  }
  class NativeHost {
    constructor({id,status,lookup,emit,getFrame,readPageMetadata=null,readConnectionEpoch=null,selectionBudget=null,savedProfileFixture=null,savedProfileContext=null,clock=()=>Date.now(),diagnostic=()=>{}}){
      if(readPageMetadata!==null&&typeof readPageMetadata!=='function'||readConnectionEpoch!==null&&typeof readConnectionEpoch!=='function')throw Error('metadata_configuration');
      this.readPageMetadata=readPageMetadata;this.readConnectionEpoch=readConnectionEpoch;
      Object.assign(this,{id,status,lookup,emit,getFrame,clock,diagnostic,selectionBudget});this.entries=new Map();this.targets=new Map();this.commits=new Map();this.memories=new Map();
      // Explicit test injection only. Production background supplies no producer.
      // Neither content metadata nor a pilot candidate can provide this binding.
      this.savedProfileFixture=savedProfileFixture;
      if(savedProfileFixture&&savedProfileContext)throw Error('conflicting_profile_providers');
      this.savedProfileContext=savedProfileContext;
      if(savedProfileContext)savedProfileContext.onInvalidate(lease=>{
        // Keep the consumed entry: deleting it would let an idle heartbeat
        // recreate the same context and repeat its lookup after revocation.
        for(const e of this.entries.values())if(e.savedQualified&&(!lease||e.savedLease===lease))this.clearSaved(e);
      });
    }
    diagnose(e,phase,reason,passed=false){if(!e||this.entries.get(e.tab)!==e)return;try{this.diagnostic(e,{phase,reason,passed});}catch{}}
    coordinatorFailure(e,stage){
      const status=e.pilot.coordinatorStatus();
      const reasons=['clock_invalid','scope_invalid','reentrant_callback','expired','scope_changed','approval_required','concurrent_attempt','event_invalid','event_unrelated','multiple_targets','route_or_event_invalid','profile_redirect_conflict','replacement_invalid','replacement_unrelated','cancelled','invalidated'];
      this.diagnose(e,'coordinator-'+stage,reasons.includes(status.reason)?'coordinator-'+status.reason.replaceAll('_','-'):'coordinator');
    }
    traceCommit(t,d,origin,stage,sequence){
      const e=t.entry;if(this.entries.get(e.tab)!==e)return;
      const key=stage==='entry'?'traceReceived':'traceProcessed',last=t[key];
      const order=last===undefined||d.timeStamp>last?'newer':d.timeStamp===last?'same':'older';
      t[key]=d.timeStamp;
      const url=d.url==='about:blank'?'blank':this.profileMatches(d.url,e.expected)?'expected':'other';
      const document=!t.document?'fresh':t.document===d.documentId?'same':'replaced';
      const state=e.pilot.coordinatorStatus().state;
      const types=['link','typed','auto_bookmark','auto_subframe','manual_subframe','generated','auto_toplevel','form_submit','reload','keyword','keyword_generated'];
      const transition=types.includes(d.transitionType)?d.transitionType:'unknown';
      const qualifiers=['client_redirect','server_redirect','forward_back','from_address_bar'];
      // Fixed four bits in the order above. Missing or unrecognized metadata
      // remains unknown; it cannot attest a redirect or relax navigation gates.
      const q=d.transitionQualifiers;
      const mask=Array.isArray(q)&&q.every(item=>qualifiers.includes(item))?qualifiers.map(item=>q.includes(item)?'1':'0').join(''):'unknown';
      this.diagnose(e,'commit-trace',[sequence,origin,stage,url,document,order,state,transition,mask].join(':'),false);
    }
    sender(s){P.senderKind(s,this.id);if(s.frameId!==0||typeof s.documentId!=='string'||!s.documentId)throw Error('invalid_request');}
    clearSaved(e){
      e.savedSerial=(e.savedSerial||0)+1;
      e.savedDeadline=0;
      if(e.savedLease&&this.savedProfileContext){this.savedProfileContext.release(e.savedLease);e.savedLease=null;}
      if(e.savedAttempted)this.emit(e.tab,e.document,{type:'native-saved-profile-clear',context_id:e.context.context_id,generation:e.context.generation,head_token:e.context.head_token});
    }
    clear(e,preserveMemory=false){this.clearSaved(e);if(e.workbench)this.view(e,{state:'pending',historical_identity:'not_checked'});e.selectionSerial=(e.selectionSerial||0)+1;e.recallSerial=(e.recallSerial||0)+1;if(!preserveMemory)this.memories.delete(e.tab);if(e.pilot)e.pilot.invalidate();e.proof=null;e.expected=null;for(const [id,t]of this.targets)if(t.entry===e)this.targets.delete(id);}
    // Background-only explicit capability, never called by context/heartbeat
    // registration. A scoped page response remains unverified metadata.
    async readCurrentMetadata(value,hostCurrent){
      if(!this.readPageMetadata||!this.readConnectionEpoch||typeof hostCurrent!=='function')return null;
      let e,operation;
      try{
        const scope=P.nativeMetadataScope(value,true);e=this.entries.get(scope.tab_id);
        if(!e||!e.workbench||e.document!==scope.document_id||contextKeys.some(k=>e.context[k]!==scope[k]))return null;
        if(e.metadataPending){e.metadataPending.cancelled=true;return null;}
        operation={cancelled:false};e.metadataPending=operation;
        const serial=e.savedSerial||0,epoch=e.workbenchEpoch,started=this.clock();
        const stable=()=>{
          // Status alone cannot fence a connection change during getFrame's
          // await. Read the existing worker's local epoch synchronously too.
          const currentEpoch=this.readConnectionEpoch(),now=this.clock();
          return !operation.cancelled&&e.metadataPending===operation&&this.entries.get(e.tab)===e&&!e.revoked&&
            (e.savedSerial||0)===serial&&e.workbenchEpoch===epoch&&currentEpoch===epoch&&Number.isSafeInteger(epoch)&&epoch>=0&&
            e.document===scope.document_id&&contextKeys.every(k=>e.context[k]===scope[k])&&
            Number.isFinite(started)&&Number.isFinite(now)&&now>=started&&now<started+30000;
        };
        const current=()=>stable()&&hostCurrent()===true&&stable();
        const live=async()=>{
          if(!current())return false;
          const status=await this.status();
          if(!current()||field(status,'format')!=='workbench-native-status-1'||field(status,'connection_epoch')!==epoch||Object.keys(status).length!==2)return false;
          return await this.live(e)&&current();
        };
        if(!await live())return null;
        const request=P.nativeMetadataRequest({type:'native-metadata-read',scope:Object.fromEntries(contextKeys.map(k=>[k,scope[k]]))});
        const raw=await this.readPageMetadata(scope.tab_id,scope.document_id,request);
        if(!current())return null;
        const response=P.nativeMetadataResponse(raw);
        if(contextKeys.some(k=>response.scope[k]!==scope[k])||!await live())return null;
        // tab/document originate from the captured host entry, not page data.
        return Object.freeze({scope,metadata:response.metadata});
      }catch{return null;}
      finally{if(e&&operation&&e.metadataPending===operation)delete e.metadataPending;}
    }
    async savedProfile(e,producer=this.savedProfileFixture,qualified=false){
      e.savedAttempted=true;
      const serial=e.savedSerial=(e.savedSerial||0)+1,started=this.clock(),deadline=started+30000;
      e.savedDeadline=deadline;
      let lease=null,published=false;
      const baseCurrent=()=>this.entries.get(e.tab)===e&&!e.revoked&&e.savedSerial===serial&&e.context.header_bound&&Number.isFinite(this.clock())&&this.clock()>=started&&this.clock()<e.savedDeadline;
      const current=()=>baseCurrent()&&(!qualified||!lease||producer.current(lease));
      const live=async()=>{
        if(!current())return false;
        if(qualified){
          const config=await this.status();
          if(!current()||!config||config.format!=='workbench-native-status-1'||config.connection_epoch!==e.workbenchEpoch)return false;
        }
        // Read the frame after the awaited authority check: a delayed native
        // navigation event must not leave an old document valid during status.
        return await this.live(e)&&current();
      };
      try{
        if(!Number.isFinite(started)||!await live())return;
        const scope=Object.freeze({tab_id:e.tab,document_id:e.document,...e.context});
        const expected=await producer.resolve(scope,baseCurrent);
        if(qualified&&expected){lease=expected;e.savedLease=lease;}
        if(!expected||!await live())return;
        const query=P.nativeProfileRequest(expected.query),binding={};
        for(const key of ['account_key','person_key','conversation_key']){
          const value=Object.getOwnPropertyDescriptor(expected.binding||{},key);
          if(!value||!Object.hasOwn(value,'value')||typeof value.value!=='string'||!value.value.length||value.value.length>256)return;
          binding[key]=value.value;
        }
        Object.freeze(binding);
        const result=await producer.lookup(qualified?lease:query,live);
        if(!await live())return;
        const clean=savedCard(result,binding,!qualified);
        if(!clean)return;
        if(!current())return;
        this.emit(e.tab,e.document,{type:qualified?'native-qualified-profile-view':'native-saved-profile-view',
          ...(qualified?{format:'native-qualified-profile-view-1'}:{}),context_id:e.context.context_id,generation:e.context.generation,head_token:e.context.head_token,expires_at:deadline,fixture:!qualified,binding,result:clean});
        published=true;
      }catch{/* Failure remains unknown; no cached result, retries or pilot fallback. */}
      finally{if(qualified&&lease&&!published){producer.release(lease);if(e.savedLease===lease)e.savedLease=null;}}
    }
    async editor(value,s){
      this.sender(s);
      const E=typeof module==='object'&&module.exports?require('../lib/profile_editor.js'):globalThis.DSHProfileEditorProtocol;
      E.exact(value,[...contextKeys,'command']);const {command,...context}=value;valid(context);
      const cmd=E.command(command),e=this.entries.get(s.tab.id);
      if(!e||!e.workbench||!e.savedQualified||e.revoked||!e.savedLease||!this.savedProfileContext?.edit||e.document!==s.documentId||contextKeys.some(k=>e.context[k]!==value[k])||!value.header_bound||e.editorPending)throw Error('stale_session');
      const lease=e.savedLease,serial=e.savedSerial,epoch=e.workbenchEpoch,operation={};
      const current=()=>this.entries.get(e.tab)===e&&!e.revoked&&e.savedLease===lease&&e.savedSerial===serial&&
        this.clock()<e.savedDeadline&&this.savedProfileContext.current(lease)&&(!this.readConnectionEpoch||this.readConnectionEpoch()===epoch);
      const live=async()=>{
        if(!current())return false;const config=await this.status();
        return current()&&config?.format==='workbench-native-status-1'&&config.connection_epoch===epoch&&await this.live(e)&&current();
      };
      e.editorPending=operation;
      try{
        if(!await live())throw Error('stale_session');
        const result=await this.savedProfileContext.edit(lease,cmd,live);
        if(!await live())throw Error('stale_session');
        if(cmd.action==='read')return result;
        // A saved mutation is not rolled back by a later read failure. Refresh
        // the card independently and never mix it with another context.
        let profile=null;
        try{const raw=await this.savedProfileContext.lookup(lease,live);if(await live())profile=savedCard(raw,lease.binding,false);}catch{}
        if(!await live())throw Error('stale_session');
        return {...result,profile};
      }
      finally{if(e.editorPending===operation)delete e.editorPending;}
    }
    async savedHeartbeat(e){
      if(e.editorPending)return {state:'workbench_pending'};
      // Only a trusted until-revoked lease may renew its display, never lookup.
      if(e.savedHeartbeatPending){this.clearSaved(e);return {state:'stale'};}
      const operation={};e.savedHeartbeatPending=operation;
      if(e.savedLease&&!this.savedProfileContext.current(e.savedLease))this.clearSaved(e);
      const lease=e.savedLease,serial=e.savedSerial,epoch=e.workbenchEpoch;
      const same=()=>this.entries.get(e.tab)===e&&!e.revoked&&e.workbenchEpoch===epoch&&e.savedLease===lease&&e.savedSerial===serial;
      const live=async()=>{
        if(!same())return false;
        const config=await this.status();
        if(!same()||!config||config.format!=='workbench-native-status-1'||config.connection_epoch!==epoch)return false;
        return await this.live(e)&&same()&&(!this.readConnectionEpoch||this.readConnectionEpoch()===epoch);
      };
      try{
        const visible=await live();
        if(!same())return {state:'stale'};
        if(!visible||lease&&!this.savedProfileContext.current(lease))this.clearSaved(e);
        else if(lease&&this.savedProfileContext.recheck){
          const valid=await this.savedProfileContext.recheck(lease,live);
          if(!same())return {state:'stale'};
          if(!valid)this.clearSaved(e);
          else if(this.savedProfileContext.renewable?.(lease)){
            const oldDeadline=e.savedDeadline,now=this.clock();
            const deadline=Math.min(this.savedProfileContext.expiresAt(lease),now+30000);
            if(!Number.isFinite(deadline)||now>=oldDeadline||!this.savedProfileContext.current(lease))this.clearSaved(e);
            else if(deadline>oldDeadline){
              const receipt=await this.emit(e.tab,e.document,{type:'native-saved-profile-renew',
                context_id:e.context.context_id,generation:e.context.generation,head_token:e.context.head_token,expires_at:deadline});
              if(!same())return {state:'stale'};
              // The old deadline must still hold after acknowledgement. A late
              // receipt cannot restore a hidden, expired or replaced display.
              if(!receipt||receipt.state!=='renewed'||receipt.expires_at!==deadline||
                this.clock()<now||this.clock()>=oldDeadline||!this.savedProfileContext.current(lease)||
                this.readConnectionEpoch&&this.readConnectionEpoch()!==epoch)this.clearSaved(e);
              else e.savedDeadline=deadline;
            }
          }
        }
      }catch{if(same()){this.clearSaved(e);this.view(e,{state:'pending',historical_identity:'not_checked',connection:'service_unavailable'});}}
      finally{if(e.savedHeartbeatPending===operation)delete e.savedHeartbeatPending;}
      return {state:'workbench_pending',header_bound:e.context.header_bound};
    }
    async control(type,value,s){
      this.sender(s);if(type==='identity-pilot-resume'){const result=await this.selection(P.resumeRequest(value),s,true);return result.state==='resumed_candidate'?result:{state:'pending'};}if(type==='identity-pilot-selection')return this.selection(P.selectionRequest(value),s);valid(value,type==='identity-pilot-arm',type==='identity-pilot-recall');const tab=s.tab.id;
      if(type==='identity-saved-profile-heartbeat'){
        const e=this.entries.get(tab);
        // A heartbeat cannot recreate worker state or consume another lookup.
        if(!e||!e.workbench||!e.savedQualified||!e.savedAttempted||e.revoked||!e.savedLease||!this.savedProfileContext||
          e.document!==s.documentId||contextKeys.some(k=>e.context[k]!==value[k])||!value.header_bound)return {state:'stale'};
        const lease=e.savedLease,serial=e.savedSerial;
        await this.savedHeartbeat(e);
        return {state:this.entries.get(tab)===e&&!e.revoked&&e.savedLease===lease&&e.savedSerial===serial&&
          this.savedProfileContext.current(lease)?'saved_current':'stale'};
      }
      if(type==='identity-saved-profile-suspend'){
        const e=this.entries.get(tab);
        if(e&&e.document===s.documentId&&contextKeys.every(k=>e.context[k]===value[k]))this.clearSaved(e);
        return {state:'pending'};
      }
      if(type==='identity-pilot-context'){
        const old=this.entries.get(tab);
        if(old&&(old.pilot||old.savedAttempted)&&!old.revoked&&old.document===s.documentId&&contextKeys.every(k=>old.context[k]===value[k]))
          return old.savedQualified?this.savedHeartbeat(old):{state:'ready',header_bound:value.header_bound};
        if(old)this.clear(old,old.document===s.documentId);
        if(this.entries.size>=32&&!old)throw Error('unavailable');
        const sameContext=old&&old.document===s.documentId&&contextKeys.every(k=>old.context[k]===value[k]);
        // Re-registration repairs a revoked scope; it never renews consumed
        // candidate requests or reconstructs any native proof.
        const e={tab,document:s.documentId,context:{...value},proof:null,pilot:null,
          resumeAttempted:!!(sameContext&&old.resumeAttempted),selectionTokens:sameContext?old.selectionTokens:undefined};this.entries.set(tab,e);
        if(this.savedProfileFixture){
          this.view(e,{state:'pending',historical_identity:'not_checked'});
          await this.savedProfile(e);
          return {state:this.entries.get(tab)===e?'ready':'stale',header_bound:value.header_bound};
        }
        let config;
        try{config=await this.status();}catch(error){if(this.entries.get(tab)===e){this.memories.delete(tab);this.view(e,{state:'pending',historical_identity:'not_checked',connection:error.message==='unauthorized'?'login_required':'service_unavailable'});}return {state:'unavailable'};}
        if(this.entries.get(tab)!==e)return {state:'stale'};
        if(config&&field(config,'format')==='workbench-native-status-1'){
          if(Object.keys(config).length!==2||!Number.isSafeInteger(field(config,'connection_epoch'))||config.connection_epoch<0)throw Error('bad_response');
          this.memories.delete(tab);e.workbench=true;e.workbenchEpoch=config.connection_epoch;
          this.view(e,{state:'pending',historical_identity:'not_checked',connection:'workbench_connected',header_bound:value.header_bound,
            current_identity:'unverified',owner:'unverified',private_context:'unverified',formal_binding:false});
          if(this.savedProfileContext&&this.savedProfileContext.enabled){
            e.savedQualified=true;
            await this.savedProfile(e,this.savedProfileContext,true);
          }
          // No ready acknowledgement: content must not initiate pilot recall,
          // selection or resume from a normal-workbench authentication check.
          // Its existing idle heartbeat will recheck this connection.
          return {state:'workbench_pending',header_bound:value.header_bound};
        }
        if(!config||typeof config.account_key!=='string'||typeof config.provider_namespace!=='string'||!/^[a-f0-9]{64}$/.test(config.artifact_sha256)||config.owner_verified!==false||config.private_context_verified!==false){this.memories.delete(tab);throw Error('bad_response');}
        e.memoryScope=this.memoryScope(e,config);
        if(B.validationScope(config)===null){this.memories.delete(tab);throw Error('bad_response');}
        e.exportSourceStatement=exportStatement(config);
        const saved=this.memories.get(tab);if(saved&&(!e.memoryScope||!this.sameMemoryScope(saved.scope,e.memoryScope)))this.memories.delete(tab);
        e.scope={account_key:config.account_key,account_namespace:config.provider_namespace,owner_state:'unknown',owner_provider_namespace:null,owner_identity_kind:null,owner_identity_value:null,provider_namespace:config.provider_namespace,source_id:'native-profile-navigation',source_version:'F-pilot-wiring-1',opener_tab_id:String(tab),opener_document_id:s.documentId,route_key:'/chat',document_generation:0,selection_generation:value.generation,head_generation:value.generation,selection_token:value.context_id,head_token:value.head_token};
        e.pilot=new I.IdentityPilot({readCurrent:()=>this.entries.get(tab)===e?e.scope:null,approve:()=>this.entries.get(tab)===e&&e.context.header_bound,
          artifactSha256:config.artifact_sha256,clock:this.clock,ttlMs:30000,
          render:v=>this.view(e,v),lookup:async(q,current)=>{
            if(!current()||!await this.live(e)||!current()){this.diagnose(e,'lookup','stale');throw Error('stale_session');}
            this.diagnose(e,'lookup','started',true);let r;try{r=await this.lookup(q);}catch(error){if(this.entries.get(tab)===e)this.memories.delete(tab);this.diagnose(e,'lookup','rejected');throw error;}this.diagnose(e,'lookup','returned',true);
            if(!current()||!await this.live(e)||!current()){this.diagnose(e,'lookup','stale');throw Error('stale_session');}return r;
          }});
        this.view(e,{state:'pending',historical_identity:'not_checked',connection:'connected',header_bound:value.header_bound});
        return {state:'ready',header_bound:value.header_bound};
      }
      if(type==='identity-pilot-recall')return this.recall(value,s);
      if(type!=='identity-pilot-arm')throw Error('invalid_request');
      const e=this.entries.get(tab);
      if(!e||!e.pilot||e.revoked||e.document!==s.documentId||contextKeys.some(k=>e.context[k]!==value[k])||!value.header_bound)return {state:'unbound'};
      this.clear(e,true);e.expected=value.profile_url;e.armedAt=this.clock();e.proof=e.pilot.begin('当前对象');
      return {state:e.proof?'armed':'unbound'};
    }
    async selection(value,s,resume=false){
      const e=this.entries.get(s.tab.id);
      if(!e||!e.pilot||e.revoked||e.document!==s.documentId||contextKeys.some(k=>e.context[k]!==value[k])||!value.header_bound||!this.selectionBudget)return {state:'unbound'};
      if(resume){
        if(e.resumeAttempted||e.selectionTokens&&e.selectionTokens.size||e.proof&&e.proof.expires_at>this.clock())return {state:'pending'};
        e.resumeAttempted=true;
      }
      // Gesture authorization is consumed synchronously, before any async work.
      // The content producer alone attests the trusted gesture and its 3s gate.
      if(!resume){
        if(!e.selectionTokens)e.selectionTokens=new Set();
        const token=value.selection_token.toLowerCase();
        if(e.selectionTokens.has(token)||e.selectionTokens.size>=128)return {state:'stale'};
        e.selectionTokens.add(token);
      }
      const serial=e.selectionSerial=(e.selectionSerial||0)+1,started=this.clock(),expires=started+25000;
      const current=()=>this.entries.get(e.tab)===e&&!e.revoked&&e.selectionSerial===serial&&Number.isFinite(this.clock())&&this.clock()>=started&&this.clock()<expires;
      const scoped=async()=>{if(!current())return false;let config;try{config=await this.status();}catch{return false;}
        if(!current()||!this.sameMemoryScope(this.memoryScope(e,config),e.memoryScope)||!await this.live(e)||!current())return false;
        e.exportSourceStatement=exportStatement(config);return true;};
      if(!Number.isFinite(started)||!await scoped())return {state:'stale'};
      const query={account_key:e.memoryScope.account,provider_namespace:e.memoryScope.namespace,artifact_sha256:e.memoryScope.artifact,sec_uid:value.sec_uid};
      try{
        if(!await (resume?this.selectionBudget.contains(query,e.memoryScope.validationBudgetScope):this.selectionBudget.reserve(query,e.memoryScope.validationBudgetScope)))return {state:resume?'pending':'budget_exhausted'};
        if(!await scoped())return {state:'stale'};
        const response=await this.lookup(Object.freeze(query));
        if(!await scoped())return {state:'stale'};
        if(!response||Object.getPrototypeOf(response)!==Object.prototype)return {state:'pending'};
        const fields=Object.getOwnPropertyDescriptors(response),keys=['format','account_key','provider_namespace','artifact_sha256','sec_uid','status','candidate_count','private_context_verified','owner_verified','formal_identity_match'];
        if(keys.some(k=>!fields[k]||!Object.hasOwn(fields[k],'value')))return {state:'pending'};
        const r=Object.fromEntries(keys.map(k=>[k,fields[k].value]));
        if(r.format!=='pending-profile-lookup-1'||Object.keys(query).some(k=>r[k]!==query[k])||!['pending','zero','conflict'].includes(r.status)||
          !Number.isSafeInteger(r.candidate_count)||r.candidate_count<0||(r.status==='pending'&&r.candidate_count!==1)||
          r.private_context_verified!==false||r.owner_verified!==false||r.formal_identity_match!==false)return {state:'pending'};
        if(!current())return {state:'stale'};
        const summary=H.fromResponse(response,r.status==='pending'&&r.candidate_count===1);
        this.emit(e.tab,e.document,{type:'identity-pilot-view',context_id:e.context.context_id,generation:e.context.generation,...(resume?{resume_token:value.resume_token}:{selection_token:value.selection_token}),expires_at:expires,
          view:{state:resume?'resumed_candidate':'selection_candidate',current_identity:'unverified',historical_identity:r.status==='pending'?'sender_candidate':r.status,owner:'unverified',private_context:'unverified',formal_binding:false,historical_export_source_statement:e.exportSourceStatement,...(summary?{history_summary:summary}:{})}});
        return {state:resume?'resumed_candidate':'selection_candidate'};
      }catch{return {state:'pending'};}
    }
    memoryScope(e,c){
      if(!c||!Number.isSafeInteger(c.connection_epoch)||c.connection_epoch<0||typeof c.account_key!=='string'||typeof c.provider_namespace!=='string'||!/^[a-f0-9]{64}$/.test(c.artifact_sha256)||c.owner_verified!==false||c.private_context_verified!==false)return null;
      const validationBudgetScope=B.validationScope(c);if(validationBudgetScope===null)return null;
      return Object.freeze({document:e.document,account:c.account_key,namespace:c.provider_namespace,artifact:c.artifact_sha256,epoch:c.connection_epoch,validationBudgetScope});
    }
    sameMemoryScope(a,b){return !!a&&!!b&&['document','account','namespace','artifact','epoch','validationBudgetScope'].every(k=>a[k]===b[k]);}
    syncMemoryEpoch(epoch,config){
      const valid=Number.isSafeInteger(epoch)&&epoch>=0;
      const checkBudget=arguments.length>1,budgetScope=checkBudget?B.validationScope(config):undefined;
      for(const [tab,memory]of this.memories)if(!valid||memory.scope.epoch!==epoch||(checkBudget&&memory.scope.validationBudgetScope!==budgetScope))this.memories.delete(tab);
      // Initializing entries have no scope yet. Do not cancel their status await.
      for(const e of this.entries.values())if(e.memoryScope&&(!valid||e.memoryScope.epoch!==epoch||(checkBudget&&(budgetScope===null||e.memoryScope.validationBudgetScope!==budgetScope)))&&!e.revoked){e.revoked=true;e.exportSourceStatement='unknown';this.clear(e);}
    }
    async recall(value,s){
      const e=this.entries.get(s.tab.id);
      if(!e||!e.pilot||e.revoked||e.document!==s.documentId||contextKeys.some(k=>e.context[k]!==value[k])||!value.header_bound)return {state:'pending'};
      // Recall cannot arm, renew, or overwrite an active native attempt.
      if(e.proof&&e.proof.expires_at>this.clock())return {state:'pending'};
      if(e.recallAttempted)return {state:'pending'};
      e.recallAttempted=true;const serial=e.recallSerial=(e.recallSerial||0)+1,proof=e.proof;
      const current=()=>this.entries.get(e.tab)===e&&!e.revoked&&e.recallSerial===serial&&e.proof===proof;
      let config;try{config=await this.status();}catch{if(current()){this.memories.delete(e.tab);this.view(e,{state:'pending',historical_identity:'not_checked',connection:'service_unavailable'});}return {state:'pending'};}
      if(!current())return {state:'stale'};
      const scope=this.memoryScope(e,config),memory=this.memories.get(e.tab);
      if(!scope||!this.sameMemoryScope(scope,e.memoryScope)||memory&&!this.sameMemoryScope(scope,memory.scope)){this.memories.delete(e.tab);return {state:'pending'};}
      if(!await this.live(e)||!current())return {state:'stale'};
      e.exportSourceStatement=exportStatement(config);
      const fact=memory&&memory.facts.get(value.sec_uid),now=this.clock();
      if(!fact||!Number.isFinite(now)||fact.expires_at<=now)return {state:'pending'};
      this.emit(e.tab,e.document,{type:'identity-pilot-view',context_id:e.context.context_id,generation:e.context.generation,expires_at:fact.expires_at,
        view:{state:'recalled_candidate',current_identity:'unverified',historical_identity:fact.historical_identity,owner:'unverified',private_context:'unverified',formal_binding:false,historical_export_source_statement:e.exportSourceStatement,...(fact.history_summary?{history_summary:fact.history_summary}:{})}});
      return {state:'recalled'};
    }
    view(e,view){
      if(this.entries.get(e.tab)!==e)return;
      const now=this.clock();
      // A slow lookup must not renew the original identity proof's lifetime.
      const expires=view.state==='candidate'&&e.proof?Math.min(now+30000,e.proof.expires_at):now+30000;
      if(view.state==='candidate'&&(!e.proof||!Number.isFinite(expires)||expires<=now))return;
      const statement=e.revoked||['service_unavailable','login_required'].includes(view.connection)?'unknown':e.exportSourceStatement==='user_confirmed'?'user_confirmed':'unknown';
      this.emit(e.tab,e.document,{type:'identity-pilot-view',context_id:e.context.context_id,generation:e.context.generation,expires_at:expires,view:{...view,historical_export_source_statement:statement}});
    }
    async live(e){
      if(this.entries.get(e.tab)!==e)return false;
      let f;try{f=await this.getFrame({tabId:e.tab,frameId:0});}catch{this.diagnose(e,'live','frame-unavailable');if(this.entries.get(e.tab)===e)this.clear(e);return false;}
      if(this.entries.get(e.tab)!==e||!f||f.documentId!==e.document||!P.chatURL(f.url)){this.diagnose(e,'live','frame-mismatch');if(this.entries.get(e.tab)===e)this.clear(e);return false;}return true;
    }
    profileMatches(url,expected,allowBlank=false){
      if(allowBlank&&url==='about:blank')return true;
      try{const actual=N.parseProfileURL(url),armed=N.parseProfileURL(expected);return actual.identity_kind===armed.identity_kind&&actual.identity_value===armed.identity_value;}catch{return false;}
    }
    rejectedURLReason(url,expected){
      if(url==='')return 'url-empty';
      if(url==='about:blank#blocked')return 'url-blocked-blank';
      if(url==='chrome://newtab/')return 'url-newtab';
      try{return N.parseProfileURL(url).identity_value===N.parseProfileURL(expected).identity_value?'url-profile-same-key':'url-profile-different-key';}catch{return 'url';}
    }
    async created(d){
      const e=this.entries.get(d.sourceTabId);if(!e)return;
      for(const [failed,reason]of [[d.sourceFrameId!==0,'source-frame'],[!Number.isInteger(d.tabId)||d.tabId===d.sourceTabId,'new-target'],[!e.proof,'no-proof'],[this.clock()-e.armedAt>2000,'age'],[d.timeStamp<e.armedAt,'time'],[!this.profileMatches(d.url,e.expected,true),'url']])if(failed){this.diagnose(e,'created',reason==='url'?this.rejectedURLReason(d.url,e.expected):reason);if(e.proof&&reason==='url')this.memories.delete(e.tab);return;}
      if([...this.targets.values()].some(t=>t.entry===e)){this.diagnose(e,'created','duplicate-target');this.clear(e);return;}
      const proof=e.proof;
      if(!await this.live(e)||e.proof!==proof){this.diagnose(e,'created','stale');return;}
      // Another creation callback can register its target while getFrame awaits.
      if([...this.targets.values()].some(t=>t.entry===e)){this.diagnose(e,'created','duplicate-target');this.clear(e);return;}
      this.diagnose(e,'created','accepted',true);
      this.targets.set(d.tabId,{entry:e,proof,url:d.url,at:d.timeStamp,document:null,previousURL:null});
      const committed=this.commits.get(d.tabId);if(committed&&committed.timeStamp>=d.timeStamp)await this.committed(committed,'replay');
    }
    async committed(d,origin='native'){
      if(d.frameId!==0||typeof d.documentId!=='string')return;
      const source=this.entries.get(d.tabId);if(source&&source.document!==d.documentId){this.clear(source);this.entries.delete(d.tabId);}
      if(!this.targets.has(d.tabId)&&![...this.entries.values()].some(e=>e.proof&&this.profileMatches(d.url,e.expected,true)))return;
      const cached=this.commits.get(d.tabId);
      const same=(a,b)=>a.tabId===b.tabId&&a.documentId===b.documentId&&a.url===b.url&&a.timeStamp===b.timeStamp;
      const conflict=cached&&cached.timeStamp===d.timeStamp&&(cached.conflicted===true||!same(cached,d));
      if(!cached||d.timeStamp>cached.timeStamp){
        if(this.commits.size>=64)this.commits.delete(this.commits.keys().next().value);this.commits.set(d.tabId,{...d});
      }else if(conflict&&d.timeStamp===cached.timeStamp)this.commits.set(d.tabId,{...cached,conflicted:true});
      const t=this.targets.get(d.tabId);if(!t)return;
      const e=t.entry,p=t.proof;
      e.traceSequence=(e.traceSequence||0)%999+1;const sequence=e.traceSequence;
      this.traceCommit(t,d,origin==='replay'?'replay':'native','entry',sequence);
      if(conflict||cached&&d.timeStamp<cached.timeStamp){this.diagnose(e,'commit','stale');this.clear(e);return;}
      if(t.received&&same(t.received,d))return;
      t.received={...d};const revision=t.revision=(t.revision||0)+1;
      // A new document after candidate creation revokes immediately, before
      // frame checks or any network wait. The existing replacement gate fails.
      if(t.document&&t.document!==d.documentId&&e.pilot.coordinatorStatus().state==='candidate'){
        e.pilot.replaced(p,{attempt_id:p.attempt_id,opener_tab_id:String(e.tab),opener_document_id:e.document,target_tab_id:String(d.tabId),target_document_id:d.documentId,previous_document_id:t.document,previous_url:t.previousURL,url:d.url});
        this.coordinatorFailure(e,'replaced');this.clear(e);return;
      }
      if(e.proof!==p||d.timeStamp<t.at||!await this.live(e)||e.proof!==p){this.diagnose(e,'commit','stale');return;}
      if(this.targets.get(d.tabId)!==t||t.revision!==revision){this.diagnose(e,'commit','stale');return;}
      this.traceCommit(t,d,origin==='replay'?'replay':'native','resume',sequence);
      if(t.document&&d.url==='about:blank'){this.diagnose(e,'commit','exact-url');this.clear(e);return;}
      const event={attempt_id:p.attempt_id,opener_tab_id:String(e.tab),opener_document_id:e.document,target_tab_id:String(d.tabId),target_document_id:d.documentId};
      if(!t.document){
        if(!this.profileMatches(d.url,e.expected,true)){this.diagnose(e,'commit','initial-url');this.clear(e);return;}
        if(!e.pilot.created(p,{...event,new_tab:true})){this.coordinatorFailure(e,'created');this.clear(e);return;}
      }else if(t.document!==d.documentId){
        if(!this.profileMatches(d.url,e.expected)){this.diagnose(e,'commit','exact-url');this.clear(e);return;}
        if(!e.pilot.replaced(p,{...event,previous_document_id:t.document,previous_url:t.previousURL,url:d.url})){this.coordinatorFailure(e,'replaced');this.clear(e);return;}
      }
      t.document=d.documentId;t.previousURL=d.url;if(d.url==='about:blank'){this.diagnose(e,'commit','blank',true);return;}
      if(!this.profileMatches(d.url,e.expected)){this.diagnose(e,'commit','exact-url');this.clear(e);return;}
        if(!e.pilot.navigated(p,{...event,url:d.url})){this.coordinatorFailure(e,'navigated');this.clear(e);return;}
      this.diagnose(e,'commit','accepted',true);const result=await e.pilot.query(p);this.diagnose(e,'query',result&&result.state==='candidate'?'completed':'rejected',!!result&&result.state==='candidate');
      if(result&&result.state==='candidate'&&this.entries.get(e.tab)===e&&e.proof===p&&this.targets.get(d.tabId)===t&&t.revision===revision&&p.expires_at>this.clock()&&e.memoryScope&&['sender_candidate','zero','conflict'].includes(result.historical_identity)){
        const sec=N.parseProfileURL(e.expected).identity_value,now=this.clock();if(!Number.isFinite(now))return;
        let memory=this.memories.get(e.tab);
        if(!memory||!this.sameMemoryScope(memory.scope,e.memoryScope))memory={scope:e.memoryScope,facts:new Map()};
        // Repeated verification never slides an existing absolute deadline.
        const old=memory.facts.get(sec);
        if(!old||old.expires_at<=now){memory.facts.delete(sec);while(memory.facts.size>=2)memory.facts.delete(memory.facts.keys().next().value);const summary=H.fromResponse(result,result.historical_identity==='sender_candidate');memory.facts.set(sec,{historical_identity:result.historical_identity,expires_at:now+300000,...(summary?{history_summary:summary}:{})});}
        this.memories.set(e.tab,memory);
      }
    }
    history(d){
      if(d.frameId!==0)return;
      const t=this.targets.get(d.tabId);if(!t)return;
      const e=t.entry;
      // A target history event can only preserve its existing document proof.
      // It never supplies creation lineage or starts an additional lookup.
      if(typeof d.documentId!=='string'||d.documentId!==t.document||!this.profileMatches(d.url,e.expected)){
        this.diagnose(e,'history','rejected');this.clear(e);return;
      }
      this.diagnose(e,'history','accepted',true);
    }
    sweep(){for(const e of this.entries.values())if(e.pilot)e.pilot.refresh();const now=this.clock();for(const[k,v]of this.commits)if(now-v.timeStamp>30000)this.commits.delete(k);for(const[k,v]of this.targets)if(now-v.at>30000)this.targets.delete(k);for(const[k,m]of this.memories){for(const[sec,f]of m.facts)if(f.expires_at<=now)m.facts.delete(sec);if(!m.facts.size)this.memories.delete(k);}}
    removed(tab){const e=this.entries.get(tab);if(e){this.clear(e);this.entries.delete(tab);}const t=this.targets.get(tab);if(t)this.clear(t.entry);this.commits.delete(tab);}
    revokeProofs(snapshot){
      // A late failed status request cannot revoke a newer attempt or document.
      for(const {entry:e,proof}of snapshot)if(this.entries.get(e.tab)===e&&e.proof===proof)this.clear(e);
    }
    invalidateAll(){for(const e of this.entries.values())this.clear(e);this.entries.clear();this.targets.clear();this.commits.clear();this.memories.clear();}
  }
  return {NativeHost};
});
