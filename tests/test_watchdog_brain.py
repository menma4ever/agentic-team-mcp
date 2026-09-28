"""Unit tests for Root Watchdog AI Conversational Brain & Telegram Bridge Integration."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.watchdog_brain import WatchdogBrain, render_box_table
from engine.models import Role, AgentStatus
from engine.orchestrator import Orchestrator


class WatchdogBrainTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.brain = WatchdogBrain(data_dir=self.data_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_render_box_table(self):
        headers = ["Agent", "Role", "Status"]
        rows = [
            ["CEO_Astra", "CEO", "Paused"],
            ["Manager_Bonsai", "MANAGER", "Queued"],
            ["Storage_Auditor", "WORKER", "Working"]
        ]
        table = render_box_table(headers, rows)
        self.assertIn("┌", table)
        self.assertIn("CEO_Astra", table)
        self.assertIn("Manager_Bonsai", table)
        self.assertIn("Storage_Auditor", table)
        self.assertIn("└", table)

    def test_workers_table_formatting(self):
        workers = [
            {"name": "Audit_Worker", "model": "antigravity/gemini-3.8-flash", "status": "working", "current_task": "cleanup"},
            {"name": "Code_Worker", "model": "zai/glm-5.3", "status": "idle", "current_task": None}
        ]
        res = self.brain.format_workers_table(workers)
        self.assertIn("Audit_Worker", res)
        self.assertIn("Code_Worker", res)
        self.assertIn("cleanup", res)

    def test_table_interception_on_user_query(self):
        with patch.object(self.brain, "fetch_live_system_state") as mock_state:
            mock_state.return_value = {
                "projects": [{"name": "Test_Project"}],
                "active_project": "Test_Project",
                "workers": [{"name": "Worker_1", "model": "gemini", "status": "idle", "current_task": "standby"}]
            }
            reply, dispatch = self.brain.generate_response(12345, "show me a table of workers", project_name="Test_Project")
            self.assertIn("Root Watchdog", reply)
            self.assertIn("Worker_1", reply)
            self.assertIn("┌", reply)
            self.assertIsNone(dispatch)

    def test_dispatch_directive_parsing_ceo(self):
        simulated_llm_response = (
            "Got it. Bringing this to CEO Astra to review and confirm whether we should authorize SDE 15.\n\n"
            "[DISPATCH: CEO] Authorize SDE 15 execution immediately"
        )
        with patch.object(self.brain, "fetch_live_system_state") as mock_state, \
             patch.object(self.brain, "_call_agy_cli", return_value=simulated_llm_response), \
             patch.object(self.brain, "_api_post") as mock_post:
            mock_state.return_value = {
                "active_project": "Test_Project",
                "ceo": {"id": "ceo_123", "name": "CEO_Astra"},
                "manager": {"id": "mgr_456", "name": "Manager_Bonsai"},
                "workers": []
            }
            reply, dispatch = self.brain.generate_response(12345, "Approve SDE 15", project_name="Test_Project")
            self.assertNotIn("[DISPATCH:", reply)
            self.assertIn("Bringing this to CEO Astra", reply)
            self.assertIsNotNone(dispatch)
            self.assertEqual(dispatch["target_role"], "CEO")
            self.assertEqual(dispatch["target_agent_id"], "ceo_123")
            mock_post.assert_called_once()

    def test_dispatch_directive_parsing_manager(self):
        simulated_llm_response = (
            "Understood. Checking with Manager Bonsai to pause the current forensic audit.\n\n"
            "[DISPATCH: MANAGER] Pause current forensic audit"
        )
        with patch.object(self.brain, "fetch_live_system_state") as mock_state, \
             patch.object(self.brain, "_call_agy_cli", return_value=simulated_llm_response), \
             patch.object(self.brain, "_api_post") as mock_post:
            mock_state.return_value = {
                "active_project": "Test_Project",
                "ceo": {"id": "ceo_123", "name": "CEO_Astra"},
                "manager": {"id": "mgr_456", "name": "Manager_Bonsai"},
                "workers": []
            }
            reply, dispatch = self.brain.generate_response(12345, "Pause the audit", project_name="Test_Project")
            self.assertNotIn("[DISPATCH:", reply)
            self.assertIn("Checking with Manager Bonsai", reply)
            self.assertIsNotNone(dispatch)
            self.assertEqual(dispatch["target_role"], "MANAGER")
            self.assertEqual(dispatch["target_agent_id"], "mgr_456")
            mock_post.assert_called_once()

    def test_dormancy_sensing_when_idle(self):
        with patch.object(self.brain, "_api_get") as mock_get:
            mock_get.side_effect = lambda path: {
                "/api/projects": {"projects": [{"name": "Test_Project", "status": "active"}]},
                "/api/tree?project=Test_Project": {
                    "ceo": {"id": "c1", "name": "CEO_Astra", "status": "idle"},
                    "manager": {"id": "m1", "name": "Manager_Bonsai", "status": "idle"},
                    "workers": [
                        {"id": "w1", "name": "Worker_1", "status": "idle"},
                        {"id": "w2", "name": "Worker_2", "status": "idle"}
                    ]
                }
            }.get(path)
            state = self.brain.fetch_live_system_state("Test_Project")
            self.assertTrue(state["is_fleet_dormant"])
            self.assertIn("Dormant / Resting", state["execution_state_label"])
            self.assertEqual(len(state["working_agents"]), 0)
            self.assertEqual(len(state["resting_agents"]), 4)

    def test_dormancy_inquiry_interception(self):
        with patch.object(self.brain, "fetch_live_system_state") as mock_state:
            mock_state.return_value = {
                "active_project": "Bonsai_Sauce_Qwen3.5-2B",
                "ceo": {"id": "c1", "name": "CEO_Astra", "model": "openai/GPT-6 Sol", "status": "idle"},
                "manager": {"id": "m1", "name": "Manager_Bonsai", "model": "antigravity/gemini-3.8-flash-high", "status": "idle"},
                "workers": [
                    {"name": "SDE14_Worker", "status": "idle", "model": "antigravity/gemini-3.8-flash-high"},
                    {"name": "SDE15_Worker", "status": "idle", "model": "antigravity/gemini-3.8-flash-high"}
                ],
                "is_fleet_dormant": True,
                "working_agents": [],
                "resting_agents": [{"name": "c1"}, {"name": "m1"}, {"name": "w1"}, {"name": "w2"}],
                "failed_agents": [],
                "paused_agents": []
            }
            reply, dispatch = self.brain.generate_response(12345, "why watchdog says theyre active even though everyone sleeping", project_name="Bonsai_Sauce_Qwen3.5-2B")
            self.assertIn("Fleet Dormancy & Model Configuration Audit", reply)
            self.assertIn("All Workers Swapped to Google Gemini Flash High", reply)
            self.assertIn("Dormancy & Sleep Sensing Activated", reply)
            self.assertIn("Dormant / Resting", reply)
            self.assertIsNone(dispatch)


class OrchestratorWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.orch = Orchestrator(data_dir=self.data_dir)

    def tearDown(self):
        self.orch.store.close()
        self.temp_dir.cleanup()

    def test_role_enum_has_watchdog(self):
        self.assertEqual(Role.WATCHDOG.value, "WATCHDOG")

    def test_get_watchdog_node(self):
        wd = self.orch.get_watchdog()
        self.assertEqual(wd.id, "system_root_watchdog")
        self.assertEqual(wd.role, Role.WATCHDOG)
        self.assertEqual(wd.model, "antigravity/gemini-3.8-flash")
        self.assertEqual(wd.avatar_logo, "gemini")

    def test_agent_resolver_finds_watchdog(self):
        resolved = self.orch.agent("system_root_watchdog")
        self.assertEqual(resolved.id, "system_root_watchdog")
        self.assertEqual(resolved.role, Role.WATCHDOG)


if __name__ == "__main__":
    unittest.main()
