export async function prepareBaseRepair(host,parent,{check=()=>{},progress=()=>{}}={}) {
 let plan=null,offset=0,total=null;
 try {
  for(;;){
   check();const r=await host.call('repair_scan',{parent_run:parent,plan_id:plan,offset});
   plan=r.plan_id;check();
   if(!/^[0-9a-f]{32}$/.test(plan)||!Number.isInteger(r.total)||r.total<offset||(total!==null&&r.total!==total)||!Number.isInteger(r.next_offset)||r.next_offset<offset||r.next_offset>r.total||(!r.done&&r.next_offset===offset)||r.done!==(r.next_offset===r.total))throw Error('REPAIR_CURSOR');
   offset=r.next_offset;total=r.total;progress({processed:offset,total});
   if(r.done)return r;
  }
 }catch(e){if(plan)await host.call('repair_discard',{plan_id:plan}).catch(()=>{});throw e;}
}
