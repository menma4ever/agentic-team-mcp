
'use strict';
const $ = id => document.getElementById(id);
let currentProject = null, tree = null, active = null, tab = 'chat', socket;
let refreshTimer, sessionTimer;
let providerPresets = {}, savedProviders = {};
const states = {working:'Working',queued:'Queued',idle:'Resting',failed:'Failed',paused:'Paused',blocked_loop:'Needs guidance',terminated:'Stopped'};
function el(tag,text,cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
let noticeFadeTimer = null, noticeRemoveTimer = null;
function notice(text, kind) {
  const box = $('notice');
  if (noticeFadeTimer) { clearTimeout(noticeFadeTimer); noticeFadeTimer = null; }
  if (noticeRemoveTimer) { clearTimeout(noticeRemoveTimer); noticeRemoveTimer = null; }
  if (box) {
    box.replaceChildren();
    box.className = '';
    if (text) {
      const lower = String(text).toLowerCase();
      const isBad = kind === 'error' || (kind !== 'success' && /\b(error|failed|fail|denied|blocked|exhausted|timeout|timed out|unavailable|not found|invalid|cannot|could not|exception|stopped|cancelled)\b/.test(lower));
      const variant = isBad ? 'notice-error' : 'notice-success';
      box.classList.add('notice-toast', variant);
      const dot = el('span', isBad ? '✕' : '✓', 'notice-icon');
      const span = el('span', text, 'notice-text');
      const closeBtn = el('button', '✕', 'notice-close-x');
      closeBtn.type = 'button';
      closeBtn.title = 'Dismiss notification';
      closeBtn.setAttribute('aria-label', 'Dismiss notification');
      closeBtn.onclick = () => {
        if (noticeFadeTimer) clearTimeout(noticeFadeTimer);
        if (noticeRemoveTimer) clearTimeout(noticeRemoveTimer);
        box.classList.add('notice-fading');
        noticeRemoveTimer = setTimeout(() => { box.replaceChildren(); box.className = ''; }, 350);
      };
      box.append(dot, span, closeBtn);
      noticeFadeTimer = setTimeout(() => {
        box.classList.add('notice-fading');
        noticeRemoveTimer = setTimeout(() => {
          box.replaceChildren();
          box.className = '';
        }, 700);
      }, 3800);
    }
  }
  if ($('settings-notice')) $('settings-notice').textContent = text || '';
}
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
        const btnQuick=el('button','+ Add account','primary');btnQuick.type='button';
        btnQuick.onclick=guard(async()=>{
          $('settings-dialog').close();
          await requireUpdatedEngine();
          const r=await api('/api/auth/google/accounts/quick_signin',{});
          notice('Initiated sign-in for: '+(r.account?.email||r.account_id));
          if(window.setView)window.setView('auth');
          if(window.refreshAuthPool)await window.refreshAuthPool();
        });
        bWrap.append(btnQuick);
        api('/health').then(h=>h.studio_revision?api('/api/auth/google/accounts'):{accounts:[]}).then(accData=>{
          const accounts=accData.accounts||[];
          const accList=el('div');accList.style.marginTop='8px';accList.style.fontSize='11px';
          if(accounts.length){
            const hdr=el('div',`Connected accounts (${accounts.length}):`,'muted');
            hdr.style.marginBottom='6px';
            accList.append(hdr);
            const col=el('div');
            col.style.display='flex';col.style.flexDirection='column';col.style.gap='4px';col.style.alignItems='flex-start';
            accounts.forEach(a=>{
              const badge=el('span',`${a.email} [${a.health_state}]`,a.health_state==='healthy'?'auth-badge badge-healthy':'auth-badge badge-blocked');
              badge.style.fontSize='10px';badge.style.cursor='pointer';badge.title='Click to view in Auth Pool';
              badge.onclick=()=>{ $('settings-dialog').close(); if(window.setView)window.setView('auth'); };
              col.append(badge);
            });
            accList.append(col);
          }else{
            accList.append(el('span','No Google accounts registered yet.','muted'));
          }
          info.append(accList);
        }).catch(()=>{});
      }else{
        const isAuthed=h.auth_status?.logged_in;
        if(h.auth_status){
          const badge=el('span',isAuthed?('✓ '+(h.auth_status.status_text||'Logged in')):(h.auth_status.status_text||'Not logged in'),isAuthed?'auth-badge badge-healthy':'auth-badge badge-blocked');
          badge.style.fontSize='10px';badge.style.display='inline-block';badge.style.marginTop='4px';
          info.append(document.createElement('br'),badge);
        }
        const bText=(isAuthed?'Re-authenticate ':'Sign in · ')+({claude:'Claude',codex:'OpenAI'}[h.name]||h.name);
        const b=el('button',bText); b.type='button';
        b.addEventListener('click',guard(async()=>{
          const r=await api('/api/harness/login',{harness:h.name});
          notice(r.note||JSON.stringify(r));
          let attempts=0;
          const poll=setInterval(async()=>{
            attempts++;
            if(attempts>24 || !$('settings-dialog').open){ clearInterval(poll); return; }
            await settings();
          }, 2500);
        }));
        bWrap.append(b);
      }
      row.append(bWrap);
    }else if(h.name==='agy'){
      const link=el('a','Install Antigravity CLI');link.href='https://antigravity.google/docs/cli/install/';link.target='_blank';link.rel='noopener noreferrer';row.append(link);
    }
    $('harnesses').append(row);
  }
  const activeKeys=Object.entries(s.api_keys).filter(([,v])=>v).map(([k])=>k);
  const cpWrap=$('configured-providers');
  if(cpWrap){
    cpWrap.replaceChildren();
    cpWrap.style.marginTop='10px';
    const cpHdr=el('div',`Connected API Providers (${activeKeys.length}):`,'muted');
    cpHdr.style.fontSize='11px';cpHdr.style.marginBottom='6px';
    cpWrap.append(cpHdr);
    if(activeKeys.length){
      const cpCol=el('div');
      cpCol.style.display='flex';cpCol.style.flexDirection='column';cpCol.style.gap='4px';cpCol.style.alignItems='flex-start';
      activeKeys.forEach(k=>{
        const pObj=s.providers?.[k]||providerPresets?.[k]||{};
        const mList=(pObj.models||[]).slice(0,3).join(', ');
        const b=el('span',`${k} [connected]${mList?' · '+mList:''}`,'auth-badge badge-healthy');
        b.style.fontSize='10px';
        cpCol.append(b);
      });
      cpWrap.append(cpCol);
    }else{
      cpWrap.append(el('small','No API provider keys configured yet.','muted'));
    }
  }
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
  await refreshTelegramStatus();
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
  api('/api/providers/' + encodeURIComponent(p.alias) + '/models?force=true').then(live => {
    if (live && Array.isArray(live.models) && live.models.length) {
      form.elements.models.value = live.models.join('\n');
      if (savedProviders[p.alias]) savedProviders[p.alias].models = live.models;
    }
  }).catch(() => {});
}
$('provider-preset').addEventListener('change',choosePreset);
$('projects').addEventListener('change',guard(async()=>{currentProject=$('projects').value;localStorage.setItem('agentic_team_project',currentProject);active=null;$('session').hidden=true;document.body.classList.remove('inspecting');graphSignature='';flowHistory.length=0;await refreshTree();await syncTelemetry();}));
$('new-project').addEventListener('click',()=>$('project-dialog').showModal());
async function confirmAndDeleteCurrentProject() {
  if (!currentProject) return;
  if (!confirm(`Delete project "${currentProject}" permanently and stop all of its agents?`)) return;
  await api('/api/projects?name=' + encodeURIComponent(currentProject), undefined, 'DELETE');
  localStorage.removeItem('agentic_team_project');
  currentProject = null;
  active = null;
  $('session').hidden = true;
  document.body.classList.remove('inspecting');
  notice('Project deleted.');
  await refreshProjects();
}

function openProjectContextMenu(e) {
  if (!currentProject) return;
  e.preventDefault();
  e.stopPropagation();
  const menu = $('project-context-menu');
  if (!menu) return;
  if ($('agent-context-menu')) $('agent-context-menu').hidden = true;
  if ($('ctx-project-name')) $('ctx-project-name').textContent = currentProject;
  menu.hidden = false;
  const x = Math.min(e.clientX, window.innerWidth - 200);
  const y = Math.min(e.clientY, window.innerHeight - 90);
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
}

['project-switch-bar', 'projects', 'project-title', 'rail-project'].forEach(id => {
  const elem = $(id);
  if (elem) elem.addEventListener('contextmenu', openProjectContextMenu);
});
document.querySelectorAll('.rail-project').forEach(elem => {
  elem.addEventListener('contextmenu', openProjectContextMenu);
});
if ($('ctx-delete-project')) {
  $('ctx-delete-project').addEventListener('click', guard(async () => {
    if ($('project-context-menu')) $('project-context-menu').hidden = true;
    await confirmAndDeleteCurrentProject();
  }));
}
document.addEventListener('click', (e) => {
  const menu = $('project-context-menu');
  if (menu && !menu.hidden && !menu.contains(e.target)) {
    menu.hidden = true;
  }
});
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

let telegramPollTimer = null;

async function refreshTelegramStatus() {
  if (!$('telegram-conn-badge')) return;
  try {
    const data = await api('/api/telegram/status');
    const badge = $('telegram-conn-badge');
    const liaison = $('telegram-liaison-status');
    const botHandle = $('telegram-bot-handle');
    const pairedUser = $('telegram-paired-user');
    const bridgePid = $('telegram-bridge-pid');
    const pairingBox = $('telegram-pairing-box');
    const pairCode = $('telegram-pair-code');
    const deepLink = $('telegram-deep-link');
    const tokenInput = $('telegram-bot-token');

    if (data.has_token && data.masked_token) {
      tokenInput.placeholder = `Saved: ${data.masked_token} (leave blank to keep)`;
    } else {
      tokenInput.placeholder = 'Enter Bot Token (e.g. 123456789:ABCDef...)';
    }

    botHandle.textContent = data.bot_handle || '—';
    pairedUser.textContent = data.paired_user_handle || '—';
    bridgePid.textContent = data.bridge_pid ? `PID ${data.bridge_pid}` : '—';
    liaison.textContent = data.liaison_status || 'INACTIVE';
    liaison.style.color = data.liaison_status === 'ACTIVE' ? '#6ee787' : '#9aabb5';

    badge.className = 'auth-badge ' + (
      data.connection_state === 'connected' ? 'badge-healthy' :
      data.connection_state === 'pairing' ? 'badge-rate-limited' : 'badge-blocked'
    );
    badge.textContent = data.connection_state === 'connected' ? 'Connected ✅' :
                       data.connection_state === 'pairing' ? 'Pairing ⏳' : 'Disconnected';

    if (data.pairing_code && data.connection_state === 'pairing') {
      pairingBox.style.display = 'block';
      pairCode.textContent = `/pair ${data.pairing_code}`;
      if (data.deep_link) {
        deepLink.href = data.deep_link;
        deepLink.style.display = 'inline-block';
      } else {
        deepLink.style.display = 'none';
      }
    } else {
      pairingBox.style.display = 'none';
    }

    return data;
  } catch (err) {
    if ($('telegram-conn-badge')) {
      $('telegram-conn-badge').textContent = 'Error';
      $('telegram-conn-badge').className = 'auth-badge badge-blocked';
    }
  }
}

function startTelegramPolling() {
  if (telegramPollTimer) clearInterval(telegramPollTimer);
  telegramPollTimer = setInterval(async () => {
    if (!$('settings-dialog') || !$('settings-dialog').open) {
      clearInterval(telegramPollTimer);
      telegramPollTimer = null;
      return;
    }
    const data = await refreshTelegramStatus();
    if (data && data.connection_state === 'connected') {
      clearInterval(telegramPollTimer);
      telegramPollTimer = null;
      notice('Telegram Bridge paired successfully! ✅');
    }
  }, 2000);
}

function tokenInputSaved() {
  return $('telegram-bot-token') && $('telegram-bot-token').placeholder.includes('Saved:');
}

if ($('btn-telegram-connect')) {
  $('btn-telegram-connect').addEventListener('click', guard(async () => {
    const token = $('telegram-bot-token').value.trim();
    if (!token && (!tokenInputSaved() || $('telegram-bot-token').placeholder.includes('Enter Bot Token'))) {
      notice('Please enter a Telegram Bot Token.');
      return;
    }
    notice('Connecting Telegram Human Bridge...');
    await api('/api/telegram/connect', { bot_token: token || 'KEEP_EXISTING' });
    $('telegram-bot-token').value = '';
    await refreshTelegramStatus();
    notice('Bridge process launched. Waiting for one-time pairing code...');
    startTelegramPolling();
  }));
}

if ($('btn-telegram-disconnect')) {
  $('btn-telegram-disconnect').addEventListener('click', guard(async () => {
    notice('Disconnecting Telegram Bridge...');
    await api('/api/telegram/disconnect', {});
    if (telegramPollTimer) { clearInterval(telegramPollTimer); telegramPollTimer = null; }
    await refreshTelegramStatus();
    notice('Telegram Bridge disconnected and session revoked.');
  }));
}

if ($('btn-telegram-test')) {
  $('btn-telegram-test').addEventListener('click', guard(async () => {
    notice('Sending test ping to paired Telegram owner...');
    const res = await api('/api/telegram/test', {});
    notice(res.detail || 'Test ping delivered to Telegram owner! ✅');
  }));
}

if ($('btn-telegram-restart')) {
  $('btn-telegram-restart').addEventListener('click', guard(async () => {
    notice('Restarting Telegram Bridge daemon...');
    await api('/api/telegram/restart', {});
    await refreshTelegramStatus();
    notice('Telegram Bridge restarted.');
  }));
}

if ($('btn-refresh-storage')) {
  $('btn-refresh-storage').addEventListener('click', guard(async () => {
    if (window.renderStorage) await window.renderStorage();
    notice('Refreshed storage cleanup audit.');
  }));
}

if ($('btn-storage-clean-safe')) {
  $('btn-storage-clean-safe').addEventListener('click', guard(async () => {
    const res = await api('/api/storage/action', { action: 'clean_safe' });
    if ($('storage-action-status')) $('storage-action-status').textContent = '⚠️ ' + res.message;
    notice(res.message);
  }));
}

if ($('btn-storage-review-list')) {
  $('btn-storage-review-list').addEventListener('click', () => {
    if ($('storage-review-dialog')) $('storage-review-dialog').showModal();
  });
}

if ($('btn-storage-move-cold')) {
  $('btn-storage-move-cold').addEventListener('click', guard(async () => {
    const res = await api('/api/storage/action', { action: 'move_cold' });
    if ($('storage-action-status')) $('storage-action-status').textContent = '⚠️ ' + res.message;
    notice(res.message);
  }));
}

if ($('btn-storage-cancel')) {
  $('btn-storage-cancel').addEventListener('click', guard(async () => {
    const res = await api('/api/storage/action', { action: 'cancel' });
    if ($('storage-action-status')) $('storage-action-status').textContent = 'Phase 1: Gated by Human Owner approval (audit only)';
    notice(res.message);
  }));
}

window.addEventListener('DOMContentLoaded',async()=>{
  try{
    const hash=new URLSearchParams(location.hash.slice(1));
    if(hash.has('token')){
      const token=hash.get('token');history.replaceState(null,'',location.pathname);
      await api('/api/session',{token});
    }
    if(hash.has('project')){
      localStorage.setItem('agentic_team_project', hash.get('project'));
    }
    await refreshProjects();await settings();connect();
    if(hash.has('open_ceo') && tree?.ceo?.id){
      await openSession(tree.ceo.id);
    }
  }catch(err){notice(err.message);}
});
