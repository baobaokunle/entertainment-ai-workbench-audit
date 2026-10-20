// Local progress estimate only: no requests, source identities or persisted claims.
export function estimateProgress({started,now,calls,data,phase,running}){
 const elapsedMs=Math.max(0,now-started),rate=elapsedMs>0?calls*60000/elapsedMs:0;
 const remaining=phase&&Number.isSafeInteger(phase.total)?Math.max(0,phase.total-phase.done):null;
 const sampled=running&&phase?.done>=3&&remaining!==null&&now>phase.started;
 return {elapsedMs,readsPerMinute:rate,phase:phase?.label||null,done:phase?.done||0,total:phase?.total??null,
  remainingReads:running?remaining:null,remainingMs:sampled?(now-phase.started)/phase.done*remaining:null,
  remainingData:sampled?Math.round(Math.max(0,data-phase.initialData)/phase.done*remaining):null,
  wholeTaskRemainingMs:null,wholeTaskDataTotal:null,running};
}
