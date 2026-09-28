/* Replay the durable event log. Never infer provider usage from text length. */
(function(root){
'use strict';
const number=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0?v:null;
function normalize(u,kind){
  if(!u||typeof u!=='object')return null;
  let input=number(u.input_tokens??u.prompt_tokens),output=number(u.output_tokens??u.completion_tokens);
  const cache=number(u.cache_read_input_tokens??u.cache_read_tokens??u.cached_input_tokens??u.prompt_tokens_details?.cached_tokens??u.input_tokens_details?.cached_tokens);
  const write=number(u.cache_creation_input_tokens??u.cache_write_tokens);
  if(input===null&&output===null)return null;
  if(kind==='anthropic'||kind==='gemini')input=input===null?null:input+(cache||0)+(write||0);
  // Some compatibility CLIs emit placeholder zeroes on every message.
  if(!(input||output||cache||write))return null;
  return {input,output,cache,write};
}
const canonModel=m=>m?String(m).trim().toLowerCase().replace(/\s+/g,'-').split('/').pop():null;
class Ledger{
 constructor(){this.cursor=-1;this.buffers=new Map();this.samples=new Map();this.activity=[];this.seen=new Set();this.estimated=new Map();this.stepText=new Map();this.raw=[];this.lastTime=null;this.geminiTotals=new Map();this.geminiFinalSteps=new Set();this.geminiSteps=new Map();this.codexTotals=new Map();this.activeThreadId=null;}
 addActivity(type,text,e,key){
  if(!text)return;
  if(key){const old=this.activity.find(x=>x.key===key);if(old){old.text=text;old.time=e.timestamp;old.run=e.run_id||'legacy';return;}}
  this.activity.push({type,text:String(text).slice(-24000),time:e.timestamp,id:e.id,key,run:e.run_id||'legacy'});
  if(this.activity.length>400)this.activity.splice(0,this.activity.length-400);
 }
 usage(key,u,kind,e,summary=false){
  const v=normalize(u,kind);if(!v)return;
  const run=e.run_id||'legacy';
  if(summary){for(const [k,s] of this.samples)if(s.run===run)this.samples.delete(k);}
  if(!summary&&this.samples.has(run+':summary'))return;
  this.samples.set(summary?run+':summary':run+':'+key,{...v,time:e.timestamp,run,model:e.model||null});
 }
 structured(d,e){
  const run=e.run_id||'legacy',type=d.type||d.event;
  if(type==='step_update'){
   const s=d.step_update||{},conversation=s.conversation_id||run,key='gemini:'+conversation+':step:'+s.step_index;
   const txt=(this.stepText.get(key)||'')+(s.text_delta||'');this.stepText.set(key,txt);
   if(txt)this.addActivity(s.step_type||'response',txt,e,key);
   const u=normalize(s.usage,'gemini');
   if(u&&!this.geminiFinalSteps.has(key)){this.samples.set(key,{...u,time:e.timestamp,run,model:e.model||null});this.geminiSteps.set(key,conversation);}
   return;
  }
  if(type==='assistant'){
   const m=d.message||{},key=m.id||d.uuid||e.id;
   this.usage('message:'+key,m.usage,'anthropic',e);
   for(const [i,c] of (Array.isArray(m.content)?m.content:[]).entries()){
    if(c.type==='text')this.addActivity('response',c.text,e,run+':'+key+':'+i);
    if(c.type==='tool_use')this.addActivity('tool',c.name+'\n'+JSON.stringify(c.input,null,2),e,c.id);
   }
  }else if(type==='user'){
   for(const c of (Array.isArray(d.message?.content)?d.message.content:[]))if(c.type==='tool_result')this.addActivity(c.is_error?'error':'tool result',typeof c.content==='string'?c.content:JSON.stringify(c.content,null,2),e,c.tool_use_id+':result');
  }else if(type==='item.started'||type==='item.completed'||type==='item.updated'){
   const i=d.item||{};this.addActivity(i.type||'activity',i.text||[i.command,i.aggregated_output].filter(Boolean).join('\n'),e,run+':'+i.id);
  }else if(type==='thread.started'){
   if(d.thread_id)this.activeThreadId=d.thread_id;
  }else if(type==='turn.completed'){
   // Codex CLI turn.completed reports cumulative thread token usage across resumed turns.
   const u=normalize(d.usage,'openai');
   if(u){
    const threadKey=this.activeThreadId||'codex:'+canonModel(e.model||'default');
    const prev=this.codexTotals.get(threadKey);
    const delta={};
    if(prev&&(u.input||0)>=(prev.input||0)&&(u.output||0)>=(prev.output||0)){
     for(const k of ['input','output','cache','write'])delta[k]=u[k]===null?null:Math.max(0,u[k]-(prev[k]||0));
    }else{
     for(const k of ['input','output','cache','write'])delta[k]=u[k];
    }
    this.codexTotals.set(threadKey,u);
    this.samples.set('codex:'+threadKey+':turn:'+e.id,{...delta,time:e.timestamp,run,model:e.model||null});
   }
  }else if(type==='result'){
   if(d.usage)this.usage('summary',d.usage,'anthropic',e,true);
   const p=d.result;
   if(p&&typeof p==='object'){
    // Antigravity result usage is cumulative across resumed conversation turns.
    // Replace provisional steps with the delta from the preceding checkpoint.
    const conversation=p.conversation_id||run,u=normalize(p.usage,'gemini');
    if(u){
     const previous=this.geminiTotals.get(conversation);
     for(const [key,conv] of this.geminiSteps)if(conv===conversation){this.samples.delete(key);this.geminiFinalSteps.add(key);this.geminiSteps.delete(key);}
     const delta={};for(const k of ['input','output','cache','write'])delta[k]=u[k]===null?null:Math.max(0,u[k]-(previous?.[k]||0));
     this.samples.set('gemini:'+conversation+':checkpoint:'+e.id,{...delta,time:e.timestamp,run,model:e.model||null});this.geminiTotals.set(conversation,u);
    }
    if(p.response)this.addActivity('response',p.response,e,run+':final');
   }else if(typeof p==='string')this.addActivity(d.is_error?'error':'response',p,e,run+':final');
  }else if(type==='tool_progress')this.addActivity('tool running',(d.tool_name||'Tool')+' · '+Math.round(d.elapsed_time_seconds||0)+'s',e,d.tool_use_id+':progress');
  else if(type==='system'&&number(d.estimated_tokens)!==null)this.estimated.set(run,d.estimated_tokens);
  else if(type==='error'||type==='turn.failed')this.addActivity('error',JSON.stringify(d.error||d),e);
 }
 ingest(events){
  for(const e of events){
   if(this.seen.has(e.id))continue;this.seen.add(e.id);this.cursor=Math.max(this.cursor,e.id);this.lastTime=e.timestamp;
   const d=e.data||{};
   if(e.type==='output'){
    this.raw.push({time:e.timestamp,text:d.text||'',stream:d.stream});if(this.raw.length>180)this.raw.shift();
    const key=(e.run_id||'legacy')+':'+d.stream;
    let buf=(this.buffers.get(key)||'')+(d.text||'');let idx;
    while((idx=buf.indexOf('\n'))>=0){const line=buf.slice(0,idx);buf=buf.slice(idx+1);try{this.structured(JSON.parse(line),e);}catch{if(line.trim())this.addActivity(d.stream||'output',line,e);}}
    this.buffers.set(key,buf);
   }else if(e.type==='model_response'){
    this.usage('event:'+e.id,d.usage,'openai',e);this.addActivity('response',d.text,e);
   }else{
    this.addActivity(e.type,d.text||JSON.stringify(d,null,2),e);
    if(e.type==='process_exit')for(const [k,v] of this.buffers)if(k.startsWith((e.run_id||'legacy')+':')&&v){try{this.structured(JSON.parse(v),e);}catch{this.addActivity('output',v,e);}this.buffers.delete(k);}
   }
  }
 }
 totals(timeWindowMs=Infinity,model=null,fallback=null){
  const now = Date.now();
  const targetCanon = model ? canonModel(model) : null;
  const result={input:0,output:0,cache:0,write:0,reported:false,cacheKnown:false,writeKnown:false,estimated:0};
  for(const s of this.samples.values()){
   if(targetCanon && canonModel(s.model||fallback)!==targetCanon)continue;
   if(timeWindowMs!==Infinity && s.time && (now - Date.parse(s.time)) > timeWindowMs) continue;
   result.reported=true;
   for(const k of ['input','output','cache','write'])result[k]+=s[k]||0;
   if(s.cache!==null)result.cacheKnown=true;
   if(s.write!==null)result.writeKnown=true;
  }
  if(!result.reported && timeWindowMs===Infinity){
   result.estimated=this.samples.size?0:([...this.estimated.values()].at(-1)||0);
  }
  return result;
 }
}
root.TeamTelemetry={Ledger,normalize};
if(typeof module!=='undefined')module.exports=root.TeamTelemetry;
})(typeof window==='undefined'?globalThis:window);
