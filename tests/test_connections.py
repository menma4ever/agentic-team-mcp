import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import httpx

from core.config import SystemSettings, Provider, ROOT
from core.providers import PROVIDER_PRESETS
from engine.models import AgentNode, Role, HarnessType
from engine.orchestrator import Orchestrator
from harness.cli_runner import CLIRunner
from harness.direct_api import DirectAPIRunner
from web.app import create_app


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_flash_requires_default_effort_and_preserves_explicit_choice(self):
        self.agent.model='antigravity/gemini-3.8-flash'
        with patch.object(self.runner,'resolve',side_effect=lambda name:['agy']):
            args,_,_=self.runner.build(self.agent,'task',self.root,'http://127.0.0.1:8765','test')
            self.assertEqual(args[args.index('--effort')+1],'medium')
            self.agent.reasoning_effort='low'
            args,_,_=self.runner.build(self.agent,'task',self.root,'http://127.0.0.1:8765','test')
            self.assertEqual(args[args.index('--effort')+1],'low')

    async def asyncSetUp(self):
        temp_root=ROOT/'tests/.tmp'
        temp_root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=temp_root)
        self.root=Path(self.temp.name)
        self.config=SystemSettings(cli_auth_enabled={'agy':True})
        self.runner=CLIRunner(self.config,google_projects_dir=self.root/'google-projects')
        self.agent=AgentNode(project_name='Test',name='CEO',role=Role.CEO,
            model='antigravity/gemini-3.1-pro-high',harness=HarnessType.ANTIGRAVITY,
            system_prompt='You are the CEO.',working_dir=str(self.root))

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_google_stream_resume_and_scoped_mcp_configuration(self):
        script=self.root/'mock_google.py'
        script.write_text('''import json,sys
message=json.loads(sys.stdin.readline())
assert message['event']=='user'
assert 'You are the CEO.' in message['message']['content']
print(json.dumps({'event':'init','conversation_id':'google-session-1','init':{}}),flush=True)
print(json.dumps({'event':'result','result':{'conversation_id':'google-session-1','status':'SUCCESS','response':'Finished the task'}}),flush=True)
''')
        with patch.object(self.runner,'resolve',return_value=[sys.executable,str(script)]), patch.dict(os.environ,{'GEMINI_API_KEY':'must-not-use','TEAM_OWNER_TOKEN':'must-not-share'}):
            result=await self.runner.execute_task(self.agent,'Say hello',self.root,AsyncMock(),
                endpoint='http://127.0.0.1:8765',token='scoped-test-token',allow_commands=True)
            argv,env,_=self.runner.build(self.agent,'Continue',self.root,'http://127.0.0.1:8765','scoped-test-token')
        self.assertEqual(result,'Finished the task')
        self.assertEqual(self.agent.session_id,'google-session-1')
        self.assertEqual(argv[argv.index('--conversation')+1],'google-session-1')
        self.assertNotIn('GEMINI_API_KEY',env)
        self.assertNotIn('TEAM_OWNER_TOKEN',env)
        self.assertEqual(env['TEAM_TOKEN'],'scoped-test-token')
        raw=(self.root/'.agents/mcp_config.json').read_text()
        self.assertNotIn('scoped-test-token',raw)
        self.assertIn('--agent-mcp',raw)
        project=json.loads(next((self.root/'google-projects').glob('*.json')).read_text())
        self.assertEqual(project['id'],argv[argv.index('--project')+1])
        self.assertIn('mcp(agentic_team/*)',project['permissionGrants']['permissionGrants']['allow'])
        self.assertEqual(len(list((self.root/'google-projects').glob('*.json'))),1)

    async def test_google_failure_envelope_is_not_success(self):
        async def process(*args,**kwargs):
            args[-1](json.dumps({'event':'result','result':{'conversation_id':'blocked-session','status':'WAITING','response':'Waiting'}}))
        with patch.object(self.runner,'resolve',return_value=['agy']),patch.object(self.runner,'_process',side_effect=process):
            with self.assertRaisesRegex(RuntimeError,'WAITING'):
                await self.runner.execute_task(self.agent,'task',self.root,AsyncMock(),
                    endpoint='http://127.0.0.1:8765',token='scoped-test-token',allow_commands=True)
        self.assertEqual(self.agent.session_id,'blocked-session')

    async def test_google_login_configuration_preserves_user_permissions(self):
        path=self.root/'settings.json'
        original={'modelProvider':'gemini','theme':'dark','permissions':{'deny':['command(rm)'],'ask':['mcp(*)']}}
        path.write_text(json.dumps(original))
        self.runner.prepare_google_account(path)
        result=json.loads(path.read_text())
        self.assertNotIn('modelProvider',result)
        self.assertEqual(result['permissions'],original['permissions'])
        self.assertEqual(result['theme'],'dark')

    async def test_google_requires_account_and_supported_effort(self):
        with patch.object(self.runner,'resolve',return_value=['agy']):
            with self.assertRaisesRegex(ValueError,'reasoning effort'):
                self.runner.validate(HarnessType.ANTIGRAVITY,'gemini/model',reasoning_effort='max')
            self.config.cli_auth_enabled['agy']=False
            with self.assertRaisesRegex(ValueError,'Google account mode'):
                self.runner.validate(HarnessType.ANTIGRAVITY,'gemini/model')

    async def test_agent_mcp_never_falls_back_to_owner(self):
        env=os.environ.copy();env.pop('TEAM_TOKEN',None);env.pop('TEAM_ENDPOINT',None)
        result=subprocess.run([sys.executable,str(ROOT/'main.py'),'--agent-mcp'],
            env=env,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('owner fallback is disabled',result.stderr)
        self.assertEqual(result.stdout,'')

    async def test_glm_reasoning_survives_tool_round_trip(self):
        preset=PROVIDER_PRESETS['zai']
        self.config.providers['zai']=Provider(adapter=preset['adapter'],base_url=preset['base_url'],models=preset['models'])
        self.config.api_keys['zai']='test-key'
        self.agent.harness=HarnessType.DIRECT_API
        self.agent.model='zai/glm-5.3'
        turns=[]
        async def complete(**kwargs):
            turns.append(json.loads(json.dumps(kwargs)))
            if len(turns)==1:
                msg={'role':'assistant','content':None,'reasoning_content':'I need to inspect the artifact.',
                     'tool_calls':[{'id':'call1','type':'function','function':{'name':'read_file','arguments':'{"path":"artifacts/result.txt"}'}}]}
            else: msg={'role':'assistant','content':'Inspected.'}
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
        result=await DirectAPIRunner(self.config,complete).generate_response(self.agent,
            [{'role':'user','content':'Inspect'}],[{'type':'function'}],AsyncMock(return_value={'content':'OK'}),AsyncMock())
        self.assertEqual(result,'Inspected.')
        self.assertEqual(turns[1]['messages'][1]['reasoning_content'],'I need to inspect the artifact.')
        self.assertEqual(turns[1]['messages'][2]['tool_call_id'],'call1')
        self.assertEqual(turns[0]['api_base'],PROVIDER_PRESETS['zai']['base_url'])
        self.assertEqual(turns[0]['model'],'openai/glm-5.3')

    async def test_connections_api_google_login_and_glm_preset(self):
        engine=Orchestrator(self.root,self.config,runner=SimpleNamespace(generate_response=AsyncMock()))
        try:
            app=create_app(engine,'test-owner')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
                denied=await client.post('/api/harness/login',json={'harness':'agy'})
                self.assertEqual(denied.status_code,401)
                client.headers['Authorization']='Bearer test-owner'
                info=(await client.get('/api/settings')).json()
                self.assertEqual(info['provider_presets']['zai']['alias'],'zai')
                self.assertTrue(next(h for h in info['harnesses'] if h['name']=='agy')['adapter_supported'])
                with patch('harness.cli_runner.CLIRunner.resolve',return_value=['agy.exe']),patch('core.auth_pool.WindowsKeyringHelper.read_credential',return_value=None),patch('core.auth_pool.WindowsKeyringHelper.delete_credential',return_value=True),patch('subprocess.Popen') as launch:
                    response=await client.post('/api/harness/login',json={'harness':'agy'})
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(engine.auth_pool.login_pending,'account_01')
                    if os.name=='nt':
                        self.assertNotIn('--device-auth',str(launch.call_args))
                        self.assertNotIn('TEAM_TOKEN',launch.call_args.kwargs['env'])
                with patch.object(SystemSettings,'save'):
                    response=await client.post('/api/settings',json={'providers':{'zai':{'adapter':'openai','base_url':PROVIDER_PRESETS['zai']['base_url'],'models':['glm-5.3']}},'api_keys':{'zai':'private-test-key'}})
                    self.assertEqual(response.status_code,200)
                    self.assertNotIn('private-test-key',response.text)
                    self.assertEqual(self.config.providers['zai'].models,['glm-5.3'])
        finally:
            await engine.close()


if __name__=='__main__':
    unittest.main()
