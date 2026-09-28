"""Quota recovery regressions using real child processes and synthetic native logs."""
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.auth_pool import GoogleAuthPool, WindowsKeyringHelper
from core.config import SystemSettings
from engine.models import AgentNode, Role, HarnessType
from harness.cli_runner import CLIRunner


class GoogleQuotaRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pool = GoogleAuthPool(self.root)
        self.runner = CLIRunner(SystemSettings(cli_auth_enabled={'agy': True}, request_timeout_seconds=10),
                                google_projects_dir=self.root/'projects')
        self.agent = AgentNode(project_name='Test', name='Manager', role=Role.MANAGER,
            model='antigravity/gemini-3.8-flash-high', harness=HarnessType.ANTIGRAVITY,
            working_dir=str(self.root), session_id='retained-session')
        self.account = {'account_id': 'two', 'email': 'two@example.test'}
        self.events = []

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def emit(self, kind, data):
        self.events.append((kind, data))

    async def run_cli(self, body, **kwargs):
        script = self.root/'native.py'
        script.write_text('import sys,json,time\nfrom pathlib import Path\n'
            'log=Path(sys.argv[sys.argv.index("--log-file")+1])\n'
            'sys.stdin.readline()\n'+body, encoding='utf-8')
        with patch.object(self.runner, 'resolve', return_value=[sys.executable, str(script)]):
            return await self.runner.execute_task(self.agent, 'Continue', self.root, self.emit,
                endpoint='http://127.0.0.1:8765', token='synthetic', allow_commands=True,
                auth_dir=self.root, google_account=self.account, **kwargs)

    def test_real_google_reset_duration(self):
        for duration, expected in [('1h56m53s',7013),('2 hours 3 minutes',7380),('41m',2460),('300s',300),('105h53m20s',18000),('48h37m38s',18000)]:
            with self.subTest(duration=duration):
                retry, seconds, reset = self.pool.classify_error(RuntimeError(
                    'RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in '+duration+'.'))
                self.assertTrue(retry)
                self.assertEqual(seconds,expected)
                self.assertIsNotNone(reset)
        self.assertFalse(self.pool.classify_error(RuntimeError('429 MODEL_CAPACITY_EXHAUSTED'))[0])

    def test_wrong_saved_identity_never_written(self):
        profile=self.pool.register_account('two@example.test',account_id='two')
        claims=base64.urlsafe_b64encode(b'{"email":"wrong@example.test"}').decode()
        (self.pool.resolve_auth_dir(profile)/'credential.dat').write_text(
            json.dumps({'id_token':'test.'+claims+'.test'}))
        with patch.object(WindowsKeyringHelper,'write_credential') as write:
            with self.assertRaisesRegex(RuntimeError,'identity does not match'):
                self.pool.activate_account_credential('two')
            write.assert_not_called()

    async def test_first_native_quota_retry_stops_child_without_waiting(self):
        started=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'Resets in 1h56m53s'):
            await self.run_cli('''log.write_text("I0924 14:43:32.0 1 server_oauth.go:196] applyAuthResult: email=two@example.test, authMethod=consumer\\n"
"I0924 14:43:32.1 1 run.go:395] Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1h56m53s.), retrying in 4s\\n")
time.sleep(30)
''')
        self.assertLess(time.monotonic()-started,5)
        self.assertFalse(self.runner.active_processes)
        self.assertIn('auth_verified',[kind for kind,_ in self.events])
        self.assertIn('auth_quota_detected',[kind for kind,_ in self.events])

    async def test_actual_cli_identity_mismatch_is_stopped(self):
        with self.assertRaisesRegex(RuntimeError,'different account'):
            await self.run_cli('''log.write_text("I0924 14:43:32.0 1 server_oauth.go:196] applyAuthResult: email=wrong@example.test, authMethod=consumer\\n")
time.sleep(30)
''')
        self.assertFalse(self.runner.active_processes)
        self.assertNotIn('auth_verified',[kind for kind,_ in self.events])

    async def test_old_log_and_quoted_tool_error_do_not_trigger_failover(self):
        logs=self.root/'.gemini/antigravity-cli/log'
        logs.mkdir(parents=True)
        (logs/'old.log').write_text('run.go:395] Run: attempt 1 failed (Individual quota reached.)')
        result=await self.run_cli('''log.write_text("I0924 14:43:32.0 1 server_oauth.go:196] applyAuthResult: email=two@example.test, authMethod=consumer\\n")
print(json.dumps({'event':'step_update','step_update':{'step_type':'tool','text_delta':'Individual quota reached.'}}),flush=True)
print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'Done'}}),flush=True)
''')
        self.assertEqual(result,'Done')

    async def test_structured_error_survives_nonzero_exit_and_output_tail(self):
        with self.assertRaisesRegex(RuntimeError,'Resets in 1h56m53s'):
            await self.run_cli('''log.write_text("I0924 14:43:32.0 1 server_oauth.go:196] applyAuthResult: email=two@example.test, authMethod=consumer\\n")
print(json.dumps({'event':'result','result':{'status':'ERROR','error':'Individual quota reached. Resets in 1h56m53s.'}}),flush=True)
print('padding'*1000,flush=True)
sys.exit(3)
''')

    async def test_stale_quota_with_fresh_response_retains_session(self):
        error='Individual quota reached. Resets in 1h56m53s.'
        body='''log.write_text("I0924 14:43:32.0 1 server_oauth.go:196] applyAuthResult: email=two@example.test, authMethod=consumer\\n")
print(json.dumps({'event':'step_update','step_update':{'step_type':'agent_response','state':'DONE','conversation_id':'retained-session'}}),flush=True)
print(json.dumps({'event':'result','result':{'status':'ERROR','response':'New completed response','error':ERROR}}),flush=True)
'''.replace('ERROR}',repr(error)+'}')
        result=await self.run_cli(body, previous_google_quota=error)
        self.assertEqual(result,'New completed response')
        self.assertEqual(self.agent.session_id,'retained-session')
        self.assertIn('auth_stale_quota_ignored',[k for k,_ in self.events])
        with self.assertRaisesRegex(RuntimeError,'Individual quota reached'):
            await self.run_cli(body)
