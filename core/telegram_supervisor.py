"""
Telegram Human Bridge Lifecycle Supervisor & Process Monitor.
Manages the long-polling daemon process, secure uncommitted token storage,
one-time pairing verification, bot profile retrieval, auto-start, and self-healing restarts.
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Optional

from core.config import DATA_DIR, ROOT

logger = logging.getLogger("TelegramSupervisor")

def mask_token(token: Optional[str]) -> Optional[str]:
    """Masks a token string for safe logging and UI display."""
    if not token:
        return ""
    if len(token) < 8:
        return "******"
    return f"{token[:4]}****:****{token[-4:]}"


class TelegramSupervisor:
    """Supervises the Telegram Bridge daemon process and manages connection lifecycle."""

    def __init__(self, data_dir: Optional[Path] = None, root_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir or DATA_DIR).resolve()
        self.root_dir = Path(root_dir or ROOT).resolve()
        self.config_file = self.data_dir / "telegram_settings.json"
        self.process: Optional[subprocess.Popen] = None
        self.bot_handle: Optional[str] = None
        self.paired_user_handle: Optional[str] = None
        self.last_error: Optional[str] = None
        self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Loads persistent telegram settings from local uncommitted configuration."""
        if self.config_file.is_file():
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
                self.bot_handle = data.get("bot_handle")
                self.paired_user_handle = data.get("paired_user_handle")
                return data
            except Exception as e:
                logger.error(f"Failed to load telegram config: {e}")
        return {
            "bot_token": "",
            "allowed_chat_ids": [],
            "pairing_code": None,
            "pairing_required": True,
            "enabled": False,
            "bot_handle": None,
            "paired_user_handle": None
        }

    def _save_config(self, data: Dict[str, Any]) -> None:
        """Saves telegram settings securely in uncommitted storage."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self.config_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp_file.replace(self.config_file)

    def get_token(self) -> Optional[str]:
        """Resolves token from config or environment variable."""
        cfg = self._load_config()
        token = cfg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
        return token if token and token not in ("YOUR_TELEGRAM_BOT_TOKEN_HERE", "MOCK_TOKEN") else None

    def mask_token(self, token: Optional[str]) -> Optional[str]:
        """Masks a token string for safe logging and UI display."""
        if not token:
            return None
        if len(token) < 8:
            return "******"
        return f"{token[:4]}****:****{token[-4:]}"

    def resolve_project_dir(self) -> Path:
        """Locates the currently active project directory, falling back to default."""
        try:
            from engine.store import Store
            st = Store(self.data_dir / "team.sqlite3")
            raw = st.load()
            active = raw.get("active")
            if active and (self.root_dir / "projects" / active).is_dir():
                return self.root_dir / "projects" / active
        except Exception:
            pass
        projects_dir = self.root_dir / "projects"
        if (projects_dir / "Job searching").is_dir():
            return projects_dir / "Job searching"
        if (projects_dir / "Bonsai_Sauce_Qwen3.5-2B").is_dir():
            return projects_dir / "Bonsai_Sauce_Qwen3.5-2B"
        return self.root_dir

    def fetch_bot_info(self, token: str) -> Optional[str]:
        """Queries the Telegram getMe endpoint to fetch the bot username."""
        if not token or token == "MOCK_TOKEN":
            return "@BonsaiSauceTeamBot"
        url = f"https://api.telegram.org/bot{token}/getMe"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AgenticTeamBridge/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("ok"):
                    username = res["result"].get("username")
                    if username:
                        return f"@{username}"
        except Exception as e:
            logger.warning(f"Failed to query getMe from Telegram: {e}")
        return self.bot_handle or "@TelegramBot"

    def _kill_stale_bridge_processes(self) -> None:
        """Finds and terminates any orphaned or duplicate telegram_bridge.py processes on the OS."""
        try:
            import psutil
            curr_pid = os.getpid()
            curr_ppid = os.getppid() if hasattr(os, "getppid") else None
            for p in psutil.process_iter(['pid', 'name']):
                try:
                    if p.pid == curr_pid or (curr_ppid and p.pid == curr_ppid):
                        continue
                    if self.process and p.pid == self.process.pid:
                        continue
                    if "python" not in p.name().lower():
                        continue
                    cmd = " ".join(p.cmdline())
                    if "telegram_bridge.py" in cmd:
                        logger.info(f"Terminating duplicate/stale telegram_bridge.py process (PID: {p.pid})")
                        p.terminate()
                        try:
                            p.wait(timeout=2)
                        except Exception:
                            p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                    pass
        except Exception as e:
            logger.warning(f"Error checking for stale bridge processes: {e}")

    def _find_live_bridge_pid(self) -> Optional[int]:
        """Finds any active telegram_bridge.py process on the OS via psutil."""
        try:
            import psutil
            curr_pid = os.getpid()
            curr_ppid = os.getppid() if hasattr(os, "getppid") else None
            for p in psutil.process_iter(['pid', 'name']):
                try:
                    if p.pid == curr_pid or (curr_ppid and p.pid == curr_ppid):
                        continue
                    if "python" not in p.name().lower():
                        continue
                    cmd = " ".join(p.cmdline())
                    if "telegram_bridge.py" in cmd and p.is_running():
                        return p.pid
                except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                    pass
        except Exception:
            pass
        return None

    def is_running(self) -> bool:
        """Checks if the supervisor-managed daemon subprocess or an OS bridge process is alive."""
        if self.process is not None:
            poll = self.process.poll()
            if poll is None:
                return True
            self.process = None
        return self._find_live_bridge_pid() is not None

    def start_daemon(self, project_dir: Optional[Path] = None) -> bool:
        """Starts the telegram_bridge.py daemon process."""
        self._kill_stale_bridge_processes()
        if self.is_running():
            return True

        token = self.get_token()
        if not token:
            self.last_error = "No bot token configured"
            return False

        pdir = project_dir or self.resolve_project_dir()
        bridge_script = self.root_dir / "core" / "telegram_bridge.py"
        if not bridge_script.is_file():
            bridge_script = pdir / "shared" / "bridge" / "telegram_bridge.py"
        if not bridge_script.is_file():
            self.last_error = f"Bridge script not found at {bridge_script}"
            logger.error(self.last_error)
            return False

        py_exe = str(self.root_dir / ".venv" / "Scripts" / "python.exe")
        if not os.path.isfile(py_exe):
            py_exe = sys.executable

        env = os.environ.copy()
        env["TELEGRAM_BOT_TOKEN"] = token
        env["PYTHONPATH"] = str(self.root_dir) + (os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else "")
        cmd = [
            py_exe,
            str(bridge_script),
            "--daemon",
            "--project-dir", str(pdir),
            "--config", str(self.config_file)
        ]

        kwargs: Dict[str, Any] = {"cwd": str(pdir), "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        log_path = self.data_dir / "telegram_bridge.log"
        try:
            self._log_file = log_path.open("a", encoding="utf-8")
            self.process = subprocess.Popen(cmd, stdout=self._log_file, stderr=self._log_file, **kwargs)
            logger.info(f"Started Telegram Bridge daemon with PID {self.process.pid}")
            time.sleep(0.3)
            return self.is_running()
        except Exception as e:
            if hasattr(self, "_log_file") and self._log_file and not self._log_file.closed:
                self._log_file.close()
                self._log_file = None
            self.last_error = f"Failed to start daemon: {e}"
            logger.error(self.last_error)
            return False

    def stop_daemon(self) -> bool:
        """Stops the telegram_bridge.py daemon process."""
        if hasattr(self, "_log_file") and self._log_file and not self._log_file.closed:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        if self.process is not None:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            except Exception as e:
                logger.warning(f"Error terminating bridge daemon: {e}")
            self.process = None
            return True
        return True

    def restart_daemon(self) -> bool:
        """Safely stops and restarts the daemon process."""
        self.stop_daemon()
        time.sleep(0.5)
        return self.start_daemon()

    def connect(self, bot_token: str) -> Dict[str, Any]:
        """Connects bot: persists token, fetches bot info, generates pairing code, and starts daemon."""
        bot_token = bot_token.strip()
        if not bot_token:
            raise ValueError("Telegram Bot Token is required")

        cfg = self._load_config()
        cfg["bot_token"] = bot_token
        cfg["enabled"] = True

        # Fetch bot handle
        handle = self.fetch_bot_info(bot_token)
        if handle:
            cfg["bot_handle"] = handle
            self.bot_handle = handle

        # Ensure active pairing code
        if not cfg.get("allowed_chat_ids"):
            if not cfg.get("pairing_code") or not str(cfg.get("pairing_code")).startswith("BONSAI-"):
                import string, random
                code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
                cfg["pairing_code"] = f"BONSAI-{code}"

        self._save_config(cfg)
        self.stop_daemon()
        self.start_daemon()
        return self.get_status()

    def disconnect(self) -> Dict[str, Any]:
        """Disconnects and revokes current bridge session."""
        self.stop_daemon()
        cfg = self._load_config()
        cfg["enabled"] = False
        cfg["allowed_chat_ids"] = []
        cfg["pairing_code"] = None
        self.paired_user_handle = None
        cfg["paired_user_handle"] = None
        self._save_config(cfg)
        return self.get_status()

    def send_test_message(self, text: Optional[str] = None) -> Dict[str, Any]:
        """Sends a verification test ping to the paired owner via Telegram Bot API."""
        token = self.get_token()
        if not token:
            raise ValueError("Telegram Bot Token is not configured")

        cfg = self._load_config()
        allowed_chat_ids = cfg.get("allowed_chat_ids", [])
        if not allowed_chat_ids:
            raise ValueError("No paired Telegram owner found. Complete the pairing workflow first using `/pair <CODE>`.")

        chat_id = allowed_chat_ids[0]
        msg_text = text or (
            "🤖 *Agentic Team MCP — Test Message*\n\n"
            "Telegram Human Bridge connection verified successfully ✅.\n"
            "Supervisory Conduit: 🛡 *Root Watchdog*\n"
            "Communication Liaison: 👤 *Human_Telegram_Liaison*"
        )

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": msg_text,
            "parse_mode": "Markdown"
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    return {"sent": True, "chat_id": chat_id, "detail": "Test message delivered successfully."}
                return {"sent": False, "error": str(data)}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            logger.error(f"HTTP error sending test message: {err}")
            raise RuntimeError(f"Telegram API error {e.code}: {err}")
        except Exception as e:
            logger.error(f"Network error sending test message: {e}")
            raise RuntimeError(f"Network error sending test message: {e}")

    def send_owner_notification(
        self,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "HTML"
    ) -> bool:
        """Sends a notification directly to the paired human owner via Telegram Bot API with chunking support."""
        token = self.get_token()
        if not token:
            return False

        cfg = self._load_config()
        allowed_chat_ids = cfg.get("allowed_chat_ids", [])
        if not allowed_chat_ids:
            return False

        chat_id = allowed_chat_ids[0]

        # De-bounce duplicate notifications within 120 seconds
        import hashlib
        content_sig = hashlib.sha256(text[:300].encode('utf-8', errors='ignore')).hexdigest()
        now_ts = time.time()
        if not hasattr(self, '_notif_cache'):
            self._notif_cache = {}
        self._notif_cache = {k: v for k, v in self._notif_cache.items() if now_ts - v < 300}
        if content_sig in self._notif_cache and (now_ts - self._notif_cache[content_sig] < 120):
            logger.info("Suppressing duplicate notification to Telegram (debounced 120s)")
            return True
        self._notif_cache[content_sig] = now_ts

        max_chunk = 3800
        chunks = []
        if len(text) <= max_chunk:
            chunks = [text]
        else:
            lines = text.split("\n")
            curr = ""
            for l in lines:
                if len(curr) + len(l) + 1 > max_chunk:
                    if curr:
                        chunks.append(curr)
                    curr = l
                else:
                    curr = f"{curr}\n{l}" if curr else l
            if curr:
                chunks.append(curr)

        success = True
        url = f"https://api.telegram.org/bot{token}/sendMessage"

        for i, chunk in enumerate(chunks):
            chunk_markup = reply_markup if i == len(chunks) - 1 else None
            try:
                from core.telegram_bridge import TelegramFormatter
                formatted_chunk = TelegramFormatter.format_to_html(chunk) if parse_mode == "HTML" else chunk
            except Exception:
                formatted_chunk = chunk

            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": formatted_chunk,
                "parse_mode": parse_mode
            }
            if chunk_markup:
                payload["reply_markup"] = chunk_markup

            try:
                data_bytes = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(url, data=data_bytes, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    if not res_data.get("ok"):
                        success = False
            except urllib.error.HTTPError as e:
                logger.warning(f"Telegram sendMessage HTML failed: {e}. Retrying with plain text...")
                try:
                    from core.telegram_bridge import TelegramFormatter
                    plain = TelegramFormatter.strip_tags(formatted_chunk)
                except Exception:
                    plain = chunk
                payload["text"] = plain
                payload.pop("parse_mode", None)
                try:
                    data_bytes = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(url, data=data_bytes, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        res_data = json.loads(resp.read().decode("utf-8"))
                        if not res_data.get("ok"):
                            success = False
                except Exception as ex2:
                    logger.error(f"Plain text retry failed: {ex2}")
                    success = False
            except Exception as e:
                logger.error(f"Failed to send telegram notification: {e}")
                success = False

        return success

    def get_status(self) -> Dict[str, Any]:
        """Returns the real-time status of the Telegram Human Bridge."""
        cfg = self._load_config()
        token = self.get_token()
        running = self.is_running()
        allowed = cfg.get("allowed_chat_ids", [])
        pairing_code = cfg.get("pairing_code")

        if allowed and running:
            state = "connected"
        elif pairing_code or (token and running):
            state = "pairing"
        else:
            state = "disconnected"

        pid = self.process.pid if (self.process and self.process.poll() is None) else self._find_live_bridge_pid()
        bot_handle = cfg.get("bot_handle") or self.bot_handle or (self.fetch_bot_info(token) if token else None)
        paired_handle = cfg.get("paired_user_handle") or (f"Chat ID: {allowed[0]}" if allowed else None)

        deep_link = None
        if bot_handle and pairing_code:
            clean_handle = bot_handle.lstrip("@")
            deep_link = f"https://t.me/{clean_handle}?start={pairing_code}"

        return {
            "connection_state": state,
            "bot_handle": bot_handle or "—",
            "paired_user_handle": paired_handle or "—",
            "bridge_pid": pid,
            "running_status": running,
            "liaison_status": "ACTIVE" if running else "INACTIVE",
            "pairing_code": pairing_code,
            "deep_link": deep_link,
            "has_token": bool(token),
            "masked_token": self.mask_token(token),
            "allowed_chat_ids": allowed,
            "last_error": self.last_error
        }

    def auto_start_if_enabled(self) -> None:
        """Auto-starts daemon on engine startup if previously enabled."""
        cfg = self._load_config()
        if cfg.get("enabled") and self.get_token():
            logger.info("Auto-starting Telegram Bridge daemon on engine startup...")
            self.start_daemon()


# Global singleton instance
telegram_supervisor = TelegramSupervisor()
