(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.DSHChatLayout=api;})(globalThis,function(){
  'use strict';
  // Geometry only: never read native text, inputs, identities or application state.
  function mount(document,host,onMode=()=>{}){
    const win=document.defaultView;
    if(!win||!host.style||!document.querySelectorAll)return {setOpen(){},dispose(){}};
    const width=336,gap=16,reserve=width+gap,owned=new Map();
    let target=null,open=false,disposed=false,frame=0,blocked=null;
    function restore(){
      if(target)for(const [key,row]of owned){
        // Do not undo a newer style supplied by the native application.
        if(target.style.getPropertyValue(key)===row.applied&&target.style.getPropertyPriority(key)==='important'){
          if(row.value)target.style.setProperty(key,row.value,row.priority);else target.style.removeProperty(key);
        }
      }
      owned.clear();target=null;
    }
    function assign(key,value){owned.set(key,{value:target.style.getPropertyValue(key),priority:target.style.getPropertyPriority(key),applied:value});target.style.setProperty(key,value,'important');}
    function mode(value){host.setAttribute('data-dsh-layout',value);onMode(value);}
    function floating(){host.style.removeProperty('--dsh-top');host.style.removeProperty('--dsh-right');host.style.removeProperty('--dsh-height');mode('floating');}
    function update(){
      frame=0;if(disposed)return;
      if(!open){restore();floating();return;}
      const matches=[...document.querySelectorAll('.componentsRightPanelwrapper')].filter(e=>!e.closest('[data-dsh-owned]'));
      if(matches.length!==1||win.innerWidth<1100){restore();floating();return;}
      const candidate=matches[0];
      if(candidate!==target){restore();if(candidate===blocked){floating();return;}
        const box=candidate.getBoundingClientRect();
        if(box.width<800||box.height<300||box.top<0||box.right>win.innerWidth+1){floating();return;}
        target=candidate;assign('margin-right',reserve+'px');assign('min-width','0px');assign('width','calc(100% - '+reserve+'px)');assign('box-sizing','border-box');
      }
      const box=target.getBoundingClientRect(),parent=target.parentElement?.getBoundingClientRect();
      const right=parent?Math.min(parent.right,win.innerWidth):win.innerWidth;
      // If the site refuses the reserved space, leave its layout intact.
      if(!target.isConnected||box.width<440||box.right+reserve>right+2||box.height<300){blocked=target;restore();floating();return;}
      host.style.setProperty('--dsh-top',Math.max(12,box.top)+'px');
      host.style.setProperty('--dsh-right',Math.max(8,win.innerWidth-right+8)+'px');
      host.style.setProperty('--dsh-height',Math.max(240,Math.min(box.height,win.innerHeight-box.top-12))+'px');
      mode('docked');
    }
    function schedule(){if(!disposed&&!frame)frame=win.requestAnimationFrame(update);}
    const observer=new win.MutationObserver(records=>{if(records.some(r=>!r.target.closest?.('[data-dsh-owned]')))schedule();});
    // Structure only. No character data and no message values are observed.
    observer.observe(document.body,{childList:true,subtree:true});
    win.addEventListener('resize',schedule);
    return {setOpen(value){open=value===true;blocked=null;update();},dispose(){disposed=true;observer.disconnect();win.removeEventListener('resize',schedule);if(frame)win.cancelAnimationFrame(frame);restore();}};
  }
  return {mount};
});
