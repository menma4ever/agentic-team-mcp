'use strict';
let engineRevision=0, runtimeTarget=null;
const harnessLabels={direct_api:'Direct API',antigravity:'Google account · Antigravity',claude_code:'Claude Code',codex:'Codex'};
async function requireUpdatedEngine(){
  const health=await api('/health');engineRevision=health.studio_revision||0;
  if(engineRevision<2)throw Error('This backend improvement is staged. The running engine is being preserved for your active workers; it becomes available after an idle restart.');
}
async function catalogForPicker(){
  const health=await api('/health');engineRevision=health.studio_revision||0;
  if(engineRevision>=2){
    const catalog=await api('/api/catalog'),connection=await api('/api/settings');
    for(const [id,label]of [['deepseek','DeepSeek API'],['openrouter','DeepSeek via OpenRouter'],['siliconflow','DeepSeek via SiliconFlow'],['together','DeepSeek via Together']]){
      let provider=catalog.providers.find(p=>p.id===id);
      if(!provider){provider={id,label,models:[]};catalog.providers.push(provider);}
      provider.label=label;
      if(typeof provider.setup_required!=='boolean')provider.setup_required=!(connection.settings.providers[id]&&connection.settings.api_keys[id]);
    }
    return catalog;
  }
  const data=await api('/api/settings'), providers=new Map();
  const add=(id,label,model,harness)=>{
    if(!providers.has(id))providers.set(id,{id,label,models:[]});
    const models=providers.get(id).models;
    if(model&&!models.some(m=>m.id===model))models.push({id:model,recommended_harness:harness,source:'Saved in this workspace'});
  };
  for(const [id,p]of Object.entries(data.settings.providers||{}))for(const m of p.models||[])add(id,id,id+'/'+m,id==='zai'?'claude_code':'direct_api');
  for(const [name,label,h]of [['agy','Google account','antigravity'],['codex','OpenAI account','codex'],['claude','Claude account','claude_code']])if(data.settings.cli_auth_enabled[name])add(name,label,null,h);
  for(const a of agentIndex.values()){
    const id=a.model.startsWith('zai/')?'zai':({antigravity:'agy',codex:'codex',claude_code:'claude'}[a.harness]||a.model.split('/')[0]);
    add(id,{agy:'Google account',codex:'OpenAI account',claude:'Claude account'}[id]||id,a.model,a.harness);
  }
  return {providers:[...providers.values()]};
}
async function installPicker(form){
  const previousPicker=form.querySelector('.provider-picker');
  if(previousPicker){const oldModel=form.elements.ceo_model||form.elements.model;previousPicker.before(oldModel.closest('label'),form.elements.harness.closest('label'));previousPicker.remove();}
  const model=form.elements.ceo_model||form.elements.model,harness=form.elements.harness;
  const modelLabelNode=model.closest('label'), harnessLabelNode=harness.closest('label');
  const box=el('div',undefined,'provider-picker'),label=el('label','1 · Provider'),select=el('select');select.name='provider';label.append(select);
  const modelLabelText=modelLabelNode.firstChild;if(modelLabelText?.nodeType===3)modelLabelText.textContent='2 · Model';
  const harnessText=harnessLabelNode.firstChild;if(harnessText?.nodeType===3)harnessText.textContent='3 · Harness';
  const datalist=el('datalist');datalist.id=form.id+'-models';model.setAttribute('list',datalist.id);
  const hint=el('small','Saved model IDs. Enter another exact ID if needed.','picker-note');
  const setup=el('button','Set up this provider in Connections','secondary');setup.type='button';setup.hidden=true;
  setup.onclick=guard(async()=>{const alias=select.value;form.closest('dialog').close();await settings();$('provider-preset').value=alias;choosePreset();$('settings-dialog').showModal();});
  modelLabelNode.before(box);box.append(label,modelLabelNode,harnessLabelNode,datalist,hint,setup);
  const data=await catalogForPicker();
  for(const p of data.providers){const o=el('option',p.label+(p.setup_required?' · setup required':''));o.value=p.id;select.append(o);}
  const custom=el('option','Custom / exact model ID');custom.value='custom';select.append(custom);
  const current=data.providers.find(p=>p.models.some(m=>m.id===model.value));if(current)select.value=current.id;
  function populateHarness(recommended,preserve=false){
    const previous=harness.value;harness.replaceChildren();
    const p=select.value;
    const partner=['deepseek','openrouter','siliconflow','together'].includes(p);
    const choices=partner?['direct_api']:p==='agy'?['antigravity','direct_api']:p==='codex'?['codex','direct_api']:p==='claude'?['claude_code','direct_api']:p==='zai'?['claude_code','direct_api']:['direct_api','claude_code','codex','antigravity'];
    choices.sort((a,b)=>(b===recommended)-(a===recommended));
    for(const h of choices){const label=partner&&h==='direct_api'?'Built-in team agent · '+({deepseek:'DeepSeek',openrouter:'OpenRouter',siliconflow:'SiliconFlow',together:'Together'}[p]):harnessLabels[h];const o=el('option',label+(h===recommended?' — recommended':''));o.value=h;harness.append(o);}
    harness.value=preserve&&choices.includes(previous)?previous:recommended;
  }
  function update(keep=false){
    const p=data.providers.find(p=>p.id===select.value);datalist.replaceChildren();
    for(const m of p?.models||[]){const o=el('option');o.value=m.id;o.label=m.source;datalist.append(o);}
    if(!keep)model.value=p?.models[0]?.id||'';
    hint.textContent=select.value==='deepseek'?'Uses your saved DeepSeek API connection with team tools, conversation history and usage reporting.':'Saved model IDs. Enter another exact ID if needed.';
    setup.hidden=!p?.setup_required;
    model.setCustomValidity(p?.setup_required?'Set up this provider in Connections first.':'');
    if(p?.setup_required)hint.textContent='No saved connection for this provider. Add its API key, endpoint and current model IDs in Connections first. Your Google login cannot authenticate this API.';
    model.placeholder=select.value==='agy'?'antigravity/<model ID>':'Exact model ID';
    const recommendation=p?.models.find(m=>m.id===model.value)?.recommended_harness||({agy:'antigravity',codex:'codex',claude:'claude_code',zai:'claude_code'}[select.value]||'direct_api');
    populateHarness(recommendation,keep);
  }
  select.onchange=()=>update();model.addEventListener('change',()=>{
    const match=data.providers.find(p=>p.id===select.value)?.models.find(m=>m.id===model.value);
    if(match)populateHarness(match.recommended_harness);
  });update(!!model.value);
}
async function showRuntime(a){
  runtimeTarget=a;const f=$('runtime-form');f.elements.model.value=a.model;f.elements.harness.value=a.harness;f.elements.mode.value='after_turn';
  // Refresh this picker for the selected agent instead of keeping another agent's provider.
  const existing=f.querySelector('.provider-picker');
  if(existing){existing.before(f.elements.model.closest('label'),f.elements.harness.closest('label'));existing.remove();}
  await installPicker(f);const staged=engineRevision<2;f.querySelector('[type=submit]').disabled=staged;
  $('runtime-note').textContent=staged?'Prepared for the next idle engine restart. The current agent keeps working.':'Agent: '+pretty(a.name)+' · '+(states[a.status]||a.status);
  $('runtime-dialog').showModal();
}
$('change-runtime').onclick=guard(()=>showRuntime(active));
$('open-console').onclick=guard(async()=>{await requireUpdatedEngine();await api('/api/agents/'+active.id+'/console',{});});
$('cancel-runtime').onclick=()=>$('runtime-dialog').close();
$('runtime-form').onsubmit=guard(async e=>{
  e.preventDefault();await requireUpdatedEngine();const f=e.target;
  await api('/api/action',{project_name:runtimeTarget.project_name,action:'reconfigure_agent',arguments:{target_agent_id:runtimeTarget.id,model:f.elements.model.value,harness:f.elements.harness.value,mode:f.elements.mode.value}});
  $('runtime-dialog').close();notice('Runtime change saved. Conversation and workspace are retained.');await refreshTree();
});
$('agent-context-menu').addEventListener('click',guard(async e=>{
  const kind=e.target.closest('[data-menu]')?.dataset.menu;if(!contextAgent)return;
  if(kind==='runtime')await showRuntime(contextAgent);
  if(kind==='console'){await requireUpdatedEngine();await api('/api/agents/'+contextAgent.id+'/console',{});}
}));
for(const id of ['project-dialog','worker-dialog'])new MutationObserver(()=>{
  if($(id).open)installPicker($(id).querySelector('form')).catch(e=>notice(e.message));
}).observe($(id),{attributes:true,attributeFilter:['open']});
// The previous daemon's account-list GET changes saved credentials. Avoid it during active work.
const previousAuthPool=renderAuthPool;
renderAuthPool=async function(){
  const health=await api('/health');
  if(!health.studio_revision){
    $('auth-health').textContent='Account management awaits an idle engine restart. Your running Google session is untouched.';
    for(const id of ['btn-quick-signin','btn-add-account','btn-import-default']){
      $(id).disabled=true;$(id).title='Available after the backend update is activated';
    }
    const rows=$('auth-rows');rows.replaceChildren();
    const tr=el('tr'),td=el('td','The dashboard is connected to an older engine. Saved accounts remain on disk; account controls require the updated engine.','empty-state');td.colSpan=6;tr.append(td);rows.append(tr);
    return;
  }
  for(const id of ['btn-quick-signin','btn-add-account','btn-import-default']){$(id).disabled=false;$(id).title='';}
  return previousAuthPool();
};
window.refreshAuthPool=renderAuthPool;
async function chooseConnection(source){
  await requireUpdatedEngine();
  const dialog=el('dialog'),form=el('form'),title=el('h2','Connect '+pretty(source.name));
  const label=el('label','Collaborate with'),select=el('select');
  for(const a of agents().filter(a=>a.id!==source.id)){
    const option=el('option',pretty(a.name)+' · '+a.role);option.value=a.id;select.append(option);
  }
  label.append(select);const note=el('p','Mark a collaboration between these agents. Agents in this project can message each other without a link; reporting relationships stay unchanged.');
  const actions=el('div',undefined,'buttons'),cancel=el('button','Cancel'),submit=el('button','Connect','primary');
  cancel.type='button';submit.type='submit';actions.append(cancel,submit);form.append(title,label,note,actions);dialog.append(form);document.body.append(dialog);
  cancel.onclick=()=>dialog.close();dialog.onclose=()=>dialog.remove();
  form.onsubmit=guard(async e=>{e.preventDefault();await api('/api/agents/connect',{project_name:source.project_name,source_id:source.id,target_id:select.value});dialog.close();await refreshTree();});
  dialog.showModal();
}

$('cancel-google-login').onclick=guard(async()=>{await api('/api/auth/google/cancel_login',{});await renderAuthPool();});

api('/health').then(health=>{
 engineRevision=health.studio_revision||0;
 window.engineRevision=engineRevision;
 if(engineRevision<2){
  $('open-console').disabled=true;$('open-console').textContent='PowerShell · update pending';
  const note=el('div','Backend update prepared. Current agents continue on the existing engine.','upgrade-note');
  document.querySelector('.app-header').after(note);
 }
}).catch(()=>{});

const toggleHandoffsBtn = $('toggle-handoffs');
if(toggleHandoffsBtn){
  const isHidden = localStorage.getItem('agentic_team_handoffs_collapsed') === 'true';
  $('flow-feed').hidden = isHidden;
  toggleHandoffsBtn.textContent = isHidden ? 'Expand ↗' : 'Collapse ▾';
  toggleHandoffsBtn.onclick = () => {
    const nextHidden = !$('flow-feed').hidden;
    $('flow-feed').hidden = nextHidden;
    toggleHandoffsBtn.textContent = nextHidden ? 'Expand ↗' : 'Collapse ▾';
    localStorage.setItem('agentic_team_handoffs_collapsed', nextHidden ? 'true' : 'false');
  };
}
