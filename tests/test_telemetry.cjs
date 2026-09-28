const {test}=require('node:test');
const assert=require('node:assert/strict');
const {Ledger,normalize}=require('../web/static/telemetry.js');
const stamp='2026-09-12T12:00:00Z';
function event(id,data,run='r'){return {id,run_id:run,timestamp:stamp,type:'output',data:{stream:'stdout',text:JSON.stringify(data)+'\n'}};}
test('OpenAI cached input is a subset of input',()=>{
 assert.deepEqual(normalize({prompt_tokens:100,completion_tokens:20,prompt_tokens_details:{cached_tokens:80}},'openai'),{input:100,output:20,cache:80,write:null});
});
test('Anthropic input includes reported cache read and creation exactly once',()=>{
 assert.deepEqual(normalize({input_tokens:10,output_tokens:20,cache_read_input_tokens:80,cache_creation_input_tokens:5},'anthropic'),{input:95,output:20,cache:80,write:5});
});
test('zero placeholders and unknown cache are distinct from measured zero',()=>{
 assert.equal(normalize({input_tokens:0,output_tokens:0},'anthropic'),null);
 assert.equal(normalize({input_tokens:10,output_tokens:5},'openai').cache,null);
 assert.equal(normalize({input_tokens:10,output_tokens:5,cached_input_tokens:0},'openai').cache,0);
});
test('split NDJSON and replayed event IDs do not duplicate usage',()=>{
 const l=new Ledger(),s=JSON.stringify({type:'turn.completed',usage:{input_tokens:100,output_tokens:20}})+'\n';
 const first={id:1,run_id:'r',timestamp:stamp,type:'output',data:{stream:'stdout',text:s.slice(0,30)}};
 l.ingest([first,first,{...first,id:2,data:{stream:'stdout',text:s.slice(30)}}]);assert.equal(l.totals().input,100);
});
test('stderr and stdout fragments cannot corrupt each other',()=>{
 const l=new Ledger();l.ingest([{...event(1,{}),data:{stream:'stdout',text:'{"type":"turn.completed",'}},{...event(2,{}),data:{stream:'stderr',text:'warning\n'}},{...event(3,{}),data:{stream:'stdout',text:'"usage":{"input_tokens":4,"output_tokens":2}}\n'}}]);assert.equal(l.totals().input,4);
});
test('Claude final usage replaces partial message usage within its run',()=>{
 const l=new Ledger();l.ingest([event(1,{type:'assistant',message:{id:'m',content:[],usage:{input_tokens:100,output_tokens:10}}}),event(2,{type:'assistant',message:{id:'m',content:[],usage:{input_tokens:100,output_tokens:20}}}),event(3,{type:'result',usage:{input_tokens:120,output_tokens:30}})]);
 assert.equal(l.totals().input,120);assert.equal(l.totals().output,30);
});
test('Gemini cumulative results across resumed runs count only differences',()=>{
 const l=new Ledger();
 l.ingest([event(1,{event:'step_update',step_update:{conversation_id:'c',step_index:1,usage:{input_tokens:10,output_tokens:5,cache_read_tokens:20}}}),event(2,{event:'result',result:{conversation_id:'c',usage:{input_tokens:10,output_tokens:5,cache_read_tokens:20}}}),event(3,{event:'step_update',step_update:{conversation_id:'c',step_index:2,usage:{input_tokens:4,output_tokens:2,cache_read_tokens:30}}},'r2'),event(4,{event:'result',result:{conversation_id:'c',usage:{input_tokens:14,output_tokens:7,cache_read_tokens:50}}},'r2')]);
 assert.equal(l.totals().input,64);assert.equal(l.totals().output,7);assert.equal(l.totals().cache,50);assert.equal(l.samples.size,2);
});
test('Gemini replay of already summarized step cannot inflate usage',()=>{
 const l=new Ledger(),step={event:'step_update',step_update:{conversation_id:'c',step_index:1,usage:{input_tokens:10,output_tokens:5}}};l.ingest([event(1,step),event(2,{event:'result',result:{conversation_id:'c',usage:{input_tokens:10,output_tokens:5}}}),event(3,step,'r2')]);assert.equal(l.totals().input,10);
});
test('estimated counts stay separate and disappear after final reported usage',()=>{
 const l=new Ledger();l.ingest([event(1,{type:'system',estimated_tokens:300})]);assert.equal(l.totals().reported,false);assert.equal(l.totals().estimated,300);
 l.ingest([event(2,{type:'result',usage:{input_tokens:50,output_tokens:40}})]);assert.equal(l.totals().estimated,0);assert.equal(l.totals().output,40);
});
test('separate model turns add, and trailing line is flushed on exit',()=>{
 const l=new Ledger();l.ingest([event(1,{type:'turn.completed',usage:{input_tokens:100,output_tokens:20}}),{...event(2,{}),data:{stream:'stdout',text:JSON.stringify({type:'turn.completed',usage:{input_tokens:200,output_tokens:30}})}},{id:3,type:'process_exit',data:{exit_code:0},run_id:'r',timestamp:stamp}]);assert.equal(l.totals().input,300);assert.equal(l.totals().output,50);
});
test('terminal preserves raw data but activity formats tool calls and results',()=>{
 const l=new Ledger();l.ingest([event(1,{type:'assistant',message:{id:'x',content:[{type:'tool_use',id:'t',name:'read_file',input:{path:'a.md'}}]}}),event(2,{type:'user',message:{content:[{type:'tool_result',tool_use_id:'t',content:'done'}]}})]);assert.equal(l.activity.length,2);assert.match(l.activity[0].text,/read_file/);assert.equal(l.activity[1].text,'done');assert.equal(l.raw.length,2);
});


test('model changes keep historical usage attributed to the original model',()=>{
 const ledger=new Ledger();
 ledger.ingest([
  {id:101,type:'model_response',run_id:'old',model:'zai/glm-test',timestamp:new Date().toISOString(),data:{usage:{input_tokens:100,output_tokens:10}}},
  {id:102,type:'model_response',run_id:'new',model:'openai/test',timestamp:new Date().toISOString(),data:{usage:{input_tokens:200,output_tokens:20}}}
 ]);
 assert.equal(ledger.totals(Infinity,'zai/glm-test').input,100);
 assert.equal(ledger.totals(Infinity,'openai/test').input,200);
 assert.equal(ledger.totals().input,300);
});
