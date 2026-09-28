import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
import psutil
from core.config import SystemSettings, Provider
from core.workspace import safe_name, contained
from engine.orchestrator import Orchestrator
from engine.models import Role, AgentStatus, HarnessType
from harness.direct_api import DirectAPIRunner
from harness.cli_runner import CLIRunner
from web.app import create_app

TMP=Path(__file__).resolve().parent/'.tmp'
TMP.mkdir(exist_ok=True)


async def until(fn, timeout=5):
    async with asyncio.timeout(timeout):
        while not fn(): await asyncio.sleep(.01)


def reply(text=None, calls=None):
    raw={'role':'assistant','content':text}
    if calls:
        raw['tool_calls']=[{'id':str(i),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}
                           for i,(name,args) in enumerate(calls)]
    return SimpleNamespace(choices=[SimpleNamespace(message=raw)])


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=str(TMP))
        self.root=Path(self.temp.name).resolve()
        assert self.root.is_relative_to(TMP.resolve())
        self.config=SystemSettings(max_autonomous_turns=6)
        self.runner=SimpleNamespace(generate_response=AsyncMock(return_value='Hello'))
        self.e=Orchestrator(self.root,self.config,runner=self.runner)
        await self.e.create_project('Project',ceo_model='test/ceo')
        self.ceo=self.e.get_ceo('Project')

    async def asyncTearDown(self):
        if self.e:
            await self.e.close()
        self.temp.cleanup()

    async def worker(self):
        manager=self.e._add_agent('Project','Manager',Role.MANAGER,'test/manager',parent_id=self.ceo.id)
        self.e.projects['Project'].manager_id=manager.id
        worker=self.e._add_agent('Project','Worker',Role.WORKER,'test/worker',parent_id=manager.id)
        self.e.projects['Project'].worker_ids.append(worker.id)
        self.e.persist()
        return manager,worker

    async def test_full_ceo_manager_worker_artifact_completion(self):
        counts={}
        async def completion(**kwargs):
            model=kwargs['model'];counts[model]=counts.get(model,0)+1
            n=counts[model]
            if model=='test/ceo':
                if n==1:
                    return reply(calls=[('write_file',{'path':'ceo/roadmap.md','content':'Create a verified greeting artifact'}),
                        ('create_manager',{'name':'Manager','model':'test/manager','task_description':'Create greeting and verify'})])
                return reply('Manager is handling execution.')
            if model=='test/manager':
                if n==1:
                    return reply(calls=[('spawn_worker',{'name':'Writer','model':'test/worker','task_description':'Write artifacts/greeting.txt'}),
                                        ('wait_for_workers',{})])
                if n==2: return reply(calls=[('read_file',{'path':'artifacts/greeting.txt'})])
                return reply(calls=[('finish_project',{'summary':'Verified greeting artifact','artifacts':['artifacts/greeting.txt']})])
            return reply(calls=[('write_file',{'path':'artifacts/greeting.txt','content':'Hello from the worker'}),
                                ('report_result',{'outcome':'completed','summary':'Created greeting','artifacts':['artifacts/greeting.txt']})])
        self.e.runner=DirectAPIRunner(self.config,completion)
        await self.e.send('Project',self.ceo.id,'Build the greeting project')
        await until(lambda:self.e.projects['Project'].status=='completed', timeout=10)
        self.assertEqual((self.root/'projects/Project/artifacts/greeting.txt').read_text(),'Hello from the worker')
        self.assertTrue(self.e.projects['Project'].completion['artifacts'][0]['sha256'])
        manager=self.e.get_manager('Project')
        self.assertTrue(any(m.recipient_id==manager.id and m.sender_role=='WORKER' for m in self.e.messages))
        self.assertIsNotNone(manager)
        self.assertEqual(counts,{'test/ceo':2,'test/manager':3,'test/worker':1})

    async def test_no_default_manager_and_spawn_really_runs(self):
        self.assertIsNone(self.e.get_manager('Project'))
        manager,unused=await self.worker()
        await self.e.spawn_worker('Project',name='Real',model='test/worker',task_description='Assigned')
        await until(lambda:self.runner.generate_response.await_count>=1)
        self.assertTrue(any(m.kind=='task' for m in self.e.messages))

    async def test_queue_is_serial(self):
        gate=asyncio.Event();started=[];active=0;maximum=0
        async def run(*args):
            nonlocal active,maximum
            active+=1;maximum=max(maximum,active);started.append(1)
            try: await gate.wait()
            finally: active-=1
            return 'response'
        self.runner.generate_response.side_effect=run
        await self.e.send('Project',self.ceo.id,'one')
        await until(lambda:len(started)==1)
        await self.e.send('Project',self.ceo.id,'two')
        await asyncio.sleep(.03)
        self.assertEqual(len(started),1)
        gate.set()
        await until(lambda:len(started)==2)
        self.assertEqual(maximum,1)

    async def test_interrupt_cancels_then_runs_and_resume_preserves_context(self):
        started=[];cancelled=[];gate=asyncio.Event()
        async def run(a,context,*args):
            content=context[-1]['content'];started.append(content)
            if len(started)==1:
                try: await gate.wait()
                except asyncio.CancelledError: cancelled.append(True);raise
            return 'answer'
        self.runner.generate_response.side_effect=run
        await self.e.send('Project',self.ceo.id,'original')
        await until(lambda:len(started)==1)
        await self.e.send('Project',self.ceo.id,'urgent',is_interrupt=True)
        await until(lambda:len(started)==2)
        self.assertTrue(cancelled)
        self.assertIn(self.ceo.id,self.e.paused)
        await self.e.action('Project','resume_agent',{'target_agent_id':self.ceo.id})
        await until(lambda:len(started)==3)
        self.assertIn('original',started[-1])

    async def test_restart_preserves_ids_history_and_pending_work(self):
        await self.e.send('Project',self.ceo.id,'hello')
        await until(lambda:self.runner.generate_response.await_count==1 and not self.e.active)
        cid=self.ceo.id
        await self.e.close()
        self.e=Orchestrator(self.root,self.config,runner=self.runner)
        self.assertEqual(self.e.get_ceo('Project').id,cid)
        self.assertTrue(self.e.contexts[cid])
        self.assertTrue(self.e.messages)

    async def test_provider_failure_is_error_not_fake_progress(self):
        self.runner.generate_response.side_effect=RuntimeError('Provider unavailable')
        await self.e.send('Project',self.ceo.id,'hello')
        await until(lambda:self.ceo.status==AgentStatus.FAILED)
        self.assertIn('Provider unavailable',self.ceo.last_error)
        self.assertFalse(any('45%' in m.content for m in self.e.messages))

    async def test_escalation_wakes_parent_preserving_identity(self):
        mgr,worker=await self.worker()
        await self.e.action('Project','escalate',{'issue_summary':'Need help'},worker.id)
        await until(lambda:self.runner.generate_response.await_count>=1)
        message=next(m for m in self.e.messages if m.content=='Need help')
        self.assertEqual(message.sender_role,'WORKER')
        self.assertEqual(message.recipient_id,mgr.id)

    async def test_real_process_tree_termination_and_archive(self):
        mgr,worker=await self.worker()
        started=asyncio.Event()
        async def run(a,context,tools,execute,emit):
            if a.id!=worker.id:return 'ok'
            async def on_event(kind,data):
                await self.e.emit(a,kind,data)
                if kind=='process_started':started.set()
            # Child has no model/network access. Verify actual OS process cancellation.
            code='import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); print(p.pid,flush=True); time.sleep(60)'
            return await self.e.cli._process(a.id,[sys.executable,'-c',code],Path(a.working_dir),on_event,timeout=90)
        self.runner.generate_response.side_effect=run
        (Path(worker.working_dir)/'keep.txt').write_text('evidence')
        await self.e.send('Project',worker.id,'run',kind='task')
        await asyncio.wait_for(started.wait(),5)
        await until(lambda:any(x['type']=='output' for x in self.e.store.events(worker.id)))
        pid=worker.pid
        outputs=[x['data']['text'] for x in self.e.store.events(worker.id) if x['type']=='output']
        child_pid=int(''.join(outputs).strip())
        result=await self.e.terminate_worker('Project',worker.id,True)
        self.assertFalse(psutil.pid_exists(pid))
        self.assertFalse(psutil.pid_exists(child_pid))
        self.assertTrue(Path(result['archive']).is_file())
        self.assertFalse(Path(worker.working_dir).exists())
        self.assertEqual(worker.status,AgentStatus.TERMINATED)

    async def test_files_and_roles_are_scoped(self):
        mgr,w=await self.worker()
        with self.assertRaises(PermissionError):
            await self.e.action('Project','spawn_worker',{'name':'no','model':'test/x','task_description':'x'},w.id)
        with self.assertRaises(PermissionError):
            await self.e.action('Project','write_file',{'path':'manager/overwrite.md','content':'no'},w.id)
        for name in ('..','a/b','a\\b','CON','x.'):
            with self.assertRaises(ValueError):safe_name(name)
        with self.assertRaises(ValueError):contained(self.root,'../outside')
        with self.assertRaises(ValueError):
            await self.e.action('Project','report_result',{'outcome':'completed','summary':'invented','artifacts':['artifacts/missing']},w.id)

    async def test_api_auth_create_spawn_and_no_secret_exposure(self):
        app=create_app(self.e,'owner-secret')
        transport=httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,base_url='http://127.0.0.1:8765') as client:
            self.assertEqual((await client.get('/api/settings')).status_code,401)
            client.headers['Authorization']='Bearer owner-secret'
            self.config.api_keys['openai']='secret-key-example'
            data=(await client.get('/api/settings')).json()
            self.assertNotIn('secret-key-example',json.dumps(data))
            self.assertEqual((await client.get('/api/settings',headers={'Origin':'https://evil.invalid'})).status_code,403)
            result=await client.post('/api/projects',json={'name':'Web','ceo_model':'test/ceo'})
            self.assertEqual(result.status_code,200,result.text)
            result=await client.post('/api/action',json={'project_name':'Web','action':'create_manager',
                'arguments':{'name':'Mgr','model':'test/mgr','task_description':'x'}})
            self.assertEqual(result.status_code,200,result.text)
            result=await client.post('/api/action',json={'project_name':'Web','action':'spawn_worker',
                'arguments':{'name':'W','model':'test/w','task_description':'x'}})
            self.assertEqual(result.status_code,200,result.text)
            result=await client.post('/api/projects',json={'name':'Web','ceo_model':'test/ceo'})
            self.assertEqual(result.status_code,400)

    async def test_agent_token_cannot_act_as_owner_or_cross_project(self):
        mgr,w=await self.worker()
        token=self.e.token_for(w.id)
        await self.e.create_project('Other',ceo_model='test/ceo')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.e,'owner')),
                                     base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+token}) as c:
            self.assertEqual((await c.get('/api/settings')).status_code,403)
            r=await c.post('/api/action',json={'project_name':'Other','action':'get_team_tree','arguments':{}})
            self.assertEqual(r.status_code,403)
            r=await c.post('/api/action',json={'project_name':'Project','action':'send_team_message',
                'arguments':{'target_agent_id':mgr.id,'message':'help'}})
            self.assertEqual(r.status_code,200)
            sent=next(m for m in self.e.messages if m.id==r.json()['message_id'])
            self.assertEqual(sent.sender_id,w.id)

    async def test_direct_api_custom_endpoint_and_tools(self):
        captured={}
        async def completion(**kwargs):
            captured.update(kwargs);return reply('answer')
        self.config.providers['custom']=Provider(adapter='openai',base_url='http://127.0.0.1:9999/v1')
        self.config.api_keys['custom']='dummy'
        self.ceo.model='custom/model'
        runner=DirectAPIRunner(self.config,completion)
        await runner.generate_response(self.ceo,[{'role':'user','content':'test'}],[{'type':'function'}],AsyncMock(),AsyncMock())
        self.assertEqual(captured['api_base'],'http://127.0.0.1:9999/v1')
        self.assertEqual(captured['model'],'openai/model')
        self.assertEqual(captured['api_key'],'dummy')
        self.assertTrue(captured['tools'])

    async def test_no_result_is_not_completion(self):
        mgr,w=await self.worker()
        await self.e.send('Project',w.id,'task',kind='task')
        await until(lambda:w.status==AgentStatus.FAILED)
        self.assertIsNone(w.last_result)
        self.assertIn('unverified',w.last_error)

    async def test_cli_streaming_session_capture_and_resume_arguments(self):
        script=self.root/'mock_cli.py'
        script.write_text('import json\nprint(json.dumps({"type":"thread.started","thread_id":"test-session"}),flush=True)\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"Real subprocess output"}}),flush=True)\n')
        self.ceo.harness=HarnessType.CODEX
        self.ceo.model='openai/test-model'
        self.config.cli_auth_enabled['codex']=True
        runner=CLIRunner(self.config)
        events=[]
        async def emit(kind,data):events.append((kind,data))
        with patch.object(runner,'resolve',return_value=[sys.executable,str(script)]):
            result=await runner.execute_task(self.ceo,'hello',Path(self.ceo.working_dir),emit,
                endpoint='http://127.0.0.1:8765',token='agent-scoped-test-token',allow_commands=True)
            argv,env,_=runner.build(self.ceo,'continue',Path(self.ceo.working_dir),'http://127.0.0.1:8765','agent-scoped-test-token')
        self.assertEqual(result,'Real subprocess output')
        self.assertEqual(self.ceo.session_id,'test-session')
        self.assertIn('resume',argv)
        self.assertIn('test-session',argv)
        self.assertNotIn('agent-scoped-test-token',' '.join(argv))
        self.assertEqual(env['TEAM_TOKEN'],'agent-scoped-test-token')
        self.assertTrue(any(k=='output' for k,d in events))

    async def test_restart_reconciles_owned_process_by_birth_time(self):
        import subprocess
        opts={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
        proc=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],**opts)
        try:
            self.ceo.pid=proc.pid
            self.ceo.process_started_at=psutil.Process(proc.pid).create_time()
            self.e.inflight[self.ceo.id]={'content':'Interrupted assignment'}
            self.e.persist()
            self.e.store.close()
            self.e=Orchestrator(self.root,self.config,runner=self.runner)
            self.assertFalse(psutil.pid_exists(proc.pid))
            self.assertEqual(self.e.get_ceo('Project').status,AgentStatus.PAUSED)
            self.assertIn(self.ceo.id,self.e.paused)
        finally:
            if proc.poll() is None:proc.terminate()
            proc.wait()

    async def test_blocked_report_stays_paused(self):
        mgr,w=await self.worker()
        await self.e.action('Project','report_result',{'outcome':'blocked','summary':'Need input','artifacts':[]},w.id)
        self.assertIn(w.id,self.e.paused)
        self.assertEqual(w.last_result['outcome'],'blocked')

    async def test_connect_and_disconnect_agents(self):
        mgr,w=await self.worker()
        # Connect worker w to manager mgr or ceo
        res=await self.e.action('Project','connect_agents',{'source_id':mgr.id,'target_id':w.id},'human_owner')
        self.assertTrue(res['connected'])
        self.assertIn(w.id,mgr.connections)
        self.assertEqual(w.parent_id,mgr.id)

        # Update position
        pos_res=await self.e.set_agent_position('Project',w.id,120,240)
        self.assertTrue(pos_res['saved'])
        self.assertEqual(w.position,{'x':120.0,'y':240.0})

        # Disconnect
        disc_res=await self.e.action('Project','disconnect_agents',{'source_id':mgr.id,'target_id':w.id},'human_owner')
        self.assertTrue(disc_res['disconnected'])
        self.assertNotIn(w.id,mgr.connections)

    async def test_auto_resume_on_quota_cleared(self):
        from core.auth_pool import GoogleAccountHealth
        mgr, w = await self.worker()
        w.status = AgentStatus.PAUSED
        w.last_error = 'No healthy Google accounts available. Earliest quota reset at 2026-09-26T12:00:00Z.'
        self.e.paused[w.id] = {'content': 'Continue previous work'}
        self.e.persist()

        acc = self.e.auth_pool.register_account('healthy@example.com', account_id='acc_healthy')
        acc.health_state = GoogleAccountHealth.HEALTHY
        auth_dir = self.e.auth_pool.resolve_auth_dir(acc)
        auth_dir.mkdir(parents=True, exist_ok=True)
        (auth_dir / 'credential.dat').write_bytes(b'dummy_cred')

        res = await self.e.resume_agent(w.id, 'Resumed automatically after quota cleared')
        self.assertTrue(res['resumed'])
        self.assertIn(w.status, (AgentStatus.QUEUED, AgentStatus.WORKING))
        self.assertNotIn(w.id, self.e.paused)


if __name__=='__main__':
    unittest.main()


