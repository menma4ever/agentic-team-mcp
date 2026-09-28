"""Real HTTP + stdio MCP + LiteLLM contract test using a local deterministic provider."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parent.parent
TMP=ROOT/'tests'/'.tmp'
TMP.mkdir(exist_ok=True)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_and_dashboard_share_engine_and_provider_tool_loop(self):
        counts={}
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                model=body['model'];counts[model]=counts.get(model,0)+1;n=counts[model]
                calls=[];text=None
                if model=='ceo':
                    if n==1:calls=[('create_manager',{'name':'Manager','model':'localtest/manager','task_description':'Write a verified local artifact'})]
                    else:text='The manager is executing.'
                elif model=='manager':
                    if n==1:calls=[('spawn_worker',{'name':'Writer','model':'localtest/worker','task_description':'Write artifacts/verified.txt'}),
                                    ('wait_for_workers',{})]
                    elif n==2:calls=[('read_file',{'path':'artifacts/verified.txt'})]
                    else:calls=[('finish_project',{'summary':'Verified local provider artifact','artifacts':['artifacts/verified.txt']})]
                else:calls=[('write_file',{'path':'artifacts/verified.txt','content':'Verified through real HTTP and MCP'}),
                            ('report_result',{'outcome':'completed','summary':'File created','artifacts':['artifacts/verified.txt']})]
                message={'role':'assistant','content':text}
                if calls:message['tool_calls']=[{'id':model+str(n)+'_'+str(i),'type':'function',
                    'function':{'name':name,'arguments':json.dumps(args)}} for i,(name,args) in enumerate(calls)]
                response={'id':'local-contract-test','object':'chat.completion','created':int(time.time()),'model':model,
                    'choices':[{'index':0,'message':message,'finish_reason':'tool_calls' if calls else 'stop'}],
                    'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
                content=json.dumps(response).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
        provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        thread=threading.Thread(target=provider.serve_forever,daemon=True);thread.start()
        with tempfile.TemporaryDirectory(dir=str(TMP)) as tmp:
            root=Path(tmp).resolve()
            assert root.is_relative_to(TMP.resolve())
            with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
            env=os.environ.copy()
            env.update(AGENTIC_TEAM_DATA=str(root),LITELLM_LOCAL_MODEL_COST_MAP='True')
            env.pop('TEAM_TOKEN',None);env.pop('TEAM_ENDPOINT',None)
            log=(root/'service.log').open('w')
            py_exe = str(ROOT / '.venv' / 'Scripts' / 'python.exe')
            if not os.path.isfile(py_exe):
                py_exe = sys.executable
            kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            proc=subprocess.Popen([py_exe,str(ROOT/'main.py'),'--serve','--port',str(port)],
                cwd=str(ROOT),env=env,stdout=log,stderr=log,**kwargs)
            try:
                async with httpx.AsyncClient(timeout=30,trust_env=False) as client:
                    deadline=time.monotonic()+15
                    info=None
                    while time.monotonic()<deadline:
                        try:
                            info=json.loads((root/'engine.json').read_text())
                            r=await client.get(info['url']+'/health')
                            if r.status_code==200:break
                        except (OSError,ValueError,httpx.HTTPError):pass
                        await asyncio.sleep(.1)
                    self.assertIsNotNone(info,(root/'service.log').read_text())
                    client.headers['Authorization']='Bearer '+info['token']
                    config={'providers':{'localtest':{'adapter':'openai','base_url':f'http://127.0.0.1:{provider.server_port}/v1','models':['ceo','manager','worker']}},
                            'api_keys':{'localtest':'synthetic-test-key'}}
                    r=await client.post(info['url']+'/api/settings',json=config)
                    self.assertEqual(r.status_code,200,r.text)
                    child_env={**env,'TEAM_ENDPOINT':info['url'],'TEAM_TOKEN':info['token']}
                    params=StdioServerParameters(command=py_exe,args=[str(ROOT/'main.py'),'--mcp'],env=child_env)
                    async with stdio_client(params) as (read,write):
                        async with ClientSession(read,write) as session:
                            await session.initialize()
                            names={t.name for t in (await session.list_tools()).tools}
                            self.assertIn('spawn_worker',names)
                            result=await session.call_tool('create_project',{'name':'Integration','ceo_model':'localtest/ceo'})
                            self.assertFalse(result.isError,str(result))
                            tree=(await client.get(info['url']+'/api/tree',params={'project':'Integration'})).json()
                            cid=tree['ceo']['id']
                            check=await session.call_tool('get_team_tree',{'project_name':'Integration'})
                            payload=check.structuredContent or json.loads(check.content[0].text)
                            self.assertEqual(payload['ceo']['id'],cid)
                            send=await session.call_tool('send_team_message',{'project_name':'Integration','target_agent_id':cid,'message':'Create and verify the artifact'})
                            self.assertFalse(send.isError,str(send))
                            deadline=time.monotonic()+40
                            while time.monotonic()<deadline:
                                tree=(await client.get(info['url']+'/api/tree',params={'project':'Integration'})).json()
                                if tree['project']['status']=='completed':break
                                if tree['ceo']['status']=='failed':break
                                await asyncio.sleep(.1)
                            self.assertEqual(tree['project']['status'],'completed',json.dumps(tree))
                            self.assertEqual((root/'projects/Integration/artifacts/verified.txt').read_text(),'Verified through real HTTP and MCP')
                            self.assertEqual(counts,{'ceo':2,'manager':3,'worker':1})
                            for tool, key in [('get_agent_activity','events'), ('get_agent_conversation','messages')]:
                                result=await session.call_tool(tool,{'agent_id':cid})
                                self.assertFalse(result.isError,str(result))
                                payload=result.structuredContent or json.loads(result.content[0].text)
                                self.assertTrue(payload[key],f'{tool} should expose actual persisted evidence')
                    # A second MCP host sees the existing team, even after the first disconnects.
                    async with stdio_client(params) as (read,write):
                        async with ClientSession(read,write) as session:
                            await session.initialize()
                            result=await session.call_tool('get_team_tree',{'project_name':'Integration'})
                            payload=result.structuredContent or json.loads(result.content[0].text)
                            self.assertEqual(payload['ceo']['id'],cid)
                            self.assertEqual(payload['project']['status'],'completed')
                    print('HTTP + MCP + local provider: full delegation, artifact verification and shared sessions PASS')
            finally:
                proc.terminate()
                await asyncio.to_thread(proc.wait,10)
                log.close()
                provider.shutdown();provider.server_close()
                thread.join(2)


if __name__=='__main__':unittest.main()

