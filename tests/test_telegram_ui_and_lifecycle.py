"""Unit and integration tests for Telegram UI Connection, Bridge Lifecycle Supervisor, and Storage Cleanup UI."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from core.config import SystemSettings
from core.telegram_supervisor import TelegramSupervisor, mask_token
from engine.orchestrator import Orchestrator
from web.app import create_app


class TelegramSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.supervisor = TelegramSupervisor(data_dir=self.data_dir)

    def tearDown(self):
        self.supervisor.stop_daemon()
        self.temp_dir.cleanup()

    def test_mask_token(self):
        self.assertEqual(mask_token(''), '')
        self.assertEqual(mask_token(None), '')
        self.assertEqual(mask_token('short'), '******')
        token = '1234567890:ABCdefGHIjklMNOpqrsTUVwxyz1234567'
        masked = mask_token(token)
        self.assertTrue(masked.startswith('1234****'))
        self.assertTrue(masked.endswith('****4567'))
        self.assertNotIn('ABCdefGHIjklMNOpqrsTUVwxyz', masked)

    def test_save_and_load_config(self):
        cfg = {
            'bot_token': '987654321:FakeTokenSecretPayload123',
            'enabled': True,
            'bot_handle': '@BonsaiTestBot'
        }
        self.supervisor._save_config(cfg)
        loaded = self.supervisor._load_config()
        self.assertEqual(loaded.get('bot_token'), '987654321:FakeTokenSecretPayload123')
        self.assertEqual(loaded.get('bot_handle'), '@BonsaiTestBot')
        self.assertTrue(loaded.get('enabled'))

    def test_status_masks_token_and_reports_state(self):
        cfg = {
            'bot_token': '987654321:FakeTokenSecretPayload123',
            'enabled': True,
            'bot_handle': '@BonsaiTestBot'
        }
        self.supervisor._save_config(cfg)
        status = self.supervisor.get_status()
        self.assertTrue(status['has_token'])
        self.assertNotIn('FakeTokenSecretPayload123', status['masked_token'])
        self.assertEqual(status['bot_handle'], '@BonsaiTestBot')
        self.assertEqual(status['connection_state'], 'disconnected')

    @patch('urllib.request.urlopen')
    def test_fetch_bot_info(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            'ok': True,
            'result': {'id': 123456, 'username': 'BonsaiAIBot', 'first_name': 'Bonsai'}
        }).encode('utf-8')
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        handle = self.supervisor.fetch_bot_info('123456:TestToken')
        self.assertEqual(handle, '@BonsaiAIBot')

    @patch('subprocess.Popen')
    def test_start_and_stop_daemon(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        bridge_file = Path(self.temp_dir.name) / 'shared' / 'bridge' / 'telegram_bridge.py'
        bridge_file.parent.mkdir(parents=True, exist_ok=True)
        bridge_file.write_text('# dummy bridge', encoding='utf-8')

        cfg = {'bot_token': '123456:TestToken', 'enabled': True}
        self.supervisor._save_config(cfg)
        started = self.supervisor.start_daemon(project_dir=Path(self.temp_dir.name))
        self.assertTrue(started)
        self.assertEqual(self.supervisor.process.pid, 99999)
        self.assertTrue(self.supervisor.is_running())

        # Test stop
        stopped = self.supervisor.stop_daemon()
        self.assertTrue(stopped)
        self.assertFalse(self.supervisor.is_running())


class WebTelegramAndStorageApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Orchestrator(
            Path(self.temp.name),
            SystemSettings(),
            cli=SimpleNamespace(capabilities=lambda: {}),
            runner=SimpleNamespace(generate_response=AsyncMock(return_value='Done'))
        )
        await self.engine.create_project('Bonsai_Sauce_Qwen3.5-2B', ceo_model='test/ceo')
        self.auth_token = 'owner-secret-key-xyz'
        self.app = create_app(self.engine, self.auth_token)

    async def asyncTearDown(self):
        await self.engine.close()
        self.temp.cleanup()

    async def test_storage_status_endpoint(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url='http://testserver',
            headers={'Authorization': f'Bearer {self.auth_token}'}
        ) as client:
            resp = await client.get('/api/storage/status')
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data['available'])
            self.assertEqual(data['approval_state'], 'pending_human_owner_approval')
            self.assertIn('reclaimable_space', data)
            self.assertIn('protected_core_assets', data)
            self.assertGreater(data['reclaimable_space']['total_immediate_safe_reclaim_gb'], 0)
            self.assertGreater(data['protected_core_assets']['protected_gb'], 0)

    async def test_storage_action_endpoint(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url='http://testserver',
            headers={'Authorization': f'Bearer {self.auth_token}'}
        ) as client:
            # 1. Clean safe items -> pending owner approval
            resp = await client.post('/api/storage/action', json={'action': 'clean_safe'})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data['ok'])
            self.assertEqual(data['status'], 'pending_approval')
            self.assertEqual(data['target_gb'], 99.15)

            # 2. Move cold files -> pending owner approval
            resp = await client.post('/api/storage/action', json={'action': 'move_cold'})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data['ok'])
            self.assertEqual(data['status'], 'pending_approval')
            self.assertEqual(data['target_gb'], 56.68)

            # 3. Cancel
            resp = await client.post('/api/storage/action', json={'action': 'cancel'})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()['ok'])
            self.assertEqual(resp.json()['status'], 'idle')

    async def test_telegram_status_unauthorized(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url='http://testserver'
        ) as client:
            resp = await client.get('/api/telegram/status')
            self.assertEqual(resp.status_code, 401)

    async def test_telegram_lifecycle_endpoints(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url='http://testserver',
            headers={'Authorization': f'Bearer {self.auth_token}'}
        ) as client:
            # 1. Get initial status
            resp = await client.get('/api/telegram/status')
            self.assertEqual(resp.status_code, 200)
            status = resp.json()
            self.assertIn('connection_state', status)
            self.assertIn('masked_token', status)

            # 2. Connect with token
            with patch('core.telegram_supervisor.TelegramSupervisor.connect') as mock_connect:
                mock_connect.return_value = {
                    'connection_state': 'pairing',
                    'bot_handle': '@BonsaiTestBot',
                    'pairing_code': 'BONSAI-TEST01',
                    'has_token': True,
                    'masked_token': '1234****:****789'
                }
                resp = await client.post('/api/telegram/connect', json={'bot_token': '123456:SecretTokenPayload789'})
                self.assertEqual(resp.status_code, 200)
                connect_data = resp.json()
                self.assertEqual(connect_data['connection_state'], 'pairing')
                self.assertEqual(connect_data['bot_handle'], '@BonsaiTestBot')

            # 3. Test ping message
            with patch('core.telegram_supervisor.TelegramSupervisor.send_test_message', return_value={'ok': True, 'detail': 'Test message sent'}):
                resp = await client.post('/api/telegram/test', json={})
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.json()['ok'])

            # 4. Restart daemon
            with patch('core.telegram_supervisor.TelegramSupervisor.restart_daemon', return_value=True), \
                 patch('core.telegram_supervisor.TelegramSupervisor.get_status') as mock_status:
                mock_status.return_value = {
                    'connection_state': 'connected',
                    'bot_handle': '@BonsaiTestBot',
                    'bridge_pid': 12345
                }
                resp = await client.post('/api/telegram/restart', json={})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()['bridge_pid'], 12345)

            # 5. Disconnect daemon
            with patch('core.telegram_supervisor.TelegramSupervisor.disconnect') as mock_disconnect:
                mock_disconnect.return_value = {
                    'connection_state': 'disconnected',
                    'bot_handle': None,
                    'paired_user_handle': None
                }
                resp = await client.post('/api/telegram/disconnect', json={})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()['connection_state'], 'disconnected')


if __name__ == '__main__':
    unittest.main()
