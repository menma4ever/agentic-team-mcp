import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.auth_pool import GoogleAuthPool, GoogleAccountHealth, GoogleAccountProfile
from core.config import SystemSettings, ROOT
from engine.models import AgentNode, Role, HarnessType, AgentStatus
from engine.orchestrator import Orchestrator
from harness.cli_runner import CLIRunner


class AuthPoolTests(unittest.IsolatedAsyncioTestCase):
    def test_launch_errors_do_not_poison_account_health(self):
        account=self.pool.register_account('test@example.invalid',account_id='test')
        account.health_state=GoogleAccountHealth.HEALTHY
        self.pool.mark_error(account.account_id,'invalid model selection: requires --effort',is_fatal=True)
        self.assertEqual(account.health_state,GoogleAccountHealth.HEALTHY)
        self.assertIsNone(account.last_error)
        self.assertIn('requires --effort',account.last_run_error)
        self.pool.mark_error(account.account_id,'401 unauthorized: invalid credentials')
        self.assertEqual(account.health_state,GoogleAccountHealth.ERROR)
        self.assertIn('unauthorized',account.last_error)

    def test_legacy_model_flag_error_is_reclassified_without_claiming_login_success(self):
        account=self.pool.register_account('test@example.invalid',account_id='test')
        account.health_state=GoogleAccountHealth.ERROR
        account.last_error='invalid model selection: requires --effort'
        self.pool.save()
        loaded=GoogleAuthPool(self.root).accounts['test']
        self.assertEqual(loaded.health_state,GoogleAccountHealth.UNKNOWN)
        self.assertIsNone(loaded.last_error)
        self.assertIn('requires --effort',loaded.last_run_error)

    async def asyncSetUp(self):
        temp_root = ROOT / 'tests' / '.tmp'
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name).resolve()
        self.config = SystemSettings(cli_auth_enabled={'agy': True})
        self.pool = GoogleAuthPool(self.root)
        for method, value in [('read_credential',None),('write_credential',True),('delete_credential',True)]:
            patcher=patch('core.auth_pool.WindowsKeyringHelper.'+method,return_value=value)
            patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(GoogleAuthPool,'has_credential',return_value=True)
        patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(GoogleAuthPool,'activate_account_credential',return_value=True)
        patcher.start();self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def test_account_registry_crud_and_persistence(self):
        # 1. Register Account A
        acc_a = self.pool.register_account('user_a@example.test', account_id='account_a', max_concurrent=3)
        self.assertEqual(acc_a.account_id, 'account_a')
        self.assertEqual(acc_a.email, 'user_a@example.test')
        self.assertEqual(acc_a.max_concurrent, 3)
        self.assertEqual(acc_a.health_state, GoogleAccountHealth.UNKNOWN)

        # 2. Check persistence in google_accounts.json
        reg_file = self.root / 'google_accounts.json'
        self.assertTrue(reg_file.exists())
        data = json.loads(reg_file.read_text(encoding='utf-8'))
        self.assertEqual(len(data['accounts']), 1)
        self.assertEqual(data['accounts'][0]['account_id'], 'account_a')
        # Check that no plaintext password field exists
        self.assertNotIn('password', json.dumps(data).lower())

        # 3. Reload into new pool instance
        pool2 = GoogleAuthPool(self.root)
        self.assertIn('account_a', pool2.accounts)
        self.assertEqual(pool2.accounts['account_a'].email, 'user_a@example.test')

        # 4. Disable and enable
        self.pool.disable_account('account_a')
        self.assertEqual(self.pool.accounts['account_a'].health_state, GoogleAccountHealth.DISABLED)
        self.pool.enable_account('account_a')
        self.assertEqual(self.pool.accounts['account_a'].health_state, GoogleAccountHealth.UNKNOWN)

        # 5. Remove account
        self.pool.remove_account('account_a')
        self.assertNotIn('account_a', self.pool.accounts)
        pool3 = GoogleAuthPool(self.root)
        self.assertNotIn('account_a', pool3.accounts)

    def test_directory_isolation(self):
        acc_a = self.pool.register_account('user_a@example.test', account_id='account_a')
        acc_b = self.pool.register_account('user_b@example.test', account_id='account_b')

        dir_a = self.pool.resolve_auth_dir(acc_a)
        dir_b = self.pool.resolve_auth_dir(acc_b)

        self.assertNotEqual(dir_a, dir_b)
        self.assertTrue((dir_a / '.gemini' / 'antigravity-cli' / 'settings.json').exists())
        self.assertTrue((dir_b / '.gemini' / 'antigravity-cli' / 'settings.json').exists())

        # Test CLI environment isolation
        runner = CLIRunner(self.config)
        agent_a = AgentNode(project_name='Proj', name='WorkerA', role=Role.WORKER,
                            model='antigravity/gemini-3.1-pro-high', harness=HarnessType.ANTIGRAVITY)
        agent_b = AgentNode(project_name='Proj', name='WorkerB', role=Role.WORKER,
                            model='antigravity/gemini-3.1-pro-high', harness=HarnessType.ANTIGRAVITY)

        with patch.object(runner, 'resolve', return_value=['agy.exe']):
            _, env_a, _ = runner.build(agent_a, 'task a', str(self.root), 'http://127.0.0.1:8765', 'tok_a', auth_dir=dir_a)
            _, env_b, _ = runner.build(agent_b, 'task b', str(self.root), 'http://127.0.0.1:8765', 'tok_b', auth_dir=dir_b)

        self.assertEqual(env_a['USERPROFILE'], str(dir_a))
        self.assertEqual(env_b['USERPROFILE'], str(dir_b))
        self.assertEqual(env_a['ANTIGRAVITY_APP_DATA_DIR'], str(dir_a / '.gemini' / 'antigravity'))
        self.assertEqual(env_b['ANTIGRAVITY_APP_DATA_DIR'], str(dir_b / '.gemini' / 'antigravity'))
        self.assertNotEqual(env_a['USERPROFILE'], env_b['USERPROFILE'])

    def test_error_classification_retryable_vs_non_retryable(self):
        # Retryable: RESOURCE_EXHAUSTED with "reset in 41m"
        err1 = Exception("RESOURCE_EXHAUSTED: Quota exceeded for gemini-3.1-pro. Reset in 41m.")
        is_ret1, secs1, iso1 = self.pool.classify_error(err1)
        self.assertTrue(is_ret1)
        self.assertEqual(secs1, 41 * 60)
        self.assertIsNotNone(iso1)

        # Retryable: 429 Too Many Requests with retry-after
        err2 = Exception("HTTP 429: Too Many Requests. retry-after: 300")
        is_ret2, secs2, iso2 = self.pool.classify_error(err2)
        self.assertTrue(is_ret2)
        self.assertEqual(secs2, 300)

        # Retryable: generic rate limit
        err3 = Exception("Rate limit exceeded for current billing profile")
        is_ret3, secs3, iso3 = self.pool.classify_error(err3)
        self.assertTrue(is_ret3)
        self.assertEqual(secs3, 1800)

        # Non-retryable: Permission Denied
        err4 = Exception("Permission denied: user does not have access to this resource")
        is_ret4, _, _ = self.pool.classify_error(err4)
        self.assertFalse(is_ret4)

        # Non-retryable: Authentication Revoked
        err5 = Exception("Authentication revoked or invalid_grant")
        is_ret5, _, _ = self.pool.classify_error(err5)
        self.assertFalse(is_ret5)

        # Non-retryable: Malformed request
        err6 = Exception("Invalid argument: model parameter is malformed")
        is_ret6, _, _ = self.pool.classify_error(err6)
        self.assertFalse(is_ret6)

    def test_scheduler_scoring_and_concurrency(self):
        acc_a = self.pool.register_account('user_a@example.test', account_id='account_a', max_concurrent=2)
        acc_b = self.pool.register_account('user_b@example.test', account_id='account_b', max_concurrent=2)

        agent1 = AgentNode(project_name='P', name='A1', role=Role.WORKER, model='antigravity/gemini-3.1-pro-high')
        agent2 = AgentNode(project_name='P', name='A2', role=Role.WORKER, model='antigravity/gemini-3.1-pro-high')
        agent3 = AgentNode(project_name='P', name='A3', role=Role.WORKER, model='antigravity/gemini-3.1-pro-high')

        # First acquisition: picks account_a (0 active vs 0 active)
        slot1 = self.pool.acquire_slot(agent1)
        self.assertEqual(slot1.account_id, 'account_a')
        self.assertEqual(slot1.active_agents, 1)

        # Second acquisition: picks account_b because account_a has 1 active
        slot2 = self.pool.acquire_slot(agent2)
        self.assertEqual(slot2.account_id, 'account_b')
        self.assertEqual(slot2.active_agents, 1)

        # Third acquisition: both have 1 active, picks account_a (under max 2)
        slot3 = self.pool.acquire_slot(agent3)
        self.assertEqual(slot3.account_id, 'account_a')
        self.assertEqual(slot3.active_agents, 2)

        # If account_a is quota-blocked, scheduler skips it
        self.pool.mark_quota_blocked('account_a', 'Quota exceeded', cooldown_seconds=3600)
        self.assertTrue(acc_a.is_cooldown_active())

        # Next acquisition should pick account_b only
        agent4 = AgentNode(project_name='P', name='A4', role=Role.WORKER, model='antigravity/gemini-3.1-pro-high')
        slot4 = self.pool.acquire_slot(agent4)
        self.assertEqual(slot4.account_id, 'account_b')

    def test_sticky_session_affinity_preserves_prompt_cache(self):
        """Verify that an agent sticks to its assigned account across turns to preserve Google's KV/prompt cache."""
        acc_a = self.pool.register_account('user_a@example.test', account_id='account_a', max_concurrent=4)
        acc_b = self.pool.register_account('user_b@example.test', account_id='account_b', max_concurrent=4)

        agent = AgentNode(project_name='P', name='Worker1', role=Role.WORKER, model='antigravity/gemini-3.1-pro-high')

        # Turn 1: Initial assignment picks account_a
        slot1 = self.pool.acquire_slot(agent)
        self.assertEqual(slot1.account_id, 'account_a')
        agent.auth_slot_id = slot1.account_id

        # Turn 1 completes: marks success, releases slot
        self.pool.mark_success(slot1.account_id)
        self.pool.release_slot(slot1.account_id)

        # Now account_a has 1 turn, while account_b has 0 turns.
        # Without sticky affinity, the scheduler would switch to account_b (busting the prompt cache).
        # With sticky affinity, the agent STAYS on account_a!
        slot2 = self.pool.acquire_slot(agent)
        self.assertEqual(slot2.account_id, 'account_a', "Agent must stick to account_a across turns for cache reuse")
        self.pool.release_slot(slot2.account_id)

        # Turn 3: Continues to stick to account_a
        slot3 = self.pool.acquire_slot(agent)
        self.assertEqual(slot3.account_id, 'account_a')
        self.pool.release_slot(slot3.account_id)

        # But if account_a hits quota exhaustion, it fails over to account_b
        self.pool.mark_quota_blocked('account_a', 'Quota exceeded', cooldown_seconds=1800)
        slot_failover = self.pool.acquire_slot(agent, exclude=['account_a'])
        self.assertEqual(slot_failover.account_id, 'account_b')
        agent.auth_slot_id = slot_failover.account_id

        # Subsequent turns stick to account_b
        self.pool.release_slot(slot_failover.account_id)
        slot_next = self.pool.acquire_slot(agent)
        self.assertEqual(slot_next.account_id, 'account_b')
        self.pool.release_slot(slot_next.account_id)

    async def test_seamless_failover_and_continuity(self):
        """Continuity test:
        1. Start manager on Account A.
        2. Create workspace state / artifacts.
        3. Simulate Account A quota exhaustion (RESOURCE_EXHAUSTED).
        4. Scheduler selects Account B.
        5. Manager resumes.
        6. Verify same workspace/state and pending task.
        7. Verify no duplicated worker/task execution.
        """
        # Register Account A and Account B
        acc_a = self.pool.register_account('user_a@example.test', account_id='account_a')
        acc_b = self.pool.register_account('user_b@example.test', account_id='account_b')

        engine = Orchestrator(self.root, self.config)
        try:
            p = await engine.create_project('ContinuityProject', ceo_model='antigravity/gemini-3.1-pro-high',
                                            harness='antigravity', allow_commands=True)

            executed_accounts = []
            turn_count = 0

            async def mock_execute_task(agent, task_prompt, working_dir, emit, endpoint='', token='', allow_commands=False, auth_dir=None, **kwargs):
                nonlocal turn_count
                turn_count += 1
                current_auth = agent.auth_slot_id
                if 'account_a' in current_auth:
                    executed_accounts.append('account_a')
                    # 3. Simulate Account A hitting quota exhaustion
                    raise RuntimeError("RESOURCE_EXHAUSTED: Individual quota reached for Account A. Reset in 30m.")
                elif 'account_b' in current_auth:
                    executed_accounts.append('account_b')
                    engine.decisions[agent.id] = 'completed'
                    # Account B succeeds and continues
                    agent.session_id = 'session-resumed-under-b'
                    # Verify artifact from Account A is still present in the workspace
                    notes = (Path(working_dir) / 'research_notes.txt').read_text(encoding='utf-8')
                    (Path(working_dir) / 'research_notes.txt').write_text(notes + '\nContinued research under Account B', encoding='utf-8')
                    return "Completed research under Account B without losing progress."
                raise RuntimeError(f"Unexpected auth_dir: {current_auth}")

            with patch.object(engine.cli, 'execute_task', side_effect=mock_execute_task):
                # Create manager (automatically queues task and starts execution)
                manager = await engine.create_manager('ContinuityProject', name='Manager',
                                                      model='antigravity/gemini-3.1-pro-high',
                                                      task_description='Manage the research workflow',
                                                      harness='antigravity')
                self.assertEqual(manager.role, Role.MANAGER)
                manager_dir = Path(manager.working_dir)

                # 2. Create state/work artifacts in manager's working dir
                artifact_file = manager_dir / 'research_notes.txt'
                artifact_file.write_text('Initial research findings on Account A', encoding='utf-8')

                # Wait for turn completion
                for _ in range(50):
                    if manager.status in (AgentStatus.IDLE, AgentStatus.PAUSED, AgentStatus.FAILED):
                        break
                    await asyncio.sleep(0.1)

            # 4. Verify Account A was tried and hit quota, then Account B took over
            self.assertEqual(executed_accounts, ['account_a', 'account_b'])

            # 5. Verify Account A is marked quota-blocked and in cooldown
            self.assertEqual(engine.auth_pool.accounts['account_a'].health_state, GoogleAccountHealth.QUOTA_BLOCKED)
            self.assertTrue(engine.auth_pool.accounts['account_a'].is_cooldown_active())

            # 6. Verify manager is bound to Account B
            self.assertEqual(manager.auth_slot_id, 'account_b')
            self.assertEqual(engine.auth_pool.accounts['account_b'].health_state, GoogleAccountHealth.HEALTHY)

            # 7. Verify workspace and artifacts are preserved
            content = artifact_file.read_text(encoding='utf-8')
            self.assertIn('Initial research findings on Account A', content)
            self.assertIn('Continued research under Account B', content)

            # 8. Verify session ID was preserved/updated and no duplicated worker execution occurred
            self.assertEqual(manager.session_id, 'session-resumed-under-b')
            self.assertEqual(turn_count, 2)  # Exactly 1 failure on A + 1 success on B
            self.assertEqual(len(engine.projects['ContinuityProject'].worker_ids), 0)

        finally:
            await engine.close()

    def test_cached_long_cooldown_cleared_by_successful_live_request(self):
        """Regression test:
        1. Account has a cached multi-day cooldown (e.g. 105 hours) and health_state=QUOTA_BLOCKED.
        2. Scheduler refuses to pick it; reports no healthy accounts.
        3. A live Gemini request succeeds (mark_success).
        4. Account immediately becomes HEALTHY, cooldown_until is cleared, and it is schedulable again.
        """
        acc = self.pool.register_account('cooldown_user@example.test', account_id='cached_cooldown_acc')
        agent = AgentNode(project_name='P', name='Mgr', role=Role.MANAGER, model='antigravity/gemini-3.8-flash-high')

        # 1. Simulate cached long cooldown (e.g. 105h)
        long_error = 'API error (attempt 1): RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 105h53m20s.'
        self.pool.mark_quota_blocked('cached_cooldown_acc', long_error, cooldown_seconds=381200)

        self.assertEqual(acc.health_state, GoogleAccountHealth.QUOTA_BLOCKED)
        self.assertTrue(acc.is_cooldown_active())
        self.assertGreater(acc.remaining_cooldown_seconds(), 300000)
        self.assertIn('105h', acc.last_error)

        # 2. Scheduler must refuse to schedule it
        with self.assertRaisesRegex(RuntimeError, 'No healthy Google accounts available'):
            self.pool.acquire_slot(agent)

        # 3. Successful live Gemini request executes
        self.pool.mark_success('cached_cooldown_acc', turn_tokens=150)

        # 4. Account is now healthy, cooldown is wiped out, and scheduler can acquire it
        self.assertEqual(acc.health_state, GoogleAccountHealth.HEALTHY)
        self.assertIsNone(acc.cooldown_until)
        self.assertIsNone(acc.last_error)
        self.assertFalse(acc.is_cooldown_active())
        self.assertEqual(acc.remaining_cooldown_seconds(), 0)

        slot = self.pool.acquire_slot(agent)
        self.assertEqual(slot.account_id, 'cached_cooldown_acc')
        self.pool.release_slot(slot.account_id)

    def test_auth_verification_required_distinguished_and_not_scheduled(self):
        """Account requiring browser/eligibility verification must be marked
        AUTH_VERIFICATION_REQUIRED, not quota-blocked, and must not be scheduled.
        """
        acc = self.pool.register_account('verify_user@example.test', account_id='acc_verify')
        agent = AgentNode(project_name='P', name='Mgr', role=Role.MANAGER, model='antigravity/gemini-3.8-flash-high')

        eligibility_error = (
            'Eligibility check failed: Your current account is not eligible for Antigravity. '
            'Verify your account to continue.\nhttps://accounts.google.com/signin/continue?...'
        )
        # Classify error: must be non-retryable
        is_ret, secs, iso = self.pool.classify_error(Exception(eligibility_error))
        self.assertFalse(is_ret, 'Eligibility check error must not be classified as retryable quota')

        # Mark error: must transition to AUTH_VERIFICATION_REQUIRED, not ERROR or QUOTA_BLOCKED
        self.pool.mark_error('acc_verify', eligibility_error)
        self.assertEqual(acc.health_state, GoogleAccountHealth.AUTH_VERIFICATION_REQUIRED)
        self.assertIsNone(acc.cooldown_until, 'Must not set a quota cooldown for auth verification')
        self.assertFalse(acc.is_cooldown_active())

        # Scheduler must NOT schedule this account
        with self.assertRaisesRegex(RuntimeError, 'No healthy Google accounts available'):
            self.pool.acquire_slot(agent)

    def test_transient_errors_capped_cooldown(self):
        """Transient 429/network errors without explicit provider reset time
        must not create multi-day cooldowns.
        """
        acc = self.pool.register_account('transient_user@example.test', account_id='acc_transient')
        self.pool.mark_transient_error('acc_transient', 'Temporary 429 server busy', cooldown_seconds=60)
        self.assertEqual(acc.health_state, GoogleAccountHealth.TRANSIENT_ERROR)
        self.assertLessEqual(acc.remaining_cooldown_seconds(), 300)
        self.assertGreater(acc.remaining_cooldown_seconds(), 0)

    def test_replace_gmail_account_when_quota_exhausted(self):
        """When an account hits quota, replacing its Gmail with a new account
        updates the email, restores HEALTHY state, and clears cooldown.
        """
        import base64
        acc1 = self.pool.register_account('old_user@example.com', account_id='account_01')
        acc2 = self.pool.register_account('existing_other@example.com', account_id='account_02')

        # 1. Simulate account_01 hitting quota
        self.pool.mark_quota_blocked('account_01', 'RESOURCE_EXHAUSTED: Individual quota reached', cooldown_seconds=18000)
        self.assertEqual(acc1.health_state, GoogleAccountHealth.QUOTA_BLOCKED)
        self.assertTrue(acc1.is_cooldown_active())

        # 2. Prepare mock credential for fresh Google account
        payload_b64 = base64.urlsafe_b64encode(json.dumps({'email': 'fresh_user@example.com'}).encode()).decode().rstrip('=')
        mock_blob = json.dumps({'id_token': f'fakeheader.{payload_b64}.fakesig'}).encode('utf-8')

        with patch('core.auth_pool.WindowsKeyringHelper.read_credential', return_value=('antigravity', mock_blob)):
            # 3. Replace Gmail on account_01
            success = self.pool.save_account_credential('account_01', allow_replacement=True)
            self.assertTrue(success)

            # 4. Verify account_01 was updated to the fresh email
            self.assertEqual(acc1.email, 'fresh_user@example.com')
            self.assertEqual(acc1.health_state, GoogleAccountHealth.HEALTHY)
            self.assertIsNone(acc1.cooldown_until)
            self.assertIsNone(acc1.last_error)
            self.assertFalse(acc1.is_cooldown_active())

            # 5. Verify duplicate collision check: trying to use acc2's email on acc1 must raise ValueError
            duplicate_b64 = base64.urlsafe_b64encode(json.dumps({'email': 'existing_other@example.com'}).encode()).decode().rstrip('=')
            duplicate_blob = json.dumps({'id_token': f'fakeheader.{duplicate_b64}.fakesig'}).encode('utf-8')
            with patch('core.auth_pool.WindowsKeyringHelper.read_credential', return_value=('antigravity', duplicate_blob)):
                with self.assertRaisesRegex(ValueError, "already registered as 'account_02'"):
                    self.pool.save_account_credential('account_01', allow_replacement=True)

    def test_auto_heal_expired_cooldowns(self):
        """When an account cooldown timestamp has passed, heal_expired_cooldowns
        restores it to HEALTHY, clears cooldown_until, and clears last_error.
        """
        from datetime import datetime, timezone, timedelta
        acc = self.pool.register_account('expiring@example.com', account_id='acc_exp')
        # Simulate an expired cooldown from 10 minutes ago
        past_dt = datetime.now(timezone.utc) - timedelta(minutes=10)
        acc.health_state = GoogleAccountHealth.QUOTA_BLOCKED
        acc.cooldown_until = past_dt.isoformat()
        acc.last_error = 'RESOURCE_EXHAUSTED: quota reached'
        self.pool.save()

        healed = self.pool.heal_expired_cooldowns()
        self.assertIn('acc_exp', healed)
        self.assertEqual(acc.health_state, GoogleAccountHealth.HEALTHY)
        self.assertIsNone(acc.cooldown_until)
        self.assertIsNone(acc.last_error)

    def test_print_timeout_not_classified_as_quota_and_auto_healed(self):
        """Print timeouts must never be classified as retryable quota, and any
        existing print-timeout error must be auto-healed to HEALTHY.
        """
        is_ret, _, _ = self.pool.classify_error(Exception("[agy] print timeout after 1m30s with turn in progress"))
        self.assertFalse(is_ret)

        acc = self.pool.register_account('timeout_acc@example.com', account_id='acc_to')
        acc.health_state = GoogleAccountHealth.QUOTA_BLOCKED
        acc.last_error = '[agy] print timeout after 1m30s with turn in progress; returning partial output'
        self.pool.save()

        healed = self.pool.heal_expired_cooldowns()
        self.assertIn('acc_to', healed)
        self.assertEqual(acc.health_state, GoogleAccountHealth.HEALTHY)
        self.assertIsNone(acc.cooldown_until)
        self.assertIsNone(acc.last_error)


if __name__ == '__main__':
    unittest.main()

