import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.auth_pool import GoogleAccountHealth, WindowsKeyringHelper
from core.config import SystemSettings
from engine.models import AgentStatus, HarnessType, Role
from engine.orchestrator import Orchestrator
from harness.cli_runner import CLIRunner


async def settle(engine, aid):
    async with asyncio.timeout(4):
        while aid in engine.pumps and not engine.pumps[aid].done():
            await asyncio.sleep(.01)


class RuntimeRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cli = SimpleNamespace(validate=lambda *a: None, execute_task=AsyncMock(return_value='Done'))
        self.runner = SimpleNamespace(generate_response=AsyncMock(return_value='Done'))
        self.engine = Orchestrator(self.root, SystemSettings(), cli=self.cli, runner=self.runner)
        await self.engine.create_project('P', ceo_model='test/old', allow_commands=True)
        self.ceo = self.engine.get_ceo('P')
        self.vault = b'original'
        def read(): return ('test', self.vault) if self.vault else None
        def write(blob, username='test'): self.vault=blob; return True
        def delete(): self.vault=None; return True
        for method, fn in [('read_credential', read), ('write_credential', write), ('delete_credential', delete)]:
            p=patch.object(WindowsKeyringHelper, method, side_effect=fn)
            p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.engine.close()
        self.temp.cleanup()

    def google(self):
        self.ceo.harness=HarnessType.ANTIGRAVITY
        self.ceo.model='antigravity/test'
        self.google_blobs = {}
        for name in ['one','two']:
            a=self.engine.auth_pool.register_account(name+'@test.local',account_id=name)
            claims = base64.urlsafe_b64encode(json.dumps({'email': a.email}).encode()).decode().rstrip('=')
            blob = json.dumps({'id_token': 'test.'+claims+'.test'}).encode()
            self.google_blobs[name] = blob
            (self.engine.auth_pool.resolve_auth_dir(a)/'credential.dat').write_bytes(blob)

    async def test_quota_failover_retains_native_session_directory_and_releases_leases(self):
        self.google(); calls=[]
        self.ceo.session_id='native-session'
        async def run(agent,prompt,folder,emit,**kw):
            calls.append((agent.auth_slot_id,str(kw['auth_dir']),agent.session_id,self.vault))
            if len(calls)==1: raise RuntimeError('429 quota exceeded; retry-after: 300')
            return 'Recovered'
        self.cli.execute_task=run
        await self.engine.send('P',self.ceo.id,'Continue')
        await settle(self.engine,self.ceo.id)
        self.assertEqual([c[0] for c in calls],['one','two'])
        self.assertEqual(calls[0][1:3],calls[1][1:3])
        self.assertEqual([c[3] for c in calls],[self.google_blobs['one'],self.google_blobs['two']])
        self.assertEqual(self.vault,b'original')
        self.assertEqual(self.ceo.status,AgentStatus.IDLE)
        self.assertTrue(all(a.active_agents==0 for a in self.engine.auth_pool.accounts.values()))

    async def test_all_accounts_blocked_pauses_without_losing_assignment(self):
        self.google()
        self.cli.execute_task=AsyncMock(side_effect=RuntimeError('RESOURCE_EXHAUSTED quota exceeded'))
        await self.engine.send('P',self.ceo.id,'Unique unfinished assignment')
        await settle(self.engine,self.ceo.id)
        self.assertEqual(self.ceo.status,AgentStatus.PAUSED)
        self.assertEqual(self.engine.paused[self.ceo.id]['content'],'Unique unfinished assignment')
        self.assertEqual(self.cli.execute_task.call_count,2)
        self.assertEqual(self.vault,b'original')

    async def test_concurrent_google_turns_keep_vault_identity_until_process_exits(self):
        self.google()
        other=self.engine._add_agent('P','Other',Role.WORKER,'antigravity/test','antigravity')
        active=0; peak=0
        async def run(agent,*args,**kw):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            expected = self.google_blobs[agent.auth_slot_id]
            self.assertEqual(self.vault, expected)
            await asyncio.sleep(.04)
            self.assertEqual(self.vault, expected)
            active-=1
            return 'Done'
        self.cli.execute_task=run
        await self.engine.send('P',self.ceo.id,'A')
        await self.engine.send('P',other.id,'B')
        await asyncio.gather(settle(self.engine,self.ceo.id),settle(self.engine,other.id))
        self.assertEqual(peak,1)
        self.assertEqual(self.vault,b'original')

    async def test_cancelled_google_turn_releases_account_and_restores_vault(self):
        self.google(); started=asyncio.Event()
        async def run(*args,**kw): started.set();await asyncio.Future()
        self.cli.execute_task=run
        await self.engine.send('P',self.ceo.id,'Wait')
        await asyncio.wait_for(started.wait(),2)
        self.engine.active[self.ceo.id].cancel()
        await settle(self.engine,self.ceo.id)
        self.assertEqual(self.vault,b'original')
        self.assertEqual(self.engine.auth_pool.accounts['one'].active_agents,0)

    async def test_model_change_waits_then_archives_last_event_and_context(self):
        started=asyncio.Event();release=asyncio.Event()
        async def run(agent,context,definitions,execute,emit):
            started.set();await release.wait()
            await emit('model_response',{'text':'Last second output','usage':{'input_tokens':12,'output_tokens':3}})
            return 'Final old response'
        self.runner.generate_response=run
        await self.engine.send('P',self.ceo.id,'Remember this')
        await asyncio.wait_for(started.wait(),2)
        await self.engine.reconfigure_agent('P',self.ceo.id,'test/new','direct_api')
        self.assertEqual(self.ceo.model,'test/old')
        self.assertFalse(self.engine.active[self.ceo.id].cancelled())
        release.set();await settle(self.engine,self.ceo.id)
        self.assertEqual(self.ceo.model,'test/new')
        record=json.loads(Path(self.ceo.session_history[-1]['archive']).read_text(encoding='utf-8'))
        self.assertIn('Last second output',json.dumps(record))
        self.assertIn('Final old response',json.dumps(record))
        self.assertIn('Remember this',self.ceo.handoff_pending)
        events=self.engine.store.events(self.ceo.id,-1)
        self.assertEqual(events[0]['model'],'test/old')

    async def test_immediate_change_resumes_same_agent_without_losing_workspace(self):
        started=asyncio.Event()
        async def run(agent,*args):
            if agent.model=='test/old':started.set();await asyncio.Future()
            return 'New runtime continued'
        self.runner.generate_response=run
        (Path(self.ceo.working_dir)/'artifact.txt').write_text('keep')
        await self.engine.send('P',self.ceo.id,'Continue exact work')
        await asyncio.wait_for(started.wait(),2)
        await asyncio.wait_for(self.engine.reconfigure_agent('P',self.ceo.id,'test/new','direct_api','interrupt'),2)
        await settle(self.engine,self.ceo.id)
        self.assertEqual(self.ceo.model,'test/new')
        self.assertEqual((Path(self.ceo.working_dir)/'artifact.txt').read_text(),'keep')
        self.assertEqual(self.ceo.status,AgentStatus.IDLE)

    async def test_collaboration_links_cannot_reparent_or_escalate_privileges(self):
        worker=self.engine._add_agent('P','W',Role.WORKER,'test/worker',parent_id=self.ceo.id)
        await self.engine.connect_agents('P',worker.id,self.ceo.id)
        self.assertIsNone(self.ceo.parent_id)
        self.assertEqual(worker.parent_id,self.ceo.id)
        with self.assertRaises(PermissionError):
            await self.engine.action('P','reconfigure_agent',{'target_agent_id':self.ceo.id,'model':'test/new','harness':'direct_api'},worker.id)
        await self.engine.disconnect_agents('P',worker.id,self.ceo.id)
        self.assertEqual(worker.parent_id,self.ceo.id)

    async def test_history_pagination_and_position_reset(self):
        for n in range(1100):self.engine.store.event(self.ceo.id,{'number':n})
        first=self.engine.store.events(self.ceo.id,0)
        second=self.engine.store.events(self.ceo.id,first[-1]['id'])
        third=self.engine.store.events(self.ceo.id,second[-1]['id'])
        self.assertEqual(len(first+second+third),1100)
        self.assertEqual(first[0]['number'],0)
        await self.engine.set_agent_position('P',self.ceo.id,200,300)
        await self.engine.set_agent_position('P',self.ceo.id,None,None)
        self.assertIsNone(self.ceo.position)
        with self.assertRaises(ValueError):await self.engine.set_agent_position('P',self.ceo.id,float('nan'),1)

    async def test_sync_is_read_only_and_invalid_paths_are_rejected(self):
        self.google()
        a=self.engine.auth_pool.accounts['one']
        before=(self.engine.auth_pool.resolve_auth_dir(a)/'credential.dat').read_bytes()
        self.engine.auth_pool.sync_accounts()
        self.assertEqual((self.engine.auth_pool.resolve_auth_dir(a)/'credential.dat').read_bytes(),before)
        with self.assertRaises(ValueError):self.engine.auth_pool.register_account('X',account_id='../escape')
        self.assertFalse(self.engine.auth_pool.classify_error(RuntimeError('429 MODEL_CAPACITY_EXHAUSTED'))[0])

    async def test_missing_or_forced_excluded_account_is_not_selected(self):
        self.google()
        (self.engine.auth_pool.resolve_auth_dir(self.engine.auth_pool.accounts['one'])/'credential.dat').unlink()
        slot=self.engine.auth_pool.acquire_slot(self.ceo)
        self.assertEqual(slot.account_id,'two')
        self.engine.auth_pool.release_slot('two')
        self.ceo.forced_auth_slot_id='two'
        with self.assertRaises(ValueError):self.engine.auth_pool.acquire_slot(self.ceo,exclude=['two'])

    @unittest.skipUnless(os.name=='nt','Windows credential storage')
    async def test_credential_encryption_round_trip_and_rolling_usage(self):
        from core.credential_store import save,unprotect,PREFIX
        self.google();account=self.engine.auth_pool.accounts['one']
        path=self.engine.auth_pool.resolve_auth_dir(account)/'credential.dat'
        secret=b'{"refresh_token":"synthetic-not-a-real-token"}'
        save(path,secret)
        self.assertTrue(path.read_bytes().startswith(PREFIX))
        self.assertNotIn(secret,path.read_bytes())
        self.assertEqual(unprotect(path.read_bytes()),secret)
        account.usage_5h={'observed_turns':[0],'turns':1}
        self.assertEqual(self.engine.auth_pool.public()[0]['usage_5h']['turns'],0)

    async def test_cli_split_utf8_remains_intact(self):
        runner=CLIRunner(SystemSettings());events=[]
        async def emit(kind,data): events.append((kind,data))
        # Cyrillic byte sequences deliberately split across subprocess writes.
        script="import os,time; b='Ўзбек'.encode(); os.write(1,b[:1]);time.sleep(.1);os.write(1,b[1:])"
        result=await runner._process('unicode',[sys.executable,'-c',script],self.root,emit)
        self.assertEqual(result,'Ўзбек')
        self.assertEqual(''.join(d['text'] for k,d in events if k=='output'),'Ўзбек')

    async def test_owner_only_console_and_shared_reconfigure_http_action(self):
        import httpx
        from web.app import create_app
        self.cli.capabilities=lambda:[]
        app=create_app(self.engine,'test-owner')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
            response=await client.get('/api/catalog',headers={'Authorization':'Bearer test-owner'})
            self.assertEqual(response.status_code,200)
            response=await client.post('/api/agents/'+self.ceo.id+'/console',headers={'Authorization':'Bearer '+self.engine.token_for(self.ceo.id)})
            self.assertEqual(response.status_code,403)
            response=await client.post('/api/action',headers={'Authorization':'Bearer test-owner'},json={
                'project_name':'P','action':'reconfigure_agent','arguments':{
                    'target_agent_id':self.ceo.id,'model':'test/next','harness':'direct_api'}})
            self.assertEqual(response.status_code,200)
            self.assertEqual(self.ceo.model,'test/next')

    async def test_login_reservation_blocks_new_google_turns_and_cancel_restores_vault(self):
        self.google()
        with patch('harness.cli_runner.CLIRunner.resolve',return_value=['fake-agy.exe']):
            self.engine.auth_pool.prepare_login_environment('one')
        self.assertIsNone(self.vault)
        self.assertFalse(self.engine.auth_pool.login_done.is_set())
        with self.assertRaises(ValueError):
            self.engine.auth_pool.prepare_login_environment('two')
        self.engine.auth_pool.cancel_login()
        self.assertEqual(self.vault,b'original')
        self.assertTrue(self.engine.auth_pool.login_done.is_set())

    async def test_corrupt_account_registry_is_not_silently_overwritten(self):
        from core.auth_pool import GoogleAuthPool
        path=self.root/'google_accounts.json'
        path.write_text('{broken',encoding='utf-8')
        with self.assertRaises(RuntimeError):GoogleAuthPool(self.root)
        self.assertEqual(path.read_text(encoding='utf-8'),'{broken')
