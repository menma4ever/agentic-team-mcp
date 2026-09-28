"""Unit test suite for Telegram Whitelist Manager, Multi-User Isolation, Safe Project Deletion,
and Watchdog Brain Conversational Capabilities.
"""
import os
import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.telegram_bridge import TelegramWhitelistManager, TelegramBridge
from core.watchdog_brain import WatchdogBrain
from core.config import settings, SystemSettings


class TestTelegramWhitelistManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.whitelist_path = Path(self.temp_dir) / "telegram_whitelist.json"
        self.mgr = TelegramWhitelistManager(storage_path=self.whitelist_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_master_owner_recognition(self):
        """Verify master owner @zwyci and chat_id 5644286697 are always recognized."""
        self.assertTrue(self.mgr.is_master_owner("zwyci", 12345))
        self.assertTrue(self.mgr.is_master_owner("@zwyci", 99999))
        self.assertTrue(self.mgr.is_master_owner("other_user", 5644286697))
        self.assertFalse(self.mgr.is_master_owner("menma", 11111))

        self.assertTrue(self.mgr.is_whitelisted("zwyci", 12345))
        self.assertTrue(self.mgr.is_whitelisted("other", 5644286697))
        self.assertFalse(self.mgr.is_whitelisted("unknown_user", 99999))

    def test_parse_and_apply_whitelist_command_add(self):
        """Verify natural language whitelist parsing with creation quota, token quota, output quota, and projects."""
        cmd = "add @menma to whitelist 1 time creation quota, 50mln token quota, 200k output quota, access to uzbek_sft"
        reply = self.mgr.parse_and_apply_whitelist_command(cmd)
        self.assertIsNotNone(reply)
        self.assertIn("User Whitelisted Successfully", reply)
        self.assertIn("@menma", reply)
        self.assertIn("`1` project(s)", reply)
        self.assertIn("50,000,000", reply)
        self.assertIn("200,000", reply)
        self.assertIn("uzbek_sft", reply)

        # Check internal storage
        self.assertTrue(self.mgr.is_whitelisted("menma", None))
        entry = self.mgr.get_user_entry("menma", None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["project_creation_quota"], 1)
        self.assertEqual(entry["token_quota"], 50_000_000)
        self.assertEqual(entry["output_quota"], 200_000)
        self.assertEqual(entry["allowed_projects"], ["uzbek_sft"])

    def test_chat_id_dynamic_registration(self):
        """Verify whitelisted username dynamically binds to chat_id upon first interaction."""
        self.mgr.add_or_update_user("menma", project_creation_quota=2)
        entry = self.mgr.get_user_entry("menma", None)
        self.assertIsNone(entry.get("chat_id"))

        # Register chat_id
        self.mgr.register_chat_id("menma", 888123)
        entry_updated = self.mgr.get_user_entry("menma", None)
        self.assertEqual(entry_updated.get("chat_id"), 888123)
        self.assertTrue(self.mgr.is_whitelisted(None, 888123))

    def test_parse_and_apply_whitelist_command_remove(self):
        """Verify removing a user from whitelist."""
        self.mgr.add_or_update_user("temp_user")
        self.assertTrue(self.mgr.is_whitelisted("temp_user", None))

        reply = self.mgr.parse_and_apply_whitelist_command("remove @temp_user from whitelist")
        self.assertIn("Removed `@temp_user`", reply)
        self.assertFalse(self.mgr.is_whitelisted("temp_user", None))

    def test_list_whitelist(self):
        """Verify /whitelist formatting."""
        reply_empty = self.mgr.parse_and_apply_whitelist_command("/whitelist")
        self.assertIn("Telegram Whitelist Registry", reply_empty)
        self.assertIn("None currently configured", reply_empty)

        self.mgr.add_or_update_user("colleague_1", project_creation_quota=1, token_quota=10_000_000)
        reply_populated = self.mgr.parse_and_apply_whitelist_command("/whitelist")
        self.assertIn("@colleague_1", reply_populated)
        self.assertIn("10,000,000 tokens", reply_populated)


class TestTelegramBridgeMultiUserAndSecurity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "test_config.json")
        self.whitelist_path = os.path.join(self.temp_dir, "telegram_whitelist.json")

        self.bridge = TelegramBridge(project_dir=self.temp_dir, config_path=self.config_path)
        self.bridge.whitelist_manager = TelegramWhitelistManager(storage_path=self.whitelist_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unauthorized_user_silent_drop(self):
        """Verify messages from non-whitelisted users are silently dropped."""
        fake_update = {
            "update_id": 101,
            "message": {
                "chat": {"id": 999888},
                "from": {"username": "random_stranger"},
                "text": "Hello bot"
            }
        }
        with patch.object(self.bridge, "send_telegram_message") as mock_send, \
             patch.object(self.bridge, "process_incoming_command") as mock_process:
            self.bridge.process_update_packet(fake_update)
            mock_send.assert_not_called()
            mock_process.assert_not_called()

    def test_unauthorized_callback_query_rejected(self):
        """Verify button callbacks from non-whitelisted users are rejected."""
        fake_update = {
            "update_id": 102,
            "callback_query": {
                "id": "cb_1",
                "chat": {"id": 999888},
                "from": {"username": "stranger"},
                "message": {"chat": {"id": 999888}},
                "data": "approve:req_123"
            }
        }
        with patch.object(self.bridge, "answer_callback_query") as mock_answer, \
             patch.object(self.bridge, "process_callback_query") as mock_proc:
            self.bridge.process_update_packet(fake_update)
            mock_answer.assert_called_once_with("cb_1", text="Unauthorized")
            mock_proc.assert_not_called()

    def test_whitelisted_user_greeting_shows_scoped_quota(self):
        """Verify whitelisted user receives tailored greeting with remaining creation & token quotas."""
        self.bridge.whitelist_manager.add_or_update_user(
            username="menma",
            project_creation_quota=1,
            token_quota=50_000_000,
            output_quota=200_000,
            allowed_projects=["Project_Menma"]
        )
        resp = self.bridge.process_incoming_command(chat_id=777111, text="hello", from_username="menma")
        self.assertIn("Hello @menma!", resp)
        self.assertIn("`1` project(s) remaining", resp)
        self.assertIn("50,000,000", resp)
        self.assertIn("200,000", resp)
        self.assertIn("Project_Menma", resp)

    def test_non_owner_blocked_from_admin_commands(self):
        """Verify non-owners cannot manage whitelist, delete projects, or configure API keys."""
        self.bridge.whitelist_manager.add_or_update_user(username="colleague")
        
        # 1. Whitelist command attempt
        resp1 = self.bridge.process_incoming_command(chat_id=555222, text="add @friend to whitelist", from_username="colleague")
        self.assertIn("Access Denied", resp1)
        self.assertIn("Only the Human Owner (@zwyci)", resp1)

        # 2. Delete project attempt
        resp2 = self.bridge.process_incoming_command(chat_id=555222, text="delete project Integration", from_username="colleague")
        self.assertIn("Access Denied", resp2)
        self.assertIn("Only the Human Owner (@zwyci)", resp2)

        # 3. Add API key attempt
        resp3 = self.bridge.process_incoming_command(chat_id=555222, text="add api key for deepseek: sk-12345678", from_username="colleague")
        self.assertIn("Access Denied", resp3)
        self.assertIn("Only the Human Owner (@zwyci)", resp3)


class TestWatchdogBrainFeatures(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.brain = WatchdogBrain()
        self.brain.data_dir = Path(self.temp_dir)
        self.brain._pending_deletions.clear()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_safe_project_deletion_flow(self):
        """Verify multi-step safe project deletion flow: Warning -> Reason -> Confirmation -> Execution."""
        chat_id = 5644286697
        state = {"projects": [{"name": "Integration"}]}

        # Step 1: Trigger deletion
        warn_msg, _ = self.brain.generate_response(chat_id, "delete project Integration", is_owner=True)
        self.assertIn("PROJECT DELETION WARNING", warn_msg)
        self.assertIn("Integration", warn_msg)
        self.assertIn(chat_id, self.brain._pending_deletions)

        # Step 2: Provide rationale/reason
        reason_msg, _ = self.brain.generate_response(chat_id, "Testing completed and project is no longer needed", is_owner=True)
        self.assertIn("Project Deletion Confirmation Required", reason_msg)
        self.assertIn("CONFIRM DELETE Integration", reason_msg)
        self.assertEqual(self.brain._pending_deletions[chat_id]["reason"], "Testing completed and project is no longer needed")

        # Step 3: Provide explicit confirmation
        with patch.object(self.brain, "_api_delete", return_value={"deleted": True}) as mock_del:
            final_msg, _ = self.brain.generate_response(chat_id, "CONFIRM DELETE Integration", is_owner=True)
            mock_del.assert_called_once_with("/api/projects?name=Integration")
            self.assertIn("Project Deleted Permanently", final_msg)
            self.assertIn("Integration", final_msg)
            self.assertNotIn(chat_id, self.brain._pending_deletions)

    def test_safe_project_deletion_cancel(self):
        """Verify cancelling a project deletion aborts safely."""
        chat_id = 5644286697
        self.brain.generate_response(chat_id, "delete project TestProj", is_owner=True)
        self.assertIn(chat_id, self.brain._pending_deletions)

        cancel_msg, _ = self.brain.generate_response(chat_id, "cancel", is_owner=True)
        self.assertIn("Project Deletion Cancelled", cancel_msg)
        self.assertNotIn(chat_id, self.brain._pending_deletions)

    def test_api_key_registration_via_chat(self):
        """Verify owner can register provider API keys directly via Telegram chat."""
        chat_id = 5644286697
        original_keys = dict(settings.api_keys)
        try:
            with patch.object(SystemSettings, "save") as mock_save:
                resp, _ = self.brain.generate_response(chat_id, "add api key for deepseek: sk-test-key-99887766", is_owner=True)
                self.assertIn("API Key Registered Successfully", resp)
                self.assertIn("deepseek", resp)
                self.assertEqual(settings.api_keys.get("deepseek"), "sk-test-key-99887766")
                mock_save.assert_called_once()
        finally:
            settings.api_keys = original_keys

    def test_usage_query_scope_clarification(self):
        """Verify ambiguous usage requests prompt for 5h, weekly, or project scope."""
        chat_id = 5644286697
        resp, _ = self.brain.generate_response(chat_id, "how is usage?", is_owner=True)
        self.assertIn("Token Usage Telemetry — Select Scope", resp)
        self.assertIn("Send `usage 5h`", resp)
        self.assertIn("Send `usage weekly`", resp)

        # 5h scope query
        resp_5h, _ = self.brain.generate_response(chat_id, "usage 5h", is_owner=True)
        self.assertIn("5-Hour Rolling Usage Telemetry", resp_5h)

        # Weekly scope query
        resp_wk, _ = self.brain.generate_response(chat_id, "usage weekly", is_owner=True)
        self.assertIn("Weekly Usage Telemetry", resp_wk)

    def test_domain_probing_before_launch(self):
        """Verify exploring a project idea (e.g. SFT data collection) triggers domain probing."""
        chat_id = 5644286697
        mock_response = (
            "🛡️ *Root Watchdog — SFT Project Domain Inquiry*\n\n"
            "Before launching the SFT data collection pipeline, let's nail down these specifications:\n"
            "1. *Domain Mixture*: What is the balance between instruction-following, math, coding, and dialogue?\n"
            "2. *Acquisition Pipeline*: Synthetic multi-agent generation or crawled/curated text?\n"
            "3. *Token / Sample Budget*: Target volume in raw tokens or sample count?\n"
            "4. *Chat Template*: ChatML, ShareGPT, or Alpaca format?\n\n"
            "Once confirmed, I will spin up the CEO and Manager immediately."
        )
        with patch.object(self.brain, "_call_agy_cli", return_value=mock_response):
            resp, dispatch = self.brain.generate_response(chat_id, "can we launch sft data project?", is_owner=True)
            self.assertIn("SFT Project Domain Inquiry", resp)
            self.assertIn("Domain Mixture", resp)
            self.assertIn("Chat Template", resp)
            self.assertIsNone(dispatch)


class TestMultimodalSupport(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "test_config.json")
        self.whitelist_path = os.path.join(self.temp_dir, "telegram_whitelist.json")
        self.bridge = TelegramBridge(project_dir=self.temp_dir, config_path=self.config_path)
        self.bridge.whitelist_manager = TelegramWhitelistManager(storage_path=self.whitelist_path)
        self.brain = WatchdogBrain(data_dir=Path(self.temp_dir))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_voice_note_native_gemini_processed(self):
        """Voice note is transcribed and executed directly by native Gemini via agy CLI."""
        audio_file = Path(self.temp_dir) / "test_voice.oga"
        audio_file.write_bytes(b"dummy audio data")

        mock_agy_reply = "🎙️ *Voice Note Transcribed*: \"wake up manager\"\n[ACTION:RESUME_AGENT:MANAGER]"
        with patch.object(self.brain, "_call_agy_cli", return_value=mock_agy_reply):
            with patch.object(self.brain, "handle_resume_agent", return_value="✅ Dispatched unpause signal to Manager_Bonsai"):
                reply, _ = self.brain.process_multimodal_input(
                    chat_id=5644286697,
                    media_type="voice",
                    file_path=audio_file,
                    is_owner=True
                )
                self.assertIn("Voice Note Transcribed", reply)
                self.assertIn("wake up manager", reply)
                self.assertIn("Dispatched unpause signal to Manager_Bonsai", reply)

    def test_voice_note_fallback_whisper_processed(self):
        """Voice note fallback transcribes via Whisper API if agy CLI is offline."""
        audio_file = Path(self.temp_dir) / "test_voice.oga"
        audio_file.write_bytes(b"dummy audio data")

        with patch.object(self.brain, "_call_agy_cli", return_value=None):
            with patch("core.multimodal.transcribe_audio", return_value="wake up manager"):
                with patch.object(self.brain, "handle_resume_agent", return_value="✅ Dispatched unpause signal to Manager_Bonsai"):
                    reply, _ = self.brain.process_multimodal_input(
                        chat_id=5644286697,
                        media_type="voice",
                        file_path=audio_file,
                        is_owner=True
                    )
                    self.assertIn("Voice Note Transcribed", reply)
                    self.assertIn("wake up manager", reply)
                    self.assertIn("Dispatched unpause signal to Manager_Bonsai", reply)

    def test_voice_note_without_api_key_guides_user(self):
        """Voice note received when no transcription key exists prompts user to add API key."""
        audio_file = Path(self.temp_dir) / "test_voice2.oga"
        audio_file.write_bytes(b"dummy audio data")

        with patch.object(self.brain, "_call_agy_cli", return_value=None):
            with patch("core.multimodal.transcribe_audio", return_value=None):
                reply, _ = self.brain.process_multimodal_input(
                    chat_id=5644286697,
                    media_type="voice",
                    file_path=audio_file,
                    is_owner=True
                )
                self.assertIn("Voice Note Received & Saved", reply)
                self.assertIn("add api key for gemini:", reply)

    def test_text_document_ingestion(self):
        """Text / code documents are parsed and analyzed directly without needing an external vision key."""
        doc_file = Path(self.temp_dir) / "pipeline_fix.py"
        doc_file.write_text("def solve_problem():\n    return 42\n", encoding="utf-8")

        mock_reply = "Analyzed pipeline_fix.py: It defines solve_problem returning 42."
        with patch.object(self.brain, "generate_response", return_value=(mock_reply, None)) as mock_gen:
            reply, _ = self.brain.process_multimodal_input(
                chat_id=5644286697,
                media_type="document",
                file_path=doc_file,
                caption="Please review this script",
                is_owner=True
            )
            self.assertIn("Document Ingested", reply)
            self.assertIn("pipeline_fix.py", reply)
            self.assertIn("Analyzed pipeline_fix.py", reply)
            mock_gen.assert_called_once()
            called_prompt = mock_gen.call_args[0][1]
            self.assertIn("def solve_problem():", called_prompt)

    def test_telegram_bridge_voice_message_pipeline(self):
        """TelegramBridge handles incoming voice packet, downloads media, and replies."""
        audio_file = Path(self.temp_dir) / "downloaded_voice.oga"
        audio_file.write_bytes(b"dummy voice")

        update = {
            "update_id": 100,
            "message": {
                "chat": {"id": 5644286697},
                "from": {"username": "zwyci"},
                "voice": {"file_id": "tg_voice_id_123", "duration": 5, "mime_type": "audio/ogg"}
            }
        }

        with patch("core.multimodal.download_telegram_file", return_value=audio_file):
            with patch("core.watchdog_brain.watchdog_brain.process_multimodal_input", return_value=("🎙️ Voice Note Processed", None)):
                with patch.object(self.bridge, "send_telegram_message") as mock_send:
                    self.bridge.process_update_packet(update)
                    mock_send.assert_called_once_with(5644286697, "🎙️ Voice Note Processed")

    def test_telegram_bridge_unauthorized_media_dropped(self):
        """Media messages from unauthorized users are silently dropped."""
        update = {
            "update_id": 101,
            "message": {
                "chat": {"id": 999111},
                "from": {"username": "stranger"},
                "photo": [{"file_id": "p1"}, {"file_id": "p2"}]
            }
        }

        with patch("core.multimodal.download_telegram_file") as mock_dl:
            with patch.object(self.bridge, "send_telegram_message") as mock_send:
                self.bridge.process_update_packet(update)
                mock_dl.assert_not_called()
                mock_send.assert_not_called()

    def test_telegram_formatter_html_rendering(self):
        """TelegramFormatter cleans GFM headers, bold, bullets, and blocks into rich HTML."""
        from core.telegram_bridge import TelegramFormatter
        raw = (
            "### 📊 *Project Status Overview*\n"
            "```text\n"
            "Project: Bonsai_Sauce_Qwen3.5-2B\n"
            "```\n"
            "---\n"
            "### 🏆 *Key Findings*\n"
            "* **SDE 14 Adversarial Audit:** **ACCEPTED**\n"
            "* **Canonical Coverage:** `70.66%`\n"
        )
        html_out = TelegramFormatter.format_to_html(raw)
        self.assertNotIn("###", html_out)
        self.assertNotIn("---", html_out)
        self.assertIn("<b>📊 Project Status Overview</b>", html_out)
        self.assertIn("<pre><code>Project: Bonsai_Sauce_Qwen3.5-2B</code></pre>", html_out)
        self.assertIn("• <b>SDE 14 Adversarial Audit:</b> <b>ACCEPTED</b>", html_out)
        self.assertIn("<code>70.66%</code>", html_out)


if __name__ == "__main__":
    unittest.main()

