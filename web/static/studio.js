'use strict';
// The studio replays the existing engine log; refreshing never restarts an agent.
const ledgers=new Map(), agentIndex=new Map(), projectIndex=new Map(), flowHistory=[], actorPositions=new Map();
let syncing=false, studioView=(localStorage.getItem('agentic_team_view')==='storage'?'team':(localStorage.getItem('agentic_team_view')||'team')), usageScope='all', usageTimeFilter='all', enabledModels=new Set(), modelSelectionTouched=false, graphSignature='', sessionSignature='', telemetryReady=false, flowLoadedProject=null;
let connectingSource=null, dragLine=null, contextAgent=null;

const brandInfo={
  gemini:{name:'Gemini',logo:'gemini-color',color:'#709afa'},
  glm:{name:'GLM',logo:'zai',color:'#b8c3d9'},
  claude:{name:'Claude',logo:'claude-color',color:'#df9478'},
  openai:{name:'OpenAI',logo:'openai',color:'#75bda6'},
  deepseek:{name:'DeepSeek',logo:'deepseek',color:'#1e66f5'},
  qwen:{name:'Qwen',logo:'qwen',color:'#8b5cf6'},
  minimax:{name:'MiniMax',logo:'minimax',color:'#ea580c'},
  groq:{name:'Groq',logo:'groq',color:'#f55036'},
  ollama:{name:'Ollama',logo:'ollama',color:'#e6e7e0'},
  mistral:{name:'Mistral',logo:'mistral',color:'#ff7000'},
  openrouter:{name:'OpenRouter',logo:'openrouter',color:'#6366f1'},
  other:{name:'Model',logo:'openai',color:'#aaa4c7'}
};

function brand(model){
  const m=(model||'').toLowerCase();
  if(m.includes('gemini'))return 'gemini';
  if(m.includes('claude'))return 'claude';
  if(m.includes('glm')||m.startsWith('zai/'))return 'glm';
  if(/gpt|codex|openai|astra/.test(m))return 'openai';
  if(m.includes('deepseek'))return 'deepseek';
  if(m.includes('qwen')||m.includes('dashscope'))return 'qwen';
  if(m.includes('minimax'))return 'minimax';
  if(m.includes('mistral')||m.includes('codestral'))return 'mistral';
  if(m.includes('ollama'))return 'ollama';
  if(m.includes('groq'))return 'groq';
  if(m.includes('openrouter'))return 'openrouter';
  return 'other';
}


function modelLabel(model){return model.split('/').slice(1).join('/')||model;}
function pretty(s){return String(s||'').replaceAll('_',' ');}
function count(n){return new Intl.NumberFormat('en',{notation:n>=10000?'compact':'standard',maximumFractionDigits:1}).format(n||0);}
function clockTime(s){return s?new Date(s).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';}
function agents(t=tree){return [t?.watchdog,t?.ceo,t?.manager,...(t?.workers||[])].filter(Boolean);}
function ledger(aid){if(!ledgers.has(aid))ledgers.set(aid,new TeamTelemetry.Ledger());return ledgers.get(aid);}
function logo(model){const i=el('img');i.src='/logos/'+(brandInfo[brand(model)]||brandInfo.other).logo+'.svg';i.alt=(brandInfo[brand(model)]||brandInfo.other).name;i.className='brand-logo '+brand(model);return i;}
function svgEl(tag,attrs={}){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,String(v));return n;}

function startConnecting(sourceId,e){
  e.stopPropagation();
  e.preventDefault();
  connectingSource=sourceId;
  document.body.classList.add('connecting-mode');
  const sourceEl=document.querySelector(`.actor[data-agent="${sourceId}"]`);
  if(sourceEl)sourceEl.classList.add('connect-source');
  
  const svg=$('team-links');
  if(!dragLine){
    dragLine=svgEl('path',{'class':'drawing-link'});
    svg.append(dragLine);
  }
  
  const moveHandler=evt=>{
    if(!connectingSource)return;
    const box=$('tree').getBoundingClientRect();
    const p=point(connectingSource,true);
    if(!p)return;
    const mx=evt.clientX-box.left, my=evt.clientY-box.top;
    const d=`M${p.x},${p.y} C${p.x},${(p.y+my)/2} ${mx},${(p.y+my)/2} ${mx},${my}`;
    dragLine.setAttribute('d',d);
    const elUnder=document.elementFromPoint(evt.clientX,evt.clientY)?.closest('.actor');
    document.querySelectorAll('.actor.connect-target-candidate').forEach(n=>n.classList.remove('connect-target-candidate'));
    if(elUnder&&elUnder.dataset.agent!==connectingSource){
      elUnder.classList.add('connect-target-candidate');
    }
  };
  
  const upHandler=async evt=>{
    window.removeEventListener('mousemove',moveHandler);
    window.removeEventListener('mouseup',upHandler);
    document.body.classList.remove('connecting-mode');
    if(sourceEl)sourceEl.classList.remove('connect-source');
    document.querySelectorAll('.actor.connect-target-candidate').forEach(n=>n.classList.remove('connect-target-candidate'));
    if(dragLine){dragLine.remove();dragLine=null;}
    const elUnder=document.elementFromPoint(evt.clientX,evt.clientY)?.closest('.actor');
    const targetId=elUnder?.dataset.agent;
    const src=connectingSource;
    connectingSource=null;
    if(targetId&&targetId!==src){
      try{
        await api('/api/agents/connect',{project_name:currentProject,source_id:src,target_id:targetId});
        notice(`Connected ${pretty(agentIndex.get(src)?.name||'Agent')} → ${pretty(agentIndex.get(targetId)?.name||'Agent')}`);
        await refreshTree();
      }catch(err){notice(err.message);}
    }
  };
  window.addEventListener('mousemove',moveHandler);
  window.addEventListener('mouseup',upHandler);
}

function openContextMenu(e, a){
  e.preventDefault();
  e.stopPropagation();
  contextAgent=a;
  const menu=$('agent-context-menu');
  if(!menu)return;
  $('menu-agent-name').textContent=pretty(a.name);
  $('menu-agent-sub').textContent=`${a.role} · ${modelLabel(a.model)}`;
  $('menu-avatar-wrap').replaceChildren(logo(a.model));
  const deleteBtn=menu.querySelector('[data-menu="delete"]');
  if(deleteBtn)deleteBtn.hidden=a.role==='CEO';
  menu.hidden=false;
  const x=Math.min(e.clientX, window.innerWidth - menu.offsetWidth - 12);
  const y=Math.min(e.clientY, window.innerHeight - menu.offsetHeight - 12);
  menu.style.left=Math.max(10,x)+'px';
  menu.style.top=Math.max(10,y)+'px';
}

function setupContextMenuListeners(){
  const menu=$('agent-context-menu');
  if(!menu||menu._ready)return;
  menu._ready=true;
  document.addEventListener('click',e=>{
    if(!menu.contains(e.target))menu.hidden=true;
  });
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape')menu.hidden=true;
  });
  menu.querySelectorAll('button[data-menu]').forEach(btn=>{
    btn.addEventListener('click',guard(async()=>{
      menu.hidden=true;
      if(!contextAgent)return;
      const actionType=btn.dataset.menu;
      const a=contextAgent;
      if(actionType==='chat'){
        tab='chat';await openSession(a.id);
      }else if(actionType==='direct-message'){
        $('quick-msg-target').textContent=pretty(a.name);
        $('quick-msg-text').value='';
        $('quick-message-dialog').showModal();
      }else if(actionType==='interrupt'){
        await api('/api/chat',{project_name:currentProject,target_agent_id:a.id,content:'Pause turn and wait for steering instructions.',is_interrupt:true});
        notice('Interrupted '+pretty(a.name));
        await refreshTree();
      }else if(actionType==='resume'){
        await action('resume_agent',{target_agent_id:a.id});
        notice('Resumed '+pretty(a.name));
        await refreshTree();
      }else if(actionType==='connect'){
        await chooseConnection(a);
      }else if(actionType==='disconnect'){
        if(a.parent_id)await api('/api/agents/disconnect',{project_name:currentProject,source_id:a.parent_id,target_id:a.id});
        for(const cid of (a.connections||[]))await api('/api/agents/disconnect',{project_name:currentProject,source_id:a.id,target_id:cid});
        notice('Disconnected '+pretty(a.name));
        await refreshTree();
      }else if(actionType==='edit-task'){
        $('edit-agent-title').textContent=pretty(a.name);
        $('edit-agent-name').value=a.name;
        $('edit-agent-task').value=a.current_task;
        $('edit-task-dialog').showModal();
      }else if(actionType==='logs'){
        tab='events';await openSession(a.id);
      }else if(actionType==='usage'){
        tab='usage';await openSession(a.id);
      }else if(actionType==='delete'){
        if(confirm(`Stop worker ${pretty(a.name)}, archive files and delete folder?`)){
          await action('terminate_worker',{worker_id:a.id,cleanup_folder:true});
          if(active?.id===a.id){$('session').hidden=true;active=null;}
          notice('Terminated worker '+pretty(a.name));
          await refreshTree();
        }
      }
    }));
  });
}

function makeActor(a){
  const c=el('div',undefined,'actor '+brand(a.model)+' '+a.status);
  c.dataset.agent=a.id;c.dataset.role=a.role.toLowerCase();
  c.dataset.specialty=/quality|review|audit|judge/i.test(a.name+' '+a.current_task)?'review':'build';
  const heading=el('button',undefined,'actor-heading');heading.type='button';
  heading.append(
    el('strong',a.role==='CEO'?'CEO':a.role==='MANAGER'?'Manager':a.role==='WATCHDOG'?'Root Watchdog':pretty(a.name)),
    el('small',a.role==='WORKER'?'Worker':a.role==='WATCHDOG'?'Antigravity CLI Agent':pretty(a.name))
  );
  heading.setAttribute('aria-label','Move '+pretty(a.name)+'; arrow keys adjust position');
  heading.title='Drag to move · arrow keys to adjust';
  const body=el('button',undefined,'actor-inspect');body.type='button';body.setAttribute('aria-label','Open '+pretty(a.name)+' conversation');
  const avatar=el('div',undefined,'avatar');avatar.append(logo(a.model));
  const eyebrows=el('span',undefined,'eyebrows');eyebrows.append(el('i','' ,'brow-left'),el('i','','brow-right'));
  const eyes=el('span',undefined,'eyes');eyes.append(el('i','','eye-left'),el('i','','eye-right'));
  const sweat=el('span',undefined,'sweat-drops');sweat.append(el('i','','sweat s1'),el('i','','sweat s2'));
  avatar.append(eyebrows,eyes,sweat,el('span','z','sleep-z z1'),el('span','z','sleep-z z2'),el('span',undefined,'role-prop'));
  const hands=el('div',undefined,'hands');hands.append(el('i'),el('i'));avatar.append(hands,el('div','▦','keyboard'));
  const base=el('div',undefined,'actor-base');base.append(el('span',modelLabel(a.model),'model-name'),el('span','','actor-status'),el('small','','actor-usage'));
  body.append(avatar,base);body.onclick=()=>{tab='chat';openSession(a.id);};
  const menu=el('button','···','actor-menu');menu.type='button';menu.setAttribute('aria-label','Actions for '+pretty(a.name));
  menu.onclick=e=>openContextMenu(e,a);c.append(heading,body,menu);
  c.addEventListener('contextmenu',e=>openContextMenu(e,a));
  let drag=null;
  const position=(x,y)=>{
    const pos={x:Math.max(16,Math.min(19000,x)),y:Math.max(16,Math.min(19000,y))};
    actorPositions.set(a.id,pos);
    c.style.left=pos.x+'px';
    c.style.top=pos.y+'px';
    const treeEl=$('tree');
    if(treeEl){
      const currentW=parseFloat(treeEl.style.width)||treeEl.clientWidth;
      const currentH=parseFloat(treeEl.style.height)||treeEl.clientHeight;
      if(pos.x+240>currentW)treeEl.style.width=(pos.x+260)+'px';
      if(pos.y+240>currentH)treeEl.style.height=(pos.y+260)+'px';
    }
    requestAnimationFrame(drawLinks);
    return pos;
  };
  const save=async pos=>{try{await api('/api/agents/'+a.id+'/position',{project_name:currentProject||a.project_name,...pos});a.position=pos;}catch(err){notice('Position not saved: '+err.message);graphSignature='';renderTeam();}};
  heading.onpointerdown=e=>{if(e.button!==0)return;e.preventDefault();const p=actorPositions.get(a.id);drag={x:e.clientX,y:e.clientY,base:{...p},moved:false};heading.setPointerCapture(e.pointerId);};
  heading.onpointermove=e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(!drag.moved&&Math.hypot(dx,dy)<5)return;drag.moved=true;c.classList.add('dragging');position(drag.base.x+dx,drag.base.y+dy);};
  heading.onpointerup=()=>{if(!drag)return;const moved=drag.moved;drag=null;c.classList.remove('dragging');if(moved)save(actorPositions.get(a.id));};
  heading.onpointercancel=()=>{if(drag)position(drag.base.x,drag.base.y);drag=null;c.classList.remove('dragging');};
  heading.onkeydown=e=>{const delta={ArrowLeft:[-20,0],ArrowRight:[20,0],ArrowUp:[0,-20],ArrowDown:[0,20]}[e.key];if(!delta)return;e.preventDefault();const p=actorPositions.get(a.id);save(position(p.x+delta[0],p.y+delta[1]));};
  return c;
}

function computeTreeLayout(treeData, containerW=900){
  const workers=(treeData?.workers||[]).filter(Boolean);
  const ceos=(treeData?.ceo?[treeData.ceo]:[]).filter(Boolean);
  const managers=(treeData?.manager?[treeData.manager]:[]).filter(Boolean);
  const watchdogs=(treeData?.watchdog?[treeData.watchdog]:[]).filter(Boolean);

  const floorW=Math.max(640,(containerW||900)-16);
  const cardW=196, gapX=20, stepX=cardW+gapX, stepY=214;
  const n=workers.length;
  const maxColsFit=Math.max(1,Math.floor((floorW-32+gapX)/stepX));
  const cols=Math.min(Math.max(1,n),maxColsFit);

  // Place Watchdog on the left (x=24) and CEO/Manager to the right-center so they are well-spaced yet inside floorW
  const watchdogX=24;
  const minCenterForWatchdog=watchdogs.length?(watchdogX+cardW+140+cardW/2):(floorW/2);
  const centerX=Math.min(floorW-cardW/2-20,Math.max(minCenterForWatchdog,Math.round(floorW*0.54)));
  const positions=new Map();

  watchdogs.forEach(a=>{
    positions.set(a.id,{x:watchdogX,y:126});
  });
  ceos.forEach(a=>{
    positions.set(a.id,{x:Math.round(centerX-cardW/2),y:20});
  });
  managers.forEach(a=>{
    positions.set(a.id,{x:Math.round(centerX-cardW/2),y:226});
  });

  const numRows=Math.ceil(Math.max(1,n)/cols);
  for(let r=0;r<numRows;r++){
    const startIdx=r*cols;
    const endIdx=Math.min(n,startIdx+cols);
    const countInRow=endIdx-startIdx;
    if(countInRow<=0)break;
    const rowW=countInRow*cardW+(countInRow-1)*gapX;
    const startX=Math.max(16,Math.round((floorW-rowW)/2));
    const rowY=438+r*stepY;
    for(let j=0;j<countInRow;j++){
      const a=workers[startIdx+j];
      positions.set(a.id,{x:Math.round(startX+j*stepX),y:rowY});
    }
  }
  const maxY=438+Math.max(1,numRows)*stepY+28;
  return {positions,width:Math.round(floorW),height:Math.round(maxY)};
}

window.resetLayout=async function(){
  const scrollEl=$('tree')?.parentElement;
  const containerW=scrollEl?.clientWidth||900;
  const layout=computeTreeLayout(tree,containerW);
  const updates=[];
  for(const a of agents(tree)){
    a.position=null;
    updates.push(api('/api/agents/'+a.id+'/position',{project_name:currentProject||a.project_name,x:null,y:null}));
  }
  await Promise.all(updates);
  actorPositions.clear();
  if(scrollEl){scrollEl.scrollLeft=0;scrollEl.scrollTop=0;}
  graphSignature='';
  await refreshTree();
  notice('Auto-arranged all agents to fit the Team floor.');
};
function renderTeam(){
  const aa=agents();for(const a of aa)agentIndex.set(a.id,a);
  if(document.querySelector('.actor.dragging'))return;
  const containerW=$('tree')?.parentElement?.clientWidth||900;
  const sig=containerW+':'+aa.map(a=>a.id+a.status+a.name+a.model+JSON.stringify(a.connections||[])+JSON.stringify(a.position)).join('|');
  if(sig!==graphSignature){
    graphSignature=sig;$('tree').replaceChildren();
    const links=svgEl('svg',{'class':'team-links','aria-hidden':'true'});links.id='team-links';$('tree').append(links);

    const layout=computeTreeLayout(tree,containerW);
    const ceoLayoutPos=tree?.ceo?layout.positions.get(tree.ceo.id):null;

    let maxX=layout.width, maxY=layout.height;
    for(const a of aa){
      let pos=a.position||layout.positions.get(a.id)||{x:40,y:440};
      // Snap legacy off-screen positions or colliding Watchdog positions into the responsive layout
      if(a.position&&(a.position.x+204>containerW+40||a.position.x<8)){
        pos=layout.positions.get(a.id)||pos;
      }else if(a.role==='WATCHDOG'&&ceoLayoutPos&&Math.abs(ceoLayoutPos.x-pos.x)<220){
        pos=layout.positions.get(a.id)||pos;
      }
      actorPositions.set(a.id,pos);
      maxX=Math.max(maxX,pos.x+216);
      maxY=Math.max(maxY,pos.y+210);
    }
    $('tree').style.width=Math.max(containerW-4,maxX)+'px';
    $('tree').style.height=maxY+'px';

    for(const a of aa){
      const pos=actorPositions.get(a.id);
      const c=makeActor(a);
      c.style.left=pos.x+'px';
      c.style.top=pos.y+'px';
      $('tree').append(c);
    }
    if(!aa.length)$('tree').append(el('p','Create a project to open its team floor.','empty-state'));
    requestAnimationFrame(drawLinks);
  }
  $('agent-count').textContent=aa.length+' agents';$('working-count').textContent=aa.filter(a=>a.status==='working').length+' working';
  $('rail-project').textContent=pretty(currentProject||'No project');
  updateActors();renderUsage();renderFeed();setupContextMenuListeners();
  if(flowLoadedProject!==currentProject)loadHandoffs().catch(err=>notice(err.message));
}

function updateActors(){
  const nowMs=Date.now();
  for(const c of document.querySelectorAll('.actor')){
    const a=agentIndex.get(c.dataset.agent);
    if(!a)continue;
    const l=ledger(a.id),u=l.totals();
    const samples=[...l.samples.values()];
    const recentSamples=samples.filter(s=>nowMs-Date.parse(s.time)<90000);
    const recentOut=recentSamples.reduce((n,s)=>n+(s.output||0),0);
    const recentTotal=recentSamples.reduce((n,s)=>n+(s.input||0)+(s.output||0),0);
    const recentEvents=l.activity.filter(ev=>nowMs-Date.parse(ev.time)<60000).length;
    const avgPerSample=samples.length>0?(samples.reduce((n,s)=>n+(s.input||0)+(s.output||0),0)/samples.length):800;
    const isPeakWorking=a.status==='working'&&(recentOut>350||recentTotal>Math.max(1200,avgPerSample*1.25)||recentEvents>=3);
    const isErrorState=a.status==='failed'||a.status==='blocked_loop'||(a.status==='paused'&&a.last_error&&/quota|error|disconnect|exhausted|rate|limit|timeout/i.test(a.last_error));

    c.classList.toggle('idle',a.status==='idle');
    c.classList.toggle('paused',a.status==='paused');
    c.classList.toggle('working',a.status==='working');
    c.classList.toggle('focused',a.status==='working'&&recentOut>200);
    c.classList.toggle('peak-working',isPeakWorking);
    c.classList.toggle('error-state',!!isErrorState);
    c.classList.toggle('selected',active?.id===a.id);
    c.querySelector('.actor-status').textContent=states[a.status]||a.status;
    c.querySelector('.actor-usage').textContent=u.reported?count(u.input+u.output)+' tokens':u.estimated?count(u.estimated)+' estimated tokens':'Usage not reported';
    c.title=pretty(a.name)+' · '+(l.activity.at(-1)?.text.slice(0,220)||a.current_task);
  }
}

function point(id,bottom){
  const c=document.querySelector(`.actor[data-agent="${id}"]`);
  if(!c)return null;
  const box=$('tree').getBoundingClientRect();
  const r=c.getBoundingClientRect();
  return{x:r.left+r.width/2-box.left,y:(bottom?r.bottom:r.top)-box.top};
}

function drawLinks(flight){
  const svg=$('team-links');if(!svg)return;svg.replaceChildren();const box=$('tree').getBoundingClientRect();svg.setAttribute('viewBox',`0 0 ${box.width} ${box.height}`);
  
  // 1. Hierarchy links (parent_id)
  for(const a of agents()){
    if(!a.parent_id)continue;const p=point(a.parent_id,true),q=point(a.id,false);if(!p||!q)continue;
    const d=`M${p.x},${p.y} C${p.x},${(p.y+q.y)/2} ${q.x},${(p.y+q.y)/2} ${q.x},${q.y}`;
    svg.append(svgEl('path',{d,'class':'hierarchy-link'}));
  }
  
  // 2. Supervisory Watchdog links (Watchdog -> CEO & Manager)
  const wd = tree?.watchdog;
  if(wd){
    const wdActor = document.querySelector(`.actor[data-agent="${wd.id}"]`);
    if(wdActor){
      const wr = wdActor.getBoundingClientRect();
      const p = {x: wr.right - box.left, y: wr.top + wr.height/2 - box.top};
      for(const targetId of (wd.connections||[])){
        const targetActor = document.querySelector(`.actor[data-agent="${targetId}"]`);
        if(!targetActor)continue;
        const tr = targetActor.getBoundingClientRect();
        const q = {x: tr.left - box.left, y: tr.top + tr.height/2 - box.top};
        const midX = (p.x + q.x) / 2;
        const d = `M${p.x},${p.y} C${midX},${p.y} ${midX},${q.y} ${q.x},${q.y}`;
        svg.append(svgEl('path',{d,'class':'watchdog-link','title':'Supervisory Conduit to '+pretty(agentIndex.get(targetId)?.name||'Agent')}));
      }
    }
  }

  // 3. Custom graph connections (connections)
  for(const a of agents()){
    if(a.role === 'WATCHDOG') continue;
    for(const targetId of (a.connections||[])){
      if(targetId===a.parent_id)continue;
      const p=point(a.id,true),q=point(targetId,false);if(!p||!q)continue;
      const d=`M${p.x},${p.y} C${p.x+30},${(p.y+q.y)/2} ${q.x-30},${(p.y+q.y)/2} ${q.x},${q.y}`;
      const pathEl=svgEl('path',{d,'class':'custom-link','title':'Collaboration link (click to remove)'});
      const hit=svgEl('path',{d,'class':'link-hitbox'});
      hit.addEventListener('click',guard(async()=>{
        if(confirm(`Sever connection between ${pretty(a.name)} and ${pretty(agentIndex.get(targetId)?.name||'agent')}?`)){
          await requireUpdatedEngine();
          await api('/api/agents/disconnect',{project_name:currentProject,source_id:a.id,target_id:targetId});
          notice('Connection removed.');
          await refreshTree();
        }
      }));
      svg.append(pathEl,hit);
    }
  }

  // 3. Flight animations
  if(flight){
    const p=point(flight.sender_id,true),q=point(flight.recipient_id,false);if(p&&q){
      const d=`M${p.x},${p.y} Q${(p.x+q.x)/2+70},${(p.y+q.y)/2} ${q.x},${q.y}`;
      const path=svgEl('path',{d,'class':'message-link'});svg.append(path);
      const dot=svgEl('circle',{r:4,fill:'#d9edb4'}),motion=svgEl('animateMotion',{dur:'1.8s',repeatCount:3,path:d});dot.append(motion);svg.append(dot);setTimeout(()=>drawLinks(),5600);
    }
  }
}
async function syncTelemetry(){
  if(syncing)return;syncing=true;
  try{
   const projects=(await api('/api/projects')).projects;
   const savedProject=localStorage.getItem('agentic_team_project');
   if(!currentProject&&projects.length){
     if(savedProject&&projects.some(p=>p.name===savedProject))currentProject=savedProject;
     else currentProject=projects[0].name;
     localStorage.setItem('agentic_team_project',currentProject);
     await refreshTree();
   }
   // Refresh the picker as other MCP hosts create new pipelines.
   const names=projects.map(p=>p.name);if([...$('projects').options].map(o=>o.value).join('|')!==names.join('|')){
    $('projects').replaceChildren(...names.map(name=>{const o=el('option',pretty(name));o.value=name;return o;}));if(currentProject)$('projects').value=currentProject;
   }
   if($('usage-scope')&&$('usage-scope').options.length<=2){
    const prevScope=usageScope;
    $('usage-scope').replaceChildren(
      el('option','All projects'),
      el('option','Current project ('+pretty(currentProject)+')'),
      ...projects.map(p=>{const o=el('option','Project: '+pretty(p.name));o.value=p.name;return o;})
    );
    $('usage-scope').options[0].value='all';
    $('usage-scope').options[1].value='project';
    $('usage-scope').value=prevScope;
   }
   for(const p of projects){const t=p.name===currentProject&&tree?tree:await api('/api/tree?project='+encodeURIComponent(p.name));projectIndex.set(p.name,t);for(const a of agents(t))agentIndex.set(a.id,a);}
   await Promise.all([...agentIndex.values()].map(async a=>{
    const l=ledger(a.id);
    for(let page=0;page<30;page++){
     const batch=(await api('/api/agents/'+a.id+'/events?after='+l.cursor)).events;l.ingest(batch);if(batch.length<500)break;
    }
   }));
   telemetryReady=true;$('usage-health').textContent='Reported usage · estimates shown separately';updateActors();renderUsage();renderFeed();
   if(active)await refreshSession();
  }catch(err){$('usage-health').textContent='Telemetry unavailable: '+err.message;}finally{syncing=false;}
 }
 function getTimeWindowMs(){
  if(usageTimeFilter==='1h')return 3600*1000;
  if(usageTimeFilter==='5h')return 5*3600*1000;
  if(usageTimeFilter==='24h')return 24*3600*1000;
  if(usageTimeFilter==='7d')return 7*24*3600*1000;
  return Infinity;
 }
 function scopedAgents(){
  return [...agentIndex.values()].filter(a=>{
   if(usageScope==='all')return true;
   if(usageScope==='project')return a.project_name===currentProject;
   return a.project_name===usageScope;
  });
 }
 function totalsFor(aa,timeWindowMs=Infinity,model=null){
  const t={input:0,output:0,cache:0,write:0,reported:0,missing:0,cacheKnown:0,estimated:0};
  for(const a of aa){
   const u=ledger(a.id).totals(timeWindowMs,model,a.model);
   for(const k of ['input','output','cache','write','estimated'])t[k]+=u[k];
   if(u.reported)t.reported++;else t.missing++;
   if(u.cacheKnown)t.cacheKnown++;
  }
  return t;
 }
 function metric(label,value,note){const d=el('div',undefined,'metric');d.append(el('small',label),el('strong',value),el('span',note));return d;}
 function uniqModels(rawList){
  const seen=new Map();
  const cFn=(window.TeamTelemetry&&TeamTelemetry.canonModel)||((m)=>String(m||'').split('/').pop().toLowerCase().replace(/[\s_]+/g,'-'));
  for(const m of rawList){
   if(!m)continue;
   const k=cFn(m);
   if(!seen.has(k)||m.includes('/'))seen.set(k,m);
  }
  return [...seen.values()];
 }
 function renderUsage(){
  const windowMs=getTimeWindowMs();
  const aa=scopedAgents(),models=uniqModels(aa.flatMap(a=>[a.model,...[...ledger(a.id).samples.values()].map(s=>s.model||a.model)]));if(!modelSelectionTouched)enabledModels=new Set(models);
  const visible=aa,t={input:0,output:0,cache:0,write:0,reported:0,cacheKnown:0,missing:0,estimated:0};
  for(const m of enabledModels){const u=totalsFor(aa,windowMs,m);for(const k of ['input','output','cache','write','estimated'])t[k]+=u[k];if(u.reported)t.reported++;if(u.cacheKnown)t.cacheKnown++;}
  const timeLabel=usageTimeFilter==='all'?'':' ('+(usageTimeFilter==='1h'?'Last 1h':usageTimeFilter==='5h'?'Last 5h':usageTimeFilter==='24h'?'Last 24h':'Last 7d')+')';
  $('metrics').replaceChildren(metric('INPUT'+timeLabel,t.reported?count(t.input):'—','Includes cache reads / writes'),metric('OUTPUT'+timeLabel,t.reported?count(t.output):'—','Reported generated tokens'),metric('CACHE READ'+timeLabel,t.cacheKnown?count(t.cache):'—',t.cacheKnown&&t.input?(100*t.cache/t.input).toFixed(1)+'% of reported input':'Not reported'),metric('TOTAL'+timeLabel,t.reported?count(t.input+t.output):'—',t.missing?t.missing+' agents without reported usage':'Input + output'));
  $('model-filters').replaceChildren();
  for(const m of models){const b=el('button',undefined,'model-filter '+brand(m)+(enabledModels.has(m)?' on':''));b.setAttribute('aria-pressed',String(enabledModels.has(m)));b.append(logo(m),el('span',modelLabel(m)));b.onclick=()=>{modelSelectionTouched=true;if(enabledModels.has(m))enabledModels.delete(m);else enabledModels.add(m);renderUsage();};$('model-filters').append(b);}
  $('usage-coverage').textContent=telemetryReady?`${t.reported} model groups have reported usage in this window. ${t.estimated?count(t.estimated)+' tokens in latest CLI response estimates; excluded from totals. ':''}Cache is part of input, not added twice.`:'Reading saved execution history…';
  drawUsageChart(visible,windowMs);
  const cFn=(window.TeamTelemetry&&TeamTelemetry.canonModel)||((m)=>String(m||'').split('/').pop().toLowerCase().replace(/[\s_]+/g,'-'));
  const body=$('usage-rows');body.replaceChildren();
  for(const m of models){const mc=cFn(m),group=aa.filter(a=>cFn(a.model)===mc||[...ledger(a.id).samples.values()].some(s=>cFn(s.model)===mc)),u=totalsFor(group,windowMs,m),tr=el('tr');const name=el('td');name.append(logo(m),el('span',modelLabel(m)));tr.append(name,...[u.reported?count(u.input):'—',u.reported?count(u.output):'—',u.cacheKnown?count(u.cache):'—',u.estimated?count(u.estimated):'—',u.reported+'/'+group.length].map(v=>el('td',v)));body.append(tr);}
  const projectAgents=agents(),projectModels=uniqModels(projectAgents.flatMap(a=>[a.model,...[...ledger(a.id).samples.values()].map(s=>s.model||a.model)]));
  const bars=$('model-bars');bars.replaceChildren();const max=Math.max(1,...projectModels.map(m=>{const u=totalsFor(projectAgents,windowMs,m);return u.input+u.output;}));
  for(const m of projectModels){const u=totalsFor(projectAgents,windowMs,m),r=el('div',undefined,'model-bar '+brand(m));const h=el('div');h.append(logo(m),el('span',modelLabel(m)),el('b',u.reported?count(u.input+u.output):'—'));const track=el('progress');track.max=max;track.value=u.input+u.output;track.setAttribute('aria-label',modelLabel(m)+' total tokens');r.append(h,track);bars.append(r);}
  const projectTotal=totalsFor(agents(),windowMs);$('mini-usage').textContent=projectTotal.reported?count(projectTotal.input+projectTotal.output):'—';$('mini-cache').textContent=projectTotal.cacheKnown?count(projectTotal.cache):'—';
 }
 function drawUsageChart(aa,windowMs=Infinity){
  const svg=$('usage-chart');svg.replaceChildren();const W=1000,H=230,L=58,R=20,T=18,B=35,n=24;
  const now=Date.now();
  const cFn=(window.TeamTelemetry&&TeamTelemetry.canonModel)||((m)=>String(m||'').split('/').pop().toLowerCase().replace(/[\s_]+/g,'-'));
  const enabledCanons=new Set([...enabledModels].map(m=>cFn(m)));
  let all=aa.flatMap(a=>[...ledger(a.id).samples.values()].map(s=>({...s,model:s.model||a.model}))).filter(s=>enabledCanons.has(cFn(s.model)));
  if(windowMs!==Infinity){
   all=all.filter(s=>s.time&&(now-Date.parse(s.time))<=windowMs);
  }
  let min=all.length?(windowMs===Infinity?Math.min(...all.map(s=>Date.parse(s.time))):now-windowMs):(windowMs===Infinity?now-3600000:now-windowMs);
  let max=now;
  if(max-min<60000)min=max-60000;
  const modelNames=uniqModels(all.map(s=>s.model)),bins=new Map(modelNames.map(m=>[cFn(m),{label:m,vals:Array(n).fill(0)}]));
  for(const s of all){const i=Math.min(n-1,Math.max(0,Math.floor((Date.parse(s.time)-min)/(max-min)*n))),k=cFn(s.model);if(bins.has(k))bins.get(k).vals[i]+=(s.input||0)+(s.output||0);}
  const heights=Array(n).fill(0);for(const b of bins.values())b.vals.forEach((v,i)=>heights[i]+=v);const peak=Math.max(1,...heights);
  for(let i=0;i<4;i++){const y=T+(H-T-B)*i/3;svg.append(svgEl('line',{x1:L,x2:W-R,y1:y,y2:y,'class':'chart-grid'}));const txt=svgEl('text',{x:L-10,y:y+4,'text-anchor':'end'});txt.textContent=count(peak*(1-i/3));svg.append(txt);}
  const used=Array(n).fill(0),width=(W-L-R)/n;
  for(const b of bins.values()){const m=b.label;b.vals.forEach((v,i)=>{if(!v)return;const h=v/peak*(H-T-B),r=svgEl('rect',{x:L+i*width+3,y:H-B-(used[i]+v)/peak*(H-T-B),width:width-6,height:h,fill:brandInfo[brand(m)].color,rx:1,tabindex:0});const title=svgEl('title');title.textContent=`${modelLabel(m)} · ${clockTime(new Date(min+i/n*(max-min)).toISOString())} · ${v.toLocaleString()} tokens`;r.append(title);svg.append(r);used[i]+=v;});}
  for(let i=0;i<5;i++){const txt=svgEl('text',{x:L+(W-L-R)*i/4,y:H-8,'text-anchor':i===0?'start':i===4?'end':'middle'});txt.textContent=new Date(min+(max-min)*i/4).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});svg.append(txt);}
  if(!all.length){const txt=svgEl('text',{x:W/2,y:H/2,'text-anchor':'middle','class':'chart-empty'});txt.textContent='No reported usage for the selected window / models';svg.append(txt);}
 }
function renderFeed(){
 const feed=$('activity-feed');if(!feed)return;const items=agents().flatMap(a=>ledger(a.id).activity.slice(-6).map(e=>({...e,agent:a}))).sort((a,b)=>Date.parse(b.time)-Date.parse(a.time)).slice(0,7);
 feed.replaceChildren();for(const item of items){const b=el('button',undefined,'feed-item');b.append(el('time',clockTime(item.time)),logo(item.agent.model));const copy=el('div');copy.append(el('strong',pretty(item.agent.name)),el('span',item.type.replaceAll('_',' ')+' · '+item.text.replace(/\s+/g,' ').slice(0,105)));b.append(copy);b.onclick=()=>openSession(item.agent.id);feed.append(b);}
 if(!items.length)feed.append(el('p','Execution events appear here when an agent starts a turn.','empty-state'));
 $('flow-feed').replaceChildren();for(const m of flowHistory.slice(-5).reverse()){const d=el('div',undefined,'flow-item');d.append(el('small',clockTime(m.timestamp)+' · '+m.kind),el('strong',pretty(m.sender_name)+' → '+pretty(m.recipient_name)),el('span',m.content.slice(0,120)));$('flow-feed').append(d);}
 if(!flowHistory.length)$('flow-feed').append(el('p','Messages between agents appear here. Dots travel along the connection when a message is sent.','empty-state'));
}
async function loadHandoffs(){
 if(!currentProject)return;
 flowLoadedProject=currentProject;
 const aa=agents(), msgs=[];
 for(const a of aa){
  try{
   const data=await api('/api/agents/'+a.id+'/messages');
   for(const m of (data.messages||[])){if(!msgs.some(x=>x.id===m.id))msgs.push(m);}
  }catch{}
 }
 msgs.sort((a,b)=>Date.parse(a.timestamp)-Date.parse(b.timestamp));
 flowHistory.length=0;
 flowHistory.push(...msgs.slice(-20));
 renderFeed();
}
// Small, DOM-only Markdown renderer: no HTML execution, including in tool output.
function inlineText(node,text){const rx=/(`[^`]+`|\*\*[^*]+\*\*)/g;let start=0;for(const m of text.matchAll(rx)){node.append(document.createTextNode(text.slice(start,m.index)));node.append(el(m[0][0]==='`'?'code':'strong',m[0].slice(m[0][0]==='`'?1:2,m[0][0]==='`'?-1:-2)));start=m.index+m[0].length;}node.append(document.createTextNode(text.slice(start)));}
function markdown(text){
 const root=el('div',undefined,'markdown'),lines=String(text||'').split('\n');let pre=null,table=null;
 for(const line of lines){
  if(line.trim().startsWith('```')){if(pre)pre=null;else{pre=el('pre');root.append(pre);}table=null;continue;}
  if(pre){pre.append(document.createTextNode(line+'\n'));continue;}
  if(/^\s*\|.*\|\s*$/.test(line)){
   const cells=line.trim().slice(1,-1).split('|').map(x=>x.trim());if(cells.every(c=>/^:?-+:?$/.test(c)))continue;
   if(!table){table=el('table');root.append(table);}const row=el('tr');for(const c of cells){const td=el('td');inlineText(td,c);row.append(td);}table.append(row);continue;
  }table=null;
  if(!line.trim())continue;let m=line.match(/^(#{1,6})\s+(.*)$/),n;
  if(m){n=el('h'+Math.min(4,m[1].length+1));inlineText(n,m[2]);}
  else{n=el('p',undefined,/^\s*[-*]\s/.test(line)?'list-line':'');inlineText(n,line.replace(/^\s*[-*]\s/,'• '));}root.append(n);
 }return root;
}
function toolDetails(item){
 const root=el('div',undefined,'tool-detail'),split=item.text.indexOf('\n');
 try{
  const name=item.text.slice(0,split),args=JSON.parse(item.text.slice(split+1));
  root.append(el('strong',name.replace('mcp__agentic_team__','')));
  if(args.description)root.append(el('p',args.description));
  if(args.command)root.append(el('pre','$ '+args.command));
  if(args.file_path||args.path)root.append(el('code',args.file_path||args.path));
  const rest={...args};delete rest.description;delete rest.command;delete rest.file_path;delete rest.path;
  if(Object.keys(rest).length){const details=el('details');details.append(el('summary',args.content?'File content':args.old_string?'Changes':'Arguments'),el('pre',args.content||JSON.stringify(rest,null,2)));root.append(details);}
 }catch{root.append(el('pre',item.text));}
 return root;
}
function sessionHeader(){
 $('session-title').textContent=pretty(active.name);$('session-role').textContent=active.role+' / '+(states[active.status]||active.status);$('session-model').replaceChildren(logo(active.model),el('span',modelLabel(active.model)));
 $('session-meta').textContent=[active.harness,active.pid?'PID '+active.pid:'No active process',active.session_id?'Session '+active.session_id:'No session yet',active.auth_slot_id?'Slot: '+active.auth_slot_id:'Auth: auto'].join(' · ');
 if($('open-console')&&window.engineRevision>=2){
  const labels={'codex':'Open Codex CLI ↗','claude_code':'Open Claude CLI ↗','antigravity':'Open Antigravity CLI ↗'};
  $('open-console').textContent=labels[active.harness]||'Open CLI Session ↗';
 }
 $('terminate').hidden=active.role!=='WORKER';updateActors();
 const u=ledger(active.id).totals();$('session-usage').replaceChildren(metric('INPUT',u.reported?count(u.input):'—',''),metric('OUTPUT',u.reported?count(u.output):'—',''),metric('CACHE',u.cacheKnown?count(u.cache):'—',''));
 if($('session-auth-select')){
  const select=$('session-auth-select');
  select.closest('.session-auth-binding').hidden=active.harness!=='antigravity';
  if(active.harness==='antigravity'&&select.dataset.agent!==active.id){
   const snapshot=active;select.dataset.agent=snapshot.id;select.replaceChildren(el('option','Loading saved accounts…'));
   api('/health').then(h=>h.studio_revision?api('/api/auth/google/accounts'):null).then(data=>{
    if(active?.id!==snapshot.id)return;
    select.replaceChildren();const auto=el('option',data?'Automatic failover':'Available after engine update');auto.value='auto';select.append(auto);
    for(const account of data?.accounts||[]){const o=el('option',account.email);o.value=account.account_id;select.append(o);}
    select.value=snapshot.forced_auth_slot_id||'auto';select.disabled=!data;$('btn-apply-auth').disabled=!data;
   }).catch(err=>notice(err.message));
  }
 }
 document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
}
async function refreshSession(){
  if(!active)return;
  sessionHeader();
  const c=$('session-content');
  if(!c)return;
  document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
  
  if(tab!=='chat')delete c.dataset.signature;
  if(tab==='chat'){
    const data=await api('/api/agents/'+active.id+'/messages');
    const msgs=data.messages||[];
    const live=active.status==='working'?ledger(active.id).activity.filter(item=>item.run===active.run_id&&/response|text|assistant/i.test(item.type)).at(-1):null;
    const signature=active.id+':chat:'+msgs.length+':'+(msgs.at(-1)?.id||'')+':'+(live?.text.length||0)+':'+(live?.text.slice(-120)||'');
    if(c.dataset.signature===signature)return;
    const follow=c.scrollHeight-c.scrollTop-c.clientHeight<90;
    const oldScroll=c.scrollTop;
    const initial=!c.dataset.signature?.startsWith(active.id+':chat:');
    c.dataset.signature=signature;c.replaceChildren();
    if(!msgs.length&&!live)c.append(el('p',active.status==='working'?'The agent is working. Its response will appear here; Activity shows tools and Output shows the process stream.':'No messages yet. Use the input below to steer this agent.','empty-state'));
    for(const m of msgs){
      const d=el('div',undefined,'message');
      d.append(el('small',`${clockTime(m.timestamp)} · ${pretty(m.sender_name)} (${m.sender_role}) → ${pretty(m.recipient_name)} [${m.kind}]`));
      d.append(markdown(m.content));
      c.append(d);
    }
    if(live){const d=el('div',undefined,'message live-response');d.append(el('small','LIVE RESPONSE · '+pretty(active.name)),markdown(live.text));c.append(d);}
    c.scrollTop=(follow||initial)?c.scrollHeight:oldScroll;
  }else if(tab==='events'){
    const l=ledger(active.id);
    c.replaceChildren();
    const ctx=el('div',undefined,'activity-context');
    ctx.append(el('small','CURRENT ASSIGNMENT'));
    ctx.append(el('p',active.current_task||'Resting / no active task'));
    c.append(ctx);
    const items=l.activity.slice().reverse();
    if(!items.length){c.append(el('p','No activity recorded for this agent yet.','empty-state'));return;}
    for(const item of items){
      const d=el('div',undefined,'execution-item'+(item.type==='error'?' error':''));
      d.append(el('small',`${clockTime(item.time)} · ${item.type.toUpperCase()}`));
      if(item.type==='tool')d.append(toolDetails(item));
      else d.append(el('pre',item.text));
      c.append(d);
    }
  }else if(tab==='terminal'){
    c.replaceChildren();
    const l=ledger(active.id);
    c.append(el('div','Live process output stream','terminal-label'));
    const pre=el('pre',undefined,'terminal-output');
    pre.textContent=l.raw.map(r=>r.text).join('')||'No console output captured.';
    c.append(pre);
    c.scrollTop=c.scrollHeight;
  }else if(tab==='status'){
    const data=await api('/api/agents/'+active.id+'/status_md');
    c.replaceChildren();
    c.append(markdown(data.content||'No status report available yet.'));
  }else if(tab==='usage'){
    const l=ledger(active.id), u=l.totals();
    c.replaceChildren();
    const grid=el('div',undefined,'agent-usage-grid');
    grid.append(metric('INPUT',u.reported?count(u.input):'—','Prompt tokens'),
                metric('OUTPUT',u.reported?count(u.output):'—','Generated tokens'),
                metric('CACHE READ',u.cacheKnown?count(u.cache):'—','KV cache reuse'),
                metric('TOTAL',u.reported?count(u.input+u.output):'—','Total processed'));
    c.append(grid);
    if(l.samples.size){
      const tbl=el('table');
      tbl.innerHTML='<thead><tr><th>Time</th><th>Run ID</th><th>Input</th><th>Output</th><th>Cache</th></tr></thead>';
      const tb=el('tbody');
      for(const [k,s] of [...l.samples.entries()].reverse().slice(0,30)){
        const tr=el('tr');
        tr.append(el('td',clockTime(s.time)),el('td',s.run||'—'),el('td',count(s.input)),el('td',count(s.output)),el('td',s.cache!==null?count(s.cache):'—'));
        tb.append(tr);
      }
      tbl.append(tb);
      c.append(tbl);
    }
  }
}
window.refreshSession=refreshSession;

async function openSession(id){
  const a=selected(id)||agentIndex.get(id);
  if(!a)return;
  active=a;
  $('session').hidden=false;
  document.body.classList.add('inspecting');
  sessionHeader();
  await refreshSession();
}
window.openSession=openSession;

async function renderAuthPool(){
 try{
  const data=await api('/api/auth/google/accounts');
  $('cancel-google-login').hidden=!data.login_pending;
  if(data.execution_policy)$('auth-health').textContent=data.execution_policy+'. Local turn counts are not provider quota.';
  const accounts=data.accounts||[];
  const fmt = (typeof count === 'function') ? count : (n => (n||0).toLocaleString());
  function renderUsageCell(usageData, windowLabel){
    const td=el('td',undefined,'token-cell-td');
    if(!usageData||typeof usageData!=='object'){
      td.text='Not recorded';
      td.textContent='Not recorded';
      return td;
    }
    const inTok=usageData.input_tokens||0;
    const outTok=usageData.output_tokens||0;
    const cacheTok=usageData.cache_read_tokens||0;
    const turns=usageData.turns||0;
    const totalPrompt=inTok+cacheTok;
    const cacheRate=totalPrompt>0?Math.round((cacheTok/totalPrompt)*100):0;
    const wrap=el('div',undefined,'tok-cell-wrap');
    const lineOut=el('div',undefined,'tok-metric-line');
    lineOut.append(el('span','Out: ','tok-lbl'),el('strong',fmt(outTok),'tok-val tok-val-out'));
    const lineIn=el('div',undefined,'tok-metric-line');
    lineIn.append(el('span','In: ','tok-lbl'),el('span',fmt(inTok),'tok-val tok-val-in'));
    const lineCache=el('div',undefined,'tok-metric-line');
    const cacheSpan=el('span',`${fmt(cacheTok)} (${cacheRate}%)`,'tok-val tok-val-cache');
    cacheSpan.title=totalPrompt>0?`${cacheRate}% prompt cache hit rate (${fmt(cacheTok)} cached / ${fmt(totalPrompt)} total prompt)`:'No prompt tokens cached yet';
    lineCache.append(el('span','Cache: ','tok-lbl'),cacheSpan);
    const lineTurns=el('div',undefined,'tok-sub-line');
    lineTurns.append(el('small',`${turns} ${turns===1?'turn':'turns'}`,'tok-turns'));
    wrap.append(lineOut,lineIn,lineCache,lineTurns);
    td.append(wrap);
    td.title=`${windowLabel} Usage: ${fmt(outTok)} output, ${fmt(inTok)} input, ${fmt(cacheTok)} cache read (${cacheRate}% cache rate), across ${turns} turns`;
    return td;
  }

  function renderPoolTier(metricsId, rowsId, isClaude){
    const metricsEl=$(metricsId);
    const tbody=$(rowsId);
    if(!tbody)return;
    let healthy=0, quotaBlocked=0, activeCount=0;
    for(const acc of accounts){
      const hState=isClaude?(acc.claude_health_state||'healthy'):acc.health_state;
      const inCd=isClaude?!!acc.claude_in_cooldown:!!acc.in_cooldown;
      const runCnt=isClaude?(acc.claude_active_agents||0):(acc.active_agents||0);
      if((hState==='healthy'||(hState==='unknown'&&acc.credential_saved))&&!inCd&&acc.health_state!=='disabled')healthy++;
      if(hState==='quota-blocked'||inCd)quotaBlocked++;
      activeCount+=runCnt;
    }
    if(metricsEl){
      metricsEl.replaceChildren(
        metric('TOTAL ACCOUNTS',accounts.length,isClaude?'Claude 4.6 Opus profiles':'Registered Google profiles'),
        metric('AVAILABLE',healthy,isClaude?'Available for Claude 4.6 Opus':'Available for Gemini 3.8 Flash'),
        metric('QUOTA-BLOCKED',quotaBlocked,'Cooldown in effect'),
        metric('ACTIVE AGENTS',activeCount,'Currently assigned')
      );
    }
    tbody.replaceChildren();
    if(!accounts.length){
      const tr=el('tr'), td=el('td','No Google accounts registered yet. Click "+ Add Google account" to connect your accounts.','empty-state');
      td.colSpan=6;tr.append(td);tbody.append(tr);return;
    }
    for(const acc of accounts){
      const tr=el('tr');
      const tdAcc=el('td');
      tdAcc.append(el('strong',acc.email),el('small',acc.account_id+' · '+(acc.credential_saved?'Saved login':'Login not saved')));
      const tdState=el('td');
      const hState=acc.health_state==='disabled'?'disabled':(isClaude?(acc.claude_health_state||'healthy'):acc.health_state);
      const inCd=isClaude?!!acc.claude_in_cooldown:!!acc.in_cooldown;
      const cdUntil=isClaude?acc.claude_cooldown_until:acc.cooldown_until;
      const lastErr=isClaude?acc.claude_last_error:acc.last_error;
      const badgeClass=hState==='healthy'&&!inCd?'badge-healthy':(hState==='quota-blocked'||inCd?'badge-blocked':(hState==='rate-limited'?'badge-rate-limited':(hState==='disabled'?'badge-disabled':'badge-unknown')));
      const stateLabel=inCd?'quota-blocked':(hState==='unknown'&&acc.credential_saved?'saved · unverified':hState);
      const badge=el('span',stateLabel.toUpperCase(),'auth-badge '+badgeClass);
      if(stateLabel==='saved · unverified')badge.title='Login is saved and available for work. Status confirms to HEALTHY on first completed turn.';
      tdState.append(badge);
      if(inCd&&cdUntil){
        const pill=el('small','','cooldown-pill');
        pill.dataset.cooldownUntil=cdUntil;
        tdState.append(pill);
      }
      if(lastErr){tdState.append(el('small',lastErr.slice(0,90),'account-err'));}
      if(!isClaude&&acc.last_run_error&&hState!=='healthy'){
        const note=el('small','Last run had a launch/task error; this is separate from login health.');
        note.title=acc.last_run_error;tdState.append(note);
      }
      const u5=isClaude?acc.claude_usage_5h:acc.usage_5h;
      const uw=isClaude?acc.claude_usage_weekly:acc.usage_weekly;
      const td5h=renderUsageCell(u5,'5-Hour');
      const tdW=renderUsageCell(uw,'7-Day');

      const tdAgents=el('td',undefined,'agents-cell-td');
      const running=isClaude?(acc.claude_active_agents||0):(acc.active_agents||0);
      const maxSlots=acc.max_concurrent||4;
      if(running>0){
        const badgeBusy=el('span',`● ${running}/${maxSlots} running`,'badge-agent-busy');
        badgeBusy.title=`${running} active agents running concurrently (max ${maxSlots})`;
        tdAgents.append(badgeBusy);
      }else{
        const badgeIdle=el('span',`Idle (0/${maxSlots})`,'badge-agent-idle');
        badgeIdle.title=`0 active agents assigned out of ${maxSlots} concurrent slots`;
        tdAgents.append(badgeIdle);
      }

      const tdActions=el('td',undefined,'auth-actions-cell');
      const isPending=data.login_pending===acc.account_id;

      if(!acc.credential_saved || isPending){
        const btnLogin=el('button','Sign in','secondary text-button auth-action-btn');
        btnLogin.title='Open terminal window to sign in to this Google account';
        btnLogin.onclick=guard(async()=>{const r=await api(`/api/auth/google/accounts/${acc.account_id}/login`,{});notice(r.note||'Sign-in opened.','success');await renderAuthPool();});

        const btnCapture=el('button',isPending?'Capture login 📥':'Capture login','secondary text-button auth-action-btn'+(isPending?' btn-highlight':''));
        btnCapture.title=isPending?'Action required: click to save completed credential from terminal':'Save credential from completed terminal sign-in to this slot';
        btnCapture.onclick=guard(async()=>{const r=await api(`/api/auth/google/accounts/${acc.account_id}/capture`,{});notice(r.note||`Captured credentials for ${acc.account_id}`,'success');await renderAuthPool();});
        tdActions.append(btnLogin,btnCapture);
      }

      const btnTest=el('button','Check setup','secondary text-button auth-action-btn');
      btnTest.title='Verify configuration and execute a live ping to Google Antigravity';
      btnTest.onclick=guard(async()=>{
        const origText=btnTest.text||btnTest.textContent||'Check setup';
        if(btnTest.textContent)btnTest.textContent='Testing...';
        try{
          const r=await api(`/api/auth/google/accounts/${acc.account_id}/test`,{});
          if(r.latency_ms){
            notice(r.ok?`⚡ ${acc.account_id} (${acc.email}) verified! Live ping: ${r.latency_ms}ms`:`Test failed: ${r.note}`, r.ok?'success':'error');
          }else{
            notice(r.note||'Checks local setup; provider quota is not verified', r.ok?'success':'error');
          }
          await renderAuthPool();
        }finally{
          if(btnTest.textContent)btnTest.textContent=origText;
        }
      });

      const btnToggle=el('button',acc.health_state==='disabled'?'Enable':'Disable','secondary text-button auth-action-btn');
      btnToggle.title=acc.health_state==='disabled'?'Enable this account for agent scheduling':'Disable this account temporarily';
      btnToggle.onclick=guard(async()=>{const a=acc.health_state==='disabled'?'enable':'disable';await api(`/api/auth/google/accounts/${acc.account_id}/${a}`,{});notice(`Account ${acc.account_id} ${a}d.`,'success');await renderAuthPool();});

      const btnDel=el('button','Delete','danger text-button auth-action-btn btn-danger');
      btnDel.title='Permanently remove this account profile';
      btnDel.onclick=guard(async()=>{if(!confirm(`Delete profile for ${acc.email} (${acc.account_id})?`))return;await api(`/api/auth/google/accounts/${acc.account_id}`,undefined,'DELETE');notice(`Deleted account ${acc.account_id}.`,'success');await renderAuthPool();});

      tdActions.append(btnTest,btnToggle,btnDel);
      tr.append(tdAcc,tdState,td5h,tdW,tdAgents,tdActions);
      tbody.append(tr);
    }
  }

  renderPoolTier('auth-metrics','auth-rows',false);
  renderPoolTier('claude-auth-metrics','claude-auth-rows',true);
  updateCooldownTickers();
 }catch(err){if($('auth-health'))$('auth-health').textContent='Auth pool unavailable: '+err.message;}
}
window.refreshAuthPool=renderAuthPool;
 let activeAuthProviderTab='google';
 async function renderProviderAuthTab(provKey, forceLive=false){
  activeAuthProviderTab=provKey;
  document.querySelectorAll('#auth-provider-tabs .provider-tab').forEach(b=>{
    const isMatch = b.dataset.providerTab===provKey;
    b.classList.toggle('active', isMatch);
    b.classList.toggle('on', isMatch);
  });
  const googleSec=$('google-auth-section'), provSec=$('provider-auth-section');
  const addBtn=$('btn-quick-signin');
  if(provKey==='google'){
   if(googleSec)googleSec.hidden=false;
   if(provSec)provSec.hidden=true;
   if(addBtn)addBtn.hidden=false;
   await renderAuthPool();
   return;
  }
  if(googleSec)googleSec.hidden=true;
  if(provSec)provSec.hidden=false;
  if(addBtn)addBtn.hidden=true;
  if(!provSec)return;
  try{
   const data=await api('/api/settings'), s=data.settings||{}, presets=data.provider_presets||{};
   const presetMap={codex:'openai',deepseek:'deepseek',zai:'zai',experiential:'experiential',claude:'anthropic'};
   const presetKey=presetMap[provKey]||provKey;
   const preset=presets[presetKey]||{label:provKey.toUpperCase(),alias:provKey,adapter:'openai_compatible',models:[]};
   const alias=preset.alias||provKey;
   let liveModels = null;
   try {
     const liveRes = await api(`/api/providers/${encodeURIComponent(alias)}/models?force=${forceLive ? 'true' : 'false'}`);
     if (liveRes && Array.isArray(liveRes.models) && liveRes.models.length) {
       liveModels = liveRes.models;
     }
   } catch (_) {}
   const saved=(s.providers&&s.providers[alias])||preset;
   const displayModels = liveModels || saved.models || [];
   const hasKey=!!(s.api_keys&&s.api_keys[alias]);
   const cliEnabled=provKey==='codex'?!!s.cli_auth_enabled?.codex:(provKey==='claude'?!!s.cli_auth_enabled?.claude:false);
   const isConnected=hasKey||cliEnabled;
   provSec.replaceChildren();
   const card=el('div',undefined,'settings-section');
   card.style.cssText='background:#131920;border:1px solid var(--line);border-radius:6px;padding:18px;';
   const head=el('div');
   head.style.cssText='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;';
   const titleWrap=el('div');
   titleWrap.append(el('h3',preset.label||alias.toUpperCase()),el('p',`Alias: ${alias} · Adapter: ${saved.adapter||'direct'} · Base URL: ${saved.base_url||'Default'}`,'muted'));
   const statusBadge=el('span',isConnected?'CONNECTED':'NOT CONFIGURED','auth-badge '+(isConnected?'badge-healthy':'badge-unknown'));
   head.append(titleWrap,statusBadge);
   const modelsHdr=el('div');
   modelsHdr.style.cssText='display:flex;align-items:center;justify-content:space-between;margin:10px 0 8px;';
   modelsHdr.append(
     el('small', `AVAILABLE MODELS RECEIVED FROM API (${displayModels.length})`, 'muted'),
     (() => {
       const btnRef = el('button', '🔄 Fetch Live Models', 'secondary text-button');
       btnRef.type = 'button';
       btnRef.style.fontSize = '11px';
       btnRef.onclick = guard(async () => {
         btnRef.textContent = 'Querying API...';
         await renderProviderAuthTab(provKey, true);
         notice(`Fetched live models from ${preset.label || alias}`);
       });
       return btnRef;
     })()
   );
   const modelsWrap=el('div');
   modelsWrap.style.cssText='margin:8px 0 14px;display:flex;flex-wrap:wrap;gap:6px;max-height:220px;overflow-y:auto;padding:4px 0;';
   for(const m of displayModels){
    const pill=el('span',`${alias}/${m}`,'chip');
    pill.style.cssText='background:#0f1419;border:1px solid #2b3642;color:#ffffff;padding:4px 10px;border-radius:4px;font-size:12px;';
    modelsWrap.append(pill);
   }
   const actionsWrap=el('div');
   actionsWrap.style.cssText='margin-top:14px;display:flex;gap:10px;align-items:center;';
   const cfgBtn=el('button','Configure API Key / Settings','primary');
   cfgBtn.onclick=guard(async()=>{
    await settings();
    if($('provider-preset')&&presets[presetKey]){
     $('provider-preset').value=presetKey;
     $('provider-preset').dispatchEvent(new Event('change'));
    }
    $('settings-dialog').showModal();
   });
   actionsWrap.append(cfgBtn);
   card.append(head,modelsHdr,modelsWrap,actionsWrap);
   provSec.append(card);
  }catch(err){
   provSec.textContent='Failed to load provider details: '+err.message;
  }
 }
 document.querySelectorAll('#auth-provider-tabs .provider-tab').forEach(b=>{
  b.addEventListener('click',()=>renderProviderAuthTab(b.dataset.providerTab));
 });
 let authPollInterval=null;
 function updateCooldownTickers(){
  const pills=document.querySelectorAll('[data-cooldown-until]');
  for(const p of pills){
   const target=new Date(p.dataset.cooldownUntil).getTime();
   const diff=Math.max(0,Math.floor((target-Date.now())/1000));
   if(diff<=0){
    p.textContent='ready · cooldown expired';
    p.className='cooldown-pill ready';
    continue;
   }
   const days=Math.floor(diff/86400);
   const hours=Math.floor((diff%86400)/3600);
   const mins=Math.floor((diff%3600)/60);
   const secs=diff%60;
   const secStr=secs<10?'0'+secs:secs;
   if(days>0)p.textContent=`reset in ${days}d ${hours}h ${mins}m ${secStr}s`;
   else if(hours>0)p.textContent=`reset in ${hours}h ${mins}m ${secStr}s`;
   else p.textContent=`reset in ${mins}m ${secStr}s`;
  }
 }
  if(!window.cooldownTimer){
   window.cooldownTimer=setInterval(updateCooldownTickers,1000);
  }

  async function renderStorage(){
   try{
    const data=await api('/api/storage/status');
    if(!data||!data.available){
      if($('storage-health'))$('storage-health').textContent='Storage cleanup audit report unavailable.';
      return;
    }
    if($('storage-health')&&data.hard_safety_invariant){
      $('storage-health').textContent=data.hard_safety_invariant;
    }
    if($('mini-storage-reclaim')){
      const rec=data.reclaimable_space?.total_immediate_safe_reclaim_gb??99.15;
      $('mini-storage-reclaim').textContent=`${rec} GB`;
    }
    if($('storage-approval-badge')){
      $('storage-approval-badge').textContent='🔒 '+(data.approval_state||'PENDING HUMAN OWNER APPROVAL').replaceAll('_',' ').toUpperCase();
    }
    if($('storage-metrics-grid')){
      const rec=data.reclaimable_space||{};
      const prot=data.protected_core_assets||{};
      const drives=data.disk_status||{};
      const cDrive=drives['C']||{free_gb:34.41,percent_free:7.24};
      const totalProtected=Math.round(((prot.protected_gb||7.71)+(prot.canonical_gb||7.75))*100)/100||15.46;
      $('storage-metrics-grid').replaceChildren(
        metric('FREE C: (CURRENT)',`${cDrive.free_gb||34.41} GB`,`${cDrive.percent_free||7.24}% free of drive C:`),
        metric('SAFE RECLAIM',`${rec.total_immediate_safe_reclaim_gb||99.15} GB`,'Expands C: to 133.56 GB'),
        metric('SAFE CACHE/TEMP',`${rec.safe_cache_temp_cleanup_gb||29.02} GB`,'Zero code or weight impact'),
        metric('EXACT DUPLICATES',`${rec.exact_duplicates_gb||70.13} GB`,'Identical content hashes'),
        metric('HISTORICAL CHECKPOINTS',`${rec.historical_checkpoints_gb||56.68} GB`,'16 reproducible checkpoints'),
        metric('EXTERNAL MIGRATION',`${rec.optional_external_drive_migration_gb||86.84} GB`,'Move to D:/G: drives'),
        metric('PROTECTED CORE',`${totalProtected} GB`,'Canonical base & active data')
      );
    }
    if($('storage-review-cache-list')){
      const clist=$('storage-review-cache-list');
      clist.replaceChildren();
      const safeItems=data.temp_and_agent_cache_audit?.safe_now||[];
      if(!safeItems.length){
        clist.append(el('li','No cache targets listed.'));
      }else{
        for(const item of safeItems.slice(0,10)){
          const li=el('li');
          li.append(el('strong',`${item.size_gb} GB`),document.createTextNode(` — ${item.path} (${item.reason})`));
          clist.append(li);
        }
      }
    }
    if($('storage-review-duplicates-list')){
      const dlist=$('storage-review-duplicates-list');
      dlist.replaceChildren();
      const clusters=data.duplicate_clusters||[];
      if(!clusters.length){
        dlist.append(el('div','No duplicate clusters recorded.','muted'));
      }else{
        for(const cl of clusters.slice(0,6)){
          const card=el('div',undefined,'duplicate-cluster-card');
          card.style.cssText='background:#181f25;padding:8px 12px;border:1px solid var(--line);border-radius:4px;margin-bottom:8px;';
          card.append(el('div',`Hash: ${cl.sha256?cl.sha256.slice(0,16)+'...':'cluster'} · Reclaimable: ${cl.reclaimable_gb||0} GB (${cl.count||0} copies)`,'muted'));
          const ul=el('ul');ul.style.cssText='margin:4px 0 0;padding-left:18px;color:var(--muted);';
          for(const f of (cl.files||[]).slice(0,3)){
            ul.append(el('li',f));
          }
          card.append(ul);
          dlist.append(card);
        }
      }
    }
    if($('storage-review-protected-list')){
      const plist=$('storage-review-protected-list');
      plist.replaceChildren();
      const pset=data.protected_core_assets?.protected_set||[];
      if(!pset.length){
        plist.append(el('li','No protected items recorded.'));
      }else{
        for(const p of pset){
          plist.append(el('li',typeof p==='string'?p:`${p.name||p.path} (${p.size_gb||0} GB)`));
        }
      }
    }
    if($('storage-drives-list')){
      const container=$('storage-drives-list');
      container.replaceChildren();
      const drives=data.disk_status||{};
      const driveKeys=Object.keys(drives);
      if(!driveKeys.length){
        container.append(el('div','No drive capacity telemetry found.','muted'));
      }else{
        for(const k of driveKeys){
          const d=drives[k];
          const div=el('div',undefined,'drive-card');
          div.style.cssText='background:#181f25;padding:12px;border:1px solid var(--line);border-radius:6px;';
          const title=el('div',undefined,'drive-header');
          title.style.cssText='display:flex;justify-content:space-between;font-size:12px;font-weight:600;margin-bottom:6px;';
          title.append(el('span',`Drive ${k}`),el('span',`${d.free_gb||0} GB Free of ${d.total_gb||0} GB`,'muted'));
          const bar=el('progress');
          bar.max=d.total_gb||1;
          bar.value=(d.total_gb||0)-(d.free_gb||0);
          bar.style.width='100%';
          div.append(title,bar);
          container.append(div);
        }
      }
    }
    if($('storage-protected-list')){
      const list=$('storage-protected-list');
      list.replaceChildren();
      const protectedItems=data.protected_core_assets?.protected_set||[];
      if(!protectedItems.length){
        list.append(el('li','No protected items recorded.'));
      }else{
        for(const item of protectedItems){
          const li=el('li');
          const strong=el('strong',item.name||item.path||'Asset');
          li.append(strong,document.createTextNode(` — ${item.path||''} (${item.size_gb||0} GB) · ${item.protection_reason||'Critical asset'}`));
          list.append(li);
        }
      }
    }
    if($('storage-categories-rows')){
      const tbody=$('storage-categories-rows');
      tbody.replaceChildren();
      const breakdown=data.category_breakdown||{};
      const catKeys=Object.keys(breakdown);
      if(!catKeys.length){
        const tr=el('tr'), td=el('td','No category breakdown recorded.','empty-state');
        td.colSpan=5;
        tr.append(td);
        tbody.append(tr);
      }else{
        for(const catName of catKeys){
          const cat=breakdown[catName];
          const tr=el('tr');
          const tdCat=el('td');
          tdCat.append(el('strong',pretty(catName)));
          const tdCount=el('td',`${cat.count||0}`);
          const tdSize=el('td',`${cat.total_gb||0} GB`);
          const tdSafety=el('td');
          const isSafe=(cat.safety_classification||'').toLowerCase().includes('safe');
          const badge=el('span',(cat.safety_classification||'UNKNOWN').toUpperCase(),'auth-badge '+(isSafe?'badge-healthy':'badge-blocked'));
          tdSafety.append(badge);
          const tdAction=el('td',cat.recommended_action||'—');
          tr.append(tdCat,tdCount,tdSize,tdSafety,tdAction);
          tbody.append(tr);
        }
      }
    }
   }catch(err){
     if($('storage-health'))$('storage-health').textContent='Storage status error: '+err.message;
   }
  }
  window.renderStorage=renderStorage;

  function setView(view){
   window.scrollTo({top:0});
   studioView=view;
   $('spawn').hidden=view!=='team'||!tree?.manager;
   localStorage.setItem('agentic_team_view',view);
   $('team-view').hidden=view!=='team';
   $('usage-view').hidden=view!=='usage';
   if($('auth-view'))$('auth-view').hidden=view!=='auth';
   if($('storage-view'))$('storage-view').hidden=view!=='storage';
   document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('on',b.dataset.view===view));
   if(authPollInterval){clearInterval(authPollInterval);authPollInterval=null;}
   if(view==='usage')renderUsage();
   else if(view==='auth'){
    renderAuthPool();
    authPollInterval=setInterval(()=>{if(studioView==='auth')renderAuthPool();},2500);
   }else if(view==='storage'){
    renderStorage();
   }else requestAnimationFrame(drawLinks);
  }
 function connect(){
  socket=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws');
  socket.onopen=()=>{$('connection').textContent='Connected';$('connection').className='connected';};
  socket.onclose=()=>{$('connection').textContent='Reconnecting';$('connection').className='';setTimeout(connect,2500);};
  socket.onmessage=e=>{
   const event=JSON.parse(e.data);if(event.type==='ping'||event.type==='ready')return;
   if(event.type==='new_message'&&event.data.project_name===currentProject){flowHistory.push(event.data);if(flowHistory.length>40)flowHistory.shift();renderFeed();drawLinks(event.data);}
   if(active&&event.type==='new_message'&&(tab==='chat'||tab==='status'))refreshSession().catch(err=>notice(err.message));

   if(event.type==='agent_updated'||event.type==='agent_event'||event.type==='worker_terminated'||event.type==='agents_connected'||event.type==='agents_disconnected'){
    if(!refreshTimer)refreshTimer=setTimeout(()=>{refreshTimer=null;refreshTree().catch(err=>notice(err.message));syncTelemetry().catch(()=>{});},700);
   }else if(!refreshTimer){
    refreshTimer=setTimeout(()=>{refreshTimer=null;refreshTree().catch(err=>notice(err.message));},1200);
   }
  };
 }
 document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>setView(b.dataset.view));
 if($('usage-scope'))$('usage-scope').onchange=()=>{usageScope=$('usage-scope').value;renderUsage();};
 if($('usage-time-filter'))$('usage-time-filter').onchange=()=>{usageTimeFilter=$('usage-time-filter').value;renderUsage();};
 $('close-session').addEventListener('click',()=>{document.body.classList.remove('inspecting');sessionSignature='';updateActors();});
 if($('btn-fullscreen-floor')){
  $('btn-fullscreen-floor').addEventListener('click',()=>{
   const isFull=document.body.classList.toggle('floor-fullscreen');
   $('btn-fullscreen-floor').classList.toggle('active',isFull);
   graphSignature='';
   requestAnimationFrame(()=>{renderTeam();drawLinks();});
  });
 }
 document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&document.body.classList.contains('floor-fullscreen')){
   document.body.classList.remove('floor-fullscreen');
   if($('btn-fullscreen-floor'))$('btn-fullscreen-floor').classList.remove('active');
   graphSignature='';
   requestAnimationFrame(()=>{renderTeam();drawLinks();});
  }
 });
 (()=>{
  const scrollEl=document.querySelector('.canvas-scroll');
  if(!scrollEl)return;
  let pan=null;
  scrollEl.addEventListener('pointerdown',e=>{
   if(e.button!==0)return;
   if(e.target.closest('.actor, button, a, input, select, textarea'))return;
   pan={startX:e.clientX,startY:e.clientY,scrollLeft:scrollEl.scrollLeft,scrollTop:scrollEl.scrollTop};
   scrollEl.classList.add('panning');
   scrollEl.setPointerCapture(e.pointerId);
   e.preventDefault();
  });
  scrollEl.addEventListener('pointermove',e=>{
   if(!pan)return;
   scrollEl.scrollLeft=pan.scrollLeft-(e.clientX-pan.startX);
   scrollEl.scrollTop=pan.scrollTop-(e.clientY-pan.startY);
  });
  const endPan=e=>{
   if(!pan)return;
   pan=null;
   scrollEl.classList.remove('panning');
   try{if(e?.pointerId!==undefined)scrollEl.releasePointerCapture(e.pointerId);}catch(_){}
  };
  scrollEl.addEventListener('pointerup',endPan);
  scrollEl.addEventListener('pointercancel',endPan);
 })();
 window.addEventListener('resize',()=>{graphSignature='';renderTeam();requestAnimationFrame(drawLinks);});
 setInterval(()=>syncTelemetry(),5000);
 setTimeout(()=>syncTelemetry(),800);
 setTimeout(()=>setView(studioView),100);
