"""Regression checks for trust boundaries, live auth leases and rejected writes."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from core.auth_pool import WindowsKeyringHelper
from core.config import SystemSettings
from core.service import validate_endpoint
from engine.models import AgentStatus, Role
from engine.orchestrator import Orchestrator
from mcp_server import server
from web.app import create_app


class BackendAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Orchestrator(Path(self.temp.name), SystemSettings(),
            cli=SimpleNamespace(capabilities=lambda: {}),
            runner=SimpleNamespace(generate_response=AsyncMock(return_value='Done')))
        await self.engine.create_project('Audit', ceo_model='test/ceo')
        self.ceo = self.engine.get_ceo('Audit')
        self.manager = self.engine._add_agent('Audit', 'Manager', Role.MANAGER,
            'test/manager', parent_id=self.ceo.id)
        self.worker = self.engine._add_agent('Audit', 'Worker', Role.WORKER,
            'test/worker', parent_id=self.manager.id)

    async def asyncTearDown(self):
        await self.engine.close()
        self.temp.cleanup()

    async def test_account_preference_does_not_relabel_executing_lease(self):
        self.engine.auth_pool.register_account('test@example.test', account_id='next')
        self.worker.auth_slot_id = 'executing'
        self.worker.status = AgentStatus.WORKING
        await self.engine.action('Audit', 'force_agent_auth',
            {'target_agent_id': self.worker.id, 'account_id': 'next'}, self.manager.id)
        self.assertEqual(self.worker.auth_slot_id, 'executing')
        self.assertEqual(self.worker.forced_auth_slot_id, 'next')
        await self.engine.action('Audit', 'force_agent_auth',
            {'target_agent_id': self.worker.id, 'account_id': 'auto'})
        self.assertEqual(self.worker.auth_slot_id, 'executing')
        self.assertIsNone(self.worker.forced_auth_slot_id)

    async def test_failed_credential_read_releases_lease_without_overwriting_vault(self):
        with patch.object(WindowsKeyringHelper, 'read_credential', side_effect=OSError('synthetic vault failure')), \
             patch.object(WindowsKeyringHelper, 'write_credential') as write, \
             patch.object(WindowsKeyringHelper, 'delete_credential') as delete:
            with self.assertRaises(OSError):
                async with self.engine.auth_pool.credential_lease():
                    self.fail('Lease must not be granted after a failed credential read')
            self.assertFalse(self.engine.auth_pool.credential_lock.locked())
            write.assert_not_called()
            delete.assert_not_called()

    async def test_manager_cannot_rebind_or_resume_ceo(self):
        self.ceo.status = AgentStatus.PAUSED
        self.engine.paused[self.ceo.id] = {'content': 'keep assignment'}
        for action in ('force_agent_auth', 'resume_agent'):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                await self.engine.action('Audit', action,
                    {'target_agent_id': self.ceo.id}, self.manager.id)
        self.assertEqual(self.engine.paused[self.ceo.id]['content'], 'keep assignment')

    async def test_self_resume_does_not_discard_paused_assignment(self):
        self.engine.paused[self.manager.id] = {'content': 'keep assignment'}
        with self.assertRaises(PermissionError):
            await self.engine.action('Audit', 'resume_agent',
                {'target_agent_id': self.manager.id}, self.manager.id)
        self.assertIn(self.manager.id, self.engine.paused)

    async def test_rejected_task_edit_is_atomic(self):
        self.ceo.current_task = 'Original'
        app = create_app(self.engine, 'synthetic-owner')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url='http://testserver', headers={'Authorization': 'Bearer synthetic-owner'}) as client:
            for body in ({'name': 'Unsupported rename', 'task_description': 'Wrong'},
                         {'task_description': {'not': 'text'}}):
                response = await client.post(f'/api/agents/{self.ceo.id}/task', json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.ceo.current_task, 'Original')

    async def test_models_get_availability_owner_retains_identity(self):
        self.engine.auth_pool.register_account('private@example.test', account_id='slot')
        for action, key in (('list_google_accounts', 'accounts'), ('list_capabilities', 'google_auth_pool')):
            public = await self.engine.action('Audit', action, {}, self.worker.id)
            self.assertEqual(public[key][0]['account_id'], 'slot')
            self.assertEqual(public[key][0]['health_state'], 'unknown')
            self.assertNotIn('private@example.test', json.dumps(public))
            self.assertNotIn('auth_dir', public[key][0])
            owner = await self.engine.action('Audit', action, {})
            self.assertIn('private@example.test', json.dumps(owner))

    async def test_optional_links_do_not_start_or_gate_message_delivery(self):
        with patch.object(self.engine, 'schedule') as schedule:
            await self.engine.connect_agents('Audit', self.manager.id, self.worker.id)
            schedule.assert_not_called()
            await self.engine.disconnect_agents('Audit', self.manager.id, self.worker.id)
            message = await self.engine.send('Audit', self.worker.id, 'Work', self.manager.id)
            self.assertEqual(message.sender_id, self.manager.id)
            self.assertEqual(self.engine.inboxes[self.worker.id][0]['content'], 'Work')
            schedule.assert_called_once_with(self.worker.id)
        self.assertEqual(self.worker.parent_id, self.manager.id)

    async def test_scoped_agent_cannot_read_owner_transcript(self):
        token = self.engine.token_for(self.worker.id)
        app = create_app(self.engine, 'synthetic-owner')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url='http://testserver', headers={'Authorization': 'Bearer ' + token}) as client:
            for resource in ('events', 'messages'):
                response = await client.get(f'/api/agents/{self.ceo.id}/{resource}')
                self.assertEqual(response.status_code, 403)


class ProxyAuditTests(unittest.IsolatedAsyncioTestCase):
    def test_endpoint_rejects_userinfo_and_remote_targets(self):
        self.assertEqual(validate_endpoint('http://127.0.0.1:8765'), 'http://127.0.0.1:8765')
        for url in ('http://127.0.0.1:8765@remote.test', 'http://127.0.0.1:8765/path',
                    'http://127.0.0.1:8765?x=1', 'http://127.0.0.1:8765#token',
                    'http://127.0.0.1:99999', 'http://remote.test:8765', None):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_endpoint(url)

    async def test_uncertain_write_is_not_replayed_and_cached_endpoint_is_cleared(self):
        failure = httpx.ReadTimeout('synthetic timeout')
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.request.side_effect = failure
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(server, '_info', {'url': 'http://127.0.0.1:8765', 'token': 'synthetic'}), \
             patch.object(server.httpx, 'AsyncClient', return_value=client):
            with self.assertRaisesRegex(ValueError, 'outcome may be unknown'):
                await server.request('POST', '/api/action', {})
            self.assertEqual(client.request.await_count, 1)
            self.assertIsNone(server._info)


if __name__ == '__main__':
    unittest.main()
