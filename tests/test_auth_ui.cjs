const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/static/studio.js'),'utf8');
const render=source.slice(source.indexOf('async function renderAuthPool(){'),source.indexOf('window.refreshAuthPool=renderAuthPool;'));
function fixture(){
  const nodes={},requests=[];
  function el(tag,text,cls){return {tag,text,cls,children:[],append(...children){this.children.push(...children)},replaceChildren(...children){this.children=children}};}
  const accounts=Array.from({length:4},(_,i)=>({account_id:'profile_'+i,email:'profile'+i+'@example.invalid',health_state:'healthy',credential_saved:true}));
  const context={el,$:id=>nodes[id]??=el('div'),metric:()=>el('div'),guard:fn=>fn,notice:()=>{},confirm:()=>false,
    api:async(url,body)=>{requests.push({url,body});return url==='/api/auth/google/accounts'?{accounts}:{};}};
  vm.createContext(context);vm.runInContext(render,context);
  return {context,nodes,requests,accounts};
}
test('live account table renders all account names and saved logins',async()=>{
  const f=fixture();await f.context.renderAuthPool();
  const rows=f.nodes['auth-rows'].children;
  assert.equal(rows.length,4);
  rows.forEach((r,i)=>{assert.equal(r.children[0].children[0].text,f.accounts[i].email);assert.match(r.children[0].children[1].text,/Saved login/);assert.equal(r.children[5].children.length,5)});
});
test('saved credentials with unknown health are not labelled a failed login',async()=>{
  const f=fixture();f.accounts[0].health_state='unknown';await f.context.renderAuthPool();
  assert.equal(f.nodes['auth-rows'].children[0].children[1].children[0].text,'SAVED · UNVERIFIED');
});
for(const [label,index,suffix] of [['sign in',0,'login'],['capture login',1,'capture'],['check setup',2,'test'],['disable',3,'disable']]){
  test(label+' sends a POST body instead of an invalid GET',async()=>{
    const f=fixture();await f.context.renderAuthPool();
    await f.nodes['auth-rows'].children[0].children[5].children[index].onclick();
    const request=f.requests.find(r=>r.url.endsWith('/'+suffix));
    assert.ok(request);assert.notEqual(request.body,undefined);
    assert.equal(Object.keys(request.body).length,0);
  });
}
