
'use strict';
const $ = id => document.getElementById(id);
let currentProject = null, tree = null, active = null, tab = 'chat', socket;
let refreshTimer, sessionTimer;
let providerPresets = {}, savedProviders = {};
const states = {working:'Working',queued:'Queued',idle:'Resting',failed:'Failed',paused:'Paused',blocked_loop:'Needs guidance',terminated:'Stopped'};
function el(tag,text,cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function notice(text) { $('notice').textContent=text; if($('settings-notice'))$('settings-notice').textContent=text; }
async function api(path,body,method) {
  const r=await fetch(path,{method:method || (body===undefined?'GET':'POST'),headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  let data;
  const text=await r.text();
  try{ data=JSON.parse(text); }catch{ data={detail:text||r.statusText}; }
  if(!r.ok) throw Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data));
  return data;
}
async function action(name,args) { return api('/api/action',{project_name:currentProject,action:name,arguments:args}); }
function guard(fn) { return async e => {try { await fn(e); }catch(err){notice(err.message);} }; }
function selected(id) {
  return [tree?.ceo,tree?.manager,...(tree?.workers||[])].find(a=>a?.id===id);
}
async function refreshProjects() {
  const data=await api('/api/projects');
  $('projects').replaceChildren();
  for(const p of data.projects) { const option=el('option',p.name); option.value=p.name; $('projects').append(option); }
  const savedProject=localStorage.getItem('agentic_team_project');
  if(savedProject&&data.projects.some(p=>p.name===savedProject))currentProject=savedProject;
  else if(!data.projects.some(p=>p.name===currentProject))currentProject=data.projects[0]?.name||null;
  if(currentProject){
    localStorage.setItem('agentic_team_project',currentProject);
    $('projects').value=currentProject; 
    await refreshTree();
  }
  else if(data.legacy_folders.length)notice('Existing folders were preserved. Create a new project; old prototype sessions cannot be reconstructed.');
}
async function refreshTree() {
  if(!currentProject)return;
  tree=await api('/api/tree?project='+encodeURIComponent(currentProject));
  $('project-title').textContent=pretty(currentProject);
  $('project-description').textContent=tree.project.description;
  $('project-state').textContent=agents(tree).some(a=>a.status==='working')?'IN PROGRESS':tree.project.status.toUpperCase();
  $('spawn').hidden=!tree.manager||studioView!=='team';
  renderTeam();
  if(active){const next=selected(active.id);if(next){active=next;sessionHeader();}else{$('session').hidden=true;active=null;document.body.classList.remove('inspecting');}}
}
async function settings(){
  const data=await api('/api/settings'), s=data.settings;
  $('account-claude').checked=!!s.cli_auth_enabled.claude;
  $('account-codex').checked=!!s.cli_auth_enabled.codex;
  $('account-agy').checked=!!s.cli_auth_enabled.agy;
  providerPresets=data.provider_presets; savedProviders=s.providers;
  const preset=$('provider-preset'), previous=preset.value;
  preset.replaceChildren();const custom=el('option','Custom provider');custom.value='';preset.append(custom);
  for(const [id,p] of Object.entries(providerPresets)){const option=el('option',p.label);option.value=id;preset.append(option);}
  preset.value=previous;
  $('configured-providers').textContent='Saved API connections: '+(Object.entries(s.api_keys).filter(([,v])=>v).map(([k])=>k).join(', ')||'None yet');
  $('harnesses').replaceChildren();
  for(const h of data.harnesses){
    if(!h.adapter_supported)continue;
    const row=el('div');
    const info=el('div');
    info.append(el('strong',h.label||h.name),el('small',(h.available?'Installed':'Not installed')+(h.auth_enabled?' · Account mode enabled':'')+(h.adapter_supported?'':' · Adapter pending')));row.append(info);
    if(h.adapter_supported&&h.available){
      const bWrap=el('div');bWrap.style.display='flex';bWrap.style.gap='6px';bWrap.style.flexWrap='wrap';
      if(h.name==='agy'){
        const btnQuick=el('button','⚡ 1-Click Sign In','primary');btnQuick.type='button';
        btnQuick.style.background='#1a73e8';btnQuick.style.borderColor='#1a73e8';btnQuick.style.color='#fff';
        btnQuick.onclick=guard(async()=>{
          $('settings-dialog').close();
          await requireUpdatedEngine();
          const r=await api('/api/auth/google/accounts/quick_signin',{});
          notice('Initiated 1-Click sign-in for: '+(r.account?.email||r.account_id));
          if(window.setView)window.setView('auth');
          if(window.refreshAuthPool)await window.refreshAuthPool();
        });
        const btnPool=el('button','🔑 Manage Auth Pool','secondary');btnPool.type='button';
        btnPool.onclick=()=>{ $('settings-dialog').close(); if(window.setView)window.setView('auth'); };
        const btnAdd=el('button','+ Add account');btnAdd.type='button';
        btnAdd.onclick=()=>{ $('settings-dialog').close(); $('account-dialog').showModal(); };
        bWrap.append(btnQuick,btnPool,btnAdd);
        api('/health').then(h=>h.studio_revision?api('/api/auth/google/accounts'):{accounts:[]}).then(accData=>{
          const accounts=accData.accounts||[];
          const accList=el('div');accList.style.marginTop='6px';accList.style.fontSize='11px';
          if(accounts.length){
            accList.append(el('span',`Connected accounts (${accounts.length}): `,'muted'));
            accounts.forEach((a,idx)=>{
              if(idx>0)accList.append(document.createTextNode(', '));
              const badge=el('span',`${a.email} [${a.health_state}]`,a.health_state==='healthy'?'auth-badge badge-healthy':'auth-badge badge-blocked');
              badge.style.fontSize='9px';badge.style.cursor='pointer';badge.title='Click to view in Auth Pool';
              badge.onclick=()=>{ $('settings-dialog').close(); if(window.setView)window.setView('auth'); };
              accList.append(badge);
            });
          }else{
            accList.append(el('span','No Google accounts registered yet. Click "+ Add account" or "Manage Auth Pool".','muted'));
          }
          info.append(accList);
        }).catch(()=>{});
      }else{
        const b=el('button','Sign in with '+({claude:'Claude',codex:'OpenAI'}[h.name]||h.name)); b.type='button';b.addEventListener('click',guard(async()=>{const r=await api('/api/harness/login',{harness:h.name});notice(r.note||JSON.stringify(r));}));
        bWrap.append(b);
      }
      row.append(bWrap);
    }else if(h.name==='agy'){
      const link=el('a','Install Antigravity CLI');link.href='https://antigravity.google/docs/cli/install/';link.target='_blank';link.rel='noopener noreferrer';row.append(link);
    }
    $('harnesses').append(row);
  }
  $('harnesses').append(el('small','Legacy Gemini CLI, Hermes and OpenClaw adapters are not yet supported.'));
  $('models').replaceChildren();
  const seen=new Set();
  const addModel=(val)=>{if(val&&!seen.has(val)){seen.add(val);const option=el('option');option.value=val;$('models').append(option);}};
  for(const [alias,p] of Object.entries(s.providers)){
    for(const model of (p.models||[])){addModel(alias+'/'+model);}
  }
  for(const [alias,p] of Object.entries(providerPresets)){
    for(const model of (p.models||[])){addModel(alias+'/'+model);}
  }
  for(const h of data.harnesses){
    for(const model of h.model_examples||[]){addModel(model);}
  }
}
function choosePreset(){
  const id=$('provider-preset').value,p=providerPresets[id],form=$('settings-form');
  $('provider-note').textContent=p?.note||'Use the endpoint and model IDs supplied by your provider.';
  $('provider-key-link').hidden=!p?.key_url;
  if(p?.key_url)$('provider-key-link').href=p.key_url;
  if(!p)return;
  const saved=savedProviders[p.alias]||p;
  form.elements.alias.value=p.alias;form.elements.adapter.value=saved.adapter;
  form.elements.base_url.value=saved.base_url||'';form.elements.models.value=saved.models.join('\n');
  form.elements.api_key.value='';
}
$('provider-preset').addEventListener('change',choosePreset);
$('projects').addEventListener('change',guard(async()=>{currentProject=$('projects').value;localStorage.setItem('agentic_team_project',currentProject);active=null;$('session').hidden=true;document.body.classList.remove('inspecting');graphSignature='';flowHistory.length=0;await refreshTree();await syncTelemetry();}));
$('new-project').addEventListener('click',()=>$('project-dialog').showModal());
$('spawn').addEventListener('click',()=>$('worker-dialog').showModal());
$('settings').addEventListener('click',guard(async()=>{await settings();$('settings-dialog').showModal();}));
$('close-session').addEventListener('click',()=>{$('session').hidden=true;active=null;});
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',guard(async()=>{tab=b.dataset.tab;await refreshSession();})));
for(const [id,interrupt] of [['send',false],['interrupt',true]]){
  $(id).addEventListener('click',guard(async()=>{
    if(!active||!$('message').value.trim())return;
    await api('/api/chat',{project_name:currentProject,target_agent_id:active.id,content:$('message').value,is_interrupt:interrupt});
    $('message').value='';await refreshSession();
  }));
}
$('resume').addEventListener('click',guard(async()=>{await action('resume_agent',{target_agent_id:active.id});await refreshTree();}));
$('terminate').addEventListener('click',guard(async()=>{
  if(!active||!confirm('Stop this worker, archive its files, and delete its working folder?'))return;
  await action('terminate_worker',{worker_id:active.id,cleanup_folder:true});active=null;$('session').hidden=true;await refreshTree();
}));
$('project-form').addEventListener('submit',guard(async e=>{
  e.preventDefault();const f=new FormData(e.target),data=Object.fromEntries(f);
  delete data.provider;
  data.allow_commands=f.has('allow_commands');data.thinking_budget=Number(data.thinking_budget);
  data.reasoning_effort=data.reasoning_effort||null;
  data.read_roots=data.read_roots.split('\n').map(x=>x.trim()).filter(Boolean);
  await api('/api/projects',data);currentProject=data.name;localStorage.setItem('agentic_team_project',currentProject);
  $('project-dialog').close();await refreshProjects();await openSession(tree.ceo.id);
}));
$('worker-form').addEventListener('submit',guard(async e=>{
  e.preventDefault();await action('spawn_worker',Object.fromEntries([...new FormData(e.target)].filter(([key])=>key!=='provider')));
  $('worker-dialog').close();await refreshTree();
}));
$('settings-form').addEventListener('submit',guard(async e=>{
  e.preventDefault();const f=Object.fromEntries([...new FormData(e.target)].filter(([key])=>key!=='provider')),alias=f.alias.trim();
  if(!alias||alias.includes('/'))throw Error('Provider alias must be non-empty and contain no slash');
  const data={providers:{[alias]:{adapter:f.adapter,base_url:f.base_url||null,models:f.models.split('\n').map(x=>x.trim()).filter(Boolean)}}};
  if(f.api_key)data.api_keys={[alias]:f.api_key};
  await api('/api/settings',data);e.target.elements.api_key.value='';await settings();notice('Connection saved.');
}));
$('save-accounts').addEventListener('click',guard(async()=>{
  const accounts={claude:$('account-claude').checked,codex:$('account-codex').checked};
  if((await api('/health')).studio_revision)accounts.agy=$('account-agy').checked;
  await api('/api/settings',{cli_auth_enabled:accounts});
  await settings();notice('Account modes saved. Complete the provider sign-in before starting a task.');
}));
if($('btn-add-worker'))$('btn-add-worker').addEventListener('click',()=>$('worker-dialog').showModal());
if($('btn-clear-connections'))$('btn-clear-connections').addEventListener('click',guard(async()=>{
  if(window.resetLayout)await window.resetLayout();

}));
if($('quick-message-form'))$('quick-message-form').addEventListener('submit',guard(async e=>{
  e.preventDefault();
  if(!contextAgent||!$('quick-msg-text').value.trim())return;
  await api('/api/chat',{project_name:currentProject,target_agent_id:contextAgent.id,content:$('quick-msg-text').value.trim(),is_interrupt:$('quick-msg-interrupt').checked});
  $('quick-msg-text').value='';
  $('quick-message-dialog').close();
  notice('Message delivered to '+pretty(contextAgent.name));
  if(active?.id===contextAgent.id)await refreshSession();
}));
if($('edit-task-form'))$('edit-task-form').addEventListener('submit',guard(async e=>{
  e.preventDefault();
  if(!contextAgent)return;
  const newName=$('edit-agent-name').value.trim(), newTask=$('edit-agent-task').value.trim();
  await api('/api/agents/'+contextAgent.id+'/task',{name:newName,task_description:newTask});
  $('edit-task-dialog').close();
  notice('Updated '+pretty(newName));
  await refreshTree();
}));
if($('btn-quick-signin'))$('btn-quick-signin').addEventListener('click',guard(async()=>{
  await requireUpdatedEngine();
  const r=await api('/api/auth/google/accounts/quick_signin',{});
  notice('Initiated 1-Click sign-in for: '+(r.account?.email||r.account_id));
  if(window.refreshAuthPool)await window.refreshAuthPool();
}));
if($('btn-add-account'))$('btn-add-account').addEventListener('click',()=>$('account-dialog').showModal());
if($('btn-refresh-accounts'))$('btn-refresh-accounts').addEventListener('click',guard(async()=>{
  if(window.refreshAuthPool)await window.refreshAuthPool();
  notice('Refreshed auth pool.');
}));
if($('btn-import-default'))$('btn-import-default').addEventListener('click',guard(async()=>{
  const r=await api('/api/auth/google/accounts/import_default',{});
  notice('Imported default Google account: '+r.account.account_id);
  if(window.refreshAuthPool)await window.refreshAuthPool();
}));
if($('account-form'))$('account-form').addEventListener('submit',guard(async e=>{
  e.preventDefault();
  const f=new FormData(e.target);
  const email=f.get('email').trim(), account_id=f.get('account_id').trim()||undefined;
  const models=f.get('model_eligibility').split('\n').map(x=>x.trim()).filter(Boolean);
  const max_concurrent=Number(f.get('max_concurrent')||4);
  await api('/api/auth/google/accounts',{email,account_id,model_eligibility:models.length?models:undefined,max_concurrent});
  $('account-dialog').close();
  notice('Registered Google account: '+email);
  if(window.refreshAuthPool)await window.refreshAuthPool();
}));
if($('btn-apply-auth'))$('btn-apply-auth').addEventListener('click',guard(async()=>{
  if(!active)return;
  await requireUpdatedEngine();
  const val=$('session-auth-select').value;
  $('session-auth-select').dataset.agent='';
  await api('/api/agents/'+active.id+'/force_auth',{account_id:val==='auto'?null:val});
  notice('Updated auth binding for '+pretty(active.name));
  await refreshTree();
}));

window.addEventListener('DOMContentLoaded',async()=>{
  try{
    const hash=new URLSearchParams(location.hash.slice(1));
    if(hash.has('token')){
      const token=hash.get('token');history.replaceState(null,'',location.pathname);
      await api('/api/session',{token});
    }
    await refreshProjects();await settings();connect();
  }catch(err){notice(err.message);}
});

