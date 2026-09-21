import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone, timedelta
from enum import Enum
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from core.config import DATA_DIR, settings
from core.credential_store import PREFIX, unprotect, save as save_credential


class WindowsKeyringHelper:
    TARGET = "gemini:antigravity"

    @classmethod
    def is_supported(cls) -> bool:
        return os.name == 'nt'

    @classmethod
    def read_credential(cls) -> Optional[Tuple[str, bytes]]:
        if not cls.is_supported():
            return None
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ('Flags', wintypes.DWORD),
                ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME),
                ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)),
                ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD),
                ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR),
                ('UserName', wintypes.LPWSTR),
            ]

        advapi32 = ctypes.windll.advapi32
        cred_ptr = ctypes.POINTER(CREDENTIAL)()
        if advapi32.CredReadW(cls.TARGET, 1, 0, ctypes.byref(cred_ptr)):
            cred = cred_ptr.contents
            blob = bytes(cred.CredentialBlob[:cred.CredentialBlobSize])
            username = cred.UserName or 'antigravity'
            advapi32.CredFree(cred_ptr)
            return username, blob
        return None

    @classmethod
    def write_credential(cls, blob: bytes, username: str = 'antigravity') -> bool:
        if not cls.is_supported():
            return False
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ('Flags', wintypes.DWORD),
                ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME),
                ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)),
                ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD),
                ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR),
                ('UserName', wintypes.LPWSTR),
            ]

        advapi32 = ctypes.windll.advapi32
        blob_buffer = (ctypes.c_byte * len(blob))(*blob)
        new_cred = CREDENTIAL()
        new_cred.Flags = 0
        new_cred.Type = 1  # CRED_TYPE_GENERIC
        new_cred.TargetName = cls.TARGET
        new_cred.Comment = None
        new_cred.CredentialBlobSize = len(blob)
        new_cred.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_byte))
        new_cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        new_cred.AttributeCount = 0
        new_cred.Attributes = None
        new_cred.TargetAlias = None
        new_cred.UserName = username
        return bool(advapi32.CredWriteW(ctypes.byref(new_cred), 0))

    @classmethod
    def delete_credential(cls) -> bool:
        if not cls.is_supported():
            return False
        import ctypes
        advapi32 = ctypes.windll.advapi32
        return bool(advapi32.CredDeleteW(cls.TARGET, 1, 0))


from engine.models import now


class GoogleAccountHealth(str, Enum):
    HEALTHY = 'healthy'
    RATE_LIMITED = 'rate-limited'
    QUOTA_BLOCKED = 'quota-blocked'
    ERROR = 'error'
    UNKNOWN = 'unknown'
    DISABLED = 'disabled'


class GoogleAccountProfile(BaseModel):
    account_id: str
    email: str
    auth_dir: str
    model_eligibility: list[str] = Field(default_factory=lambda: ['*'])
    last_successful_use: Optional[str] = None
    last_error: Optional[str] = None
    last_run_error: Optional[str] = None
    health_state: GoogleAccountHealth = GoogleAccountHealth.UNKNOWN
    cooldown_until: Optional[str] = None
    usage_5h: Optional[dict] = None
    usage_weekly: Optional[dict] = None
    active_agents: int = 0
    max_concurrent: int = 4
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)

    def is_cooldown_active(self) -> bool:
        if not self.cooldown_until:
            return False
        try:
            dt = datetime.fromisoformat(self.cooldown_until)
            return datetime.now(timezone.utc) < dt
        except (ValueError, TypeError):
            return False

    def remaining_cooldown_seconds(self) -> int:
        if not self.cooldown_until:
            return 0
        try:
            dt = datetime.fromisoformat(self.cooldown_until)
            diff = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(0, int(diff))
        except (ValueError, TypeError):
            return 0

    def is_eligible_for_model(self, model: str) -> bool:
        if '*' in self.model_eligibility:
            return True
        model_slug = model.split('/', 1)[-1] if '/' in model else model
        for pattern in self.model_eligibility:
            pat_slug = pattern.split('/', 1)[-1] if '/' in pattern else pattern
            if pat_slug == '*' or pat_slug.casefold() == model_slug.casefold():
                return True
        return False


class GoogleAuthPool:
    def __init__(self, data_dir: Optional[Path] = None, config=settings):
        self.config = config
        self.data_dir = Path(data_dir or DATA_DIR).resolve()
        self.registry_file = self.data_dir / 'google_accounts.json'
        self.auth_base_dir = self.data_dir / 'auth' / 'google'
        self.auth_base_dir.mkdir(parents=True, exist_ok=True)
        self.accounts: Dict[str, GoogleAccountProfile] = {}
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        self.credential_lock = asyncio.Lock()
        self.login_pending = None
        self._login_previous = None
        self.login_done = asyncio.Event()
        self.login_done.set()
        self.load()

    def load(self):
        if self.registry_file.exists():
            try:
                data = json.loads(self.registry_file.read_text(encoding='utf-8-sig'))
                self.accounts = {
                    item['account_id']: GoogleAccountProfile.model_validate(item)
                    for item in data.get('accounts', [])
                }
                # Older versions treated invalid CLI flags as failed Google logins.
                # Retain the diagnostic without claiming the credential was tested.
                for account in self.accounts.values():
                    if account.health_state == GoogleAccountHealth.ERROR and self.is_launch_error(account.last_error or ''):
                        account.last_run_error = account.last_error
                        account.last_error = None
                        account.health_state = GoogleAccountHealth.UNKNOWN
            except Exception as exc:
                raise RuntimeError("Google account registry is unreadable; restore or repair it before starting") from exc
        else:
            self.accounts = {}

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp = self.registry_file.with_suffix('.tmp')
        payload = {
            'accounts': [acc.model_dump() for acc in self.accounts.values()],
            'updated_at': now(),
        }
        temp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        temp.replace(self.registry_file)

    def public(self, include_identity=True) -> List[dict]:
        """Public view of accounts for dashboard and MCP clients (no plaintext secrets)."""
        result = []
        for acc in self.accounts.values():
            d = acc.model_dump()
            d['credential_saved'] = self.has_credential(acc)
            d['in_cooldown'] = acc.is_cooldown_active()
            for attr, seconds in [('usage_5h', 18000), ('usage_weekly', 604800)]:
                usage = getattr(acc, attr) or {}
                if 'observed_turns' in usage:
                    cutoff = datetime.now(timezone.utc).timestamp() - seconds
                    d[attr] = {'turns':sum(t > cutoff for t in usage['observed_turns']), 'source':'local observations'}
                else: d[attr] = None
            d['remaining_cooldown_seconds'] = acc.remaining_cooldown_seconds()
            if not include_identity:
                # Models need scheduler availability, not owner email addresses,
                # native credential paths, or provider error transcripts.
                fields = {'account_id', 'health_state', 'enabled', 'active_agents',
                          'max_concurrent', 'credential_saved', 'in_cooldown',
                          'remaining_cooldown_seconds', 'usage_5h', 'usage_weekly'}
                d = {key: value for key, value in d.items() if key in fields}
            result.append(d)
        return result

    def get_account_dir(self, account_id: str) -> Path:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', account_id):
            raise ValueError('Account ID must contain only letters, digits, underscores or hyphens')
        folder = self.auth_base_dir / account_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def provision_account_dir(self, account_id: str) -> Path:
        """Sets up the isolated .gemini settings and projects directories for an account."""
        auth_dir = self.get_account_dir(account_id)
        cli_settings_dir = auth_dir / '.gemini' / 'antigravity-cli'
        cli_settings_dir.mkdir(parents=True, exist_ok=True)
        settings_file = cli_settings_dir / 'settings.json'
        if not settings_file.exists():
            settings_file.write_text(json.dumps({'permissions': {'allow': ['mcp(agentic_team/*)', 'command(*)']}}, indent=2), encoding='utf-8')
        projects_dir = auth_dir / '.gemini' / 'config' / 'projects'
        projects_dir.mkdir(parents=True, exist_ok=True)
        return auth_dir

    def register_account(
        self,
        email: str,
        account_id: Optional[str] = None,
        model_eligibility: Optional[List[str]] = None,
        max_concurrent: int = 4,
    ) -> GoogleAccountProfile:
        if not account_id:
            base_id = re.sub(r'[^a-zA-Z0-9_]', '_', email.split('@')[0]).lower()
            candidate = base_id
            counter = 1
            while candidate in self.accounts:
                candidate = f"{base_id}_{counter}"
                counter += 1
            account_id = candidate
        if account_id in self.accounts:
            raise ValueError(f"Account ID '{account_id}' already exists")

        auth_dir = self.provision_account_dir(account_id)
        profile = GoogleAccountProfile(
            account_id=account_id,
            email=email,
            auth_dir=str(auth_dir.relative_to(self.data_dir)),
            model_eligibility=model_eligibility or ['*'],
            health_state=GoogleAccountHealth.UNKNOWN,
            max_concurrent=max(1, min(max_concurrent, 10)),
        )
        self.accounts[account_id] = profile
        self.save()
        return profile

    def remove_account(self, account_id: str, delete_files: bool = True) -> bool:
        if account_id not in self.accounts:
            raise ValueError(f"Account '{account_id}' not found")
        if self.accounts[account_id].active_agents or self.login_pending == account_id:
            raise ValueError("Account is in use")
        acc = self.accounts.pop(account_id)
        if delete_files:
            dir_path = self.resolve_auth_dir(acc)
            if dir_path.exists() and dir_path.is_relative_to(self.auth_base_dir):
                shutil.rmtree(dir_path, ignore_errors=True)
        self.save()
        return True

    def disable_account(self, account_id: str) -> GoogleAccountProfile:
        if account_id not in self.accounts:
            raise ValueError(f"Account '{account_id}' not found")
        acc = self.accounts[account_id]
        acc.health_state = GoogleAccountHealth.DISABLED
        acc.updated_at = now()
        self.save()
        return acc

    def enable_account(self, account_id: str) -> GoogleAccountProfile:
        if account_id not in self.accounts:
            raise ValueError(f"Account '{account_id}' not found")
        acc = self.accounts[account_id]
        acc.health_state = GoogleAccountHealth.UNKNOWN
        acc.cooldown_until = None
        acc.last_error = None
        acc.updated_at = now()
        self.save()
        return acc

    def resolve_auth_dir(self, profile: GoogleAccountProfile) -> Path:
        p = Path(profile.auth_dir)
        resolved = (self.data_dir / p).resolve() if not p.is_absolute() else p.resolve()
        if not resolved.is_relative_to(self.auth_base_dir.resolve()) or resolved == self.auth_base_dir.resolve():
            raise ValueError('Account directory is outside the Google profile directory')
        return resolved

    def has_credential(self, account):
        return (self.resolve_auth_dir(account) / 'credential.dat').is_file()

    def get_semaphore(self, account_id: str, max_concurrent: int = 4) -> asyncio.Semaphore:
        if account_id not in self._semaphores:
            self._semaphores[account_id] = asyncio.Semaphore(max_concurrent)
        return self._semaphores[account_id]

    def acquire_slot(
        self,
        agent,
        exclude: Optional[List[str]] = None,
    ) -> GoogleAccountProfile:
        """Selects the best available account profile according to the scheduler score.
        Do NOT rotate during an ongoing request.
        """
        exclude = set(exclude or [])
        forced = getattr(agent, 'forced_auth_slot_id', None)
        if forced:
            if forced not in self.accounts:
                raise ValueError(f"Forced account '{forced}' is not registered in Google Auth Pool")
            acc = self.accounts[forced]
            if forced in exclude or acc.health_state in (GoogleAccountHealth.DISABLED, GoogleAccountHealth.ERROR):
                raise ValueError(f"Forced account '{forced}' is unavailable")
            if not acc.is_eligible_for_model(agent.model) or acc.active_agents >= acc.max_concurrent:
                raise ValueError(f"Forced account '{forced}' cannot accept this model/turn")
            if not self.has_credential(acc):
                raise ValueError(f"Forced account '{forced}' has no saved credential")
            if acc.is_cooldown_active():
                raise ValueError(f"Forced account '{forced}' is quota-blocked until {acc.cooldown_until}")
            acc.active_agents += 1
            acc.updated_at = now()
            self.save()
            return acc

        # Sticky session / Account affinity:
        # Keep agent on its current account to maximize server-side prompt / KV caching.
        current_slot_id = getattr(agent, 'auth_slot_id', None)
        if current_slot_id and current_slot_id not in exclude and current_slot_id in self.accounts:
            current_acc = self.accounts[current_slot_id]
            if (current_acc.health_state not in (GoogleAccountHealth.DISABLED, GoogleAccountHealth.ERROR)
                    and self.has_credential(current_acc)
                    and not current_acc.is_cooldown_active()
                    and current_acc.is_eligible_for_model(agent.model)
                    and current_acc.active_agents < current_acc.max_concurrent):
                current_acc.active_agents += 1
                current_acc.updated_at = now()
                self.save()
                return current_acc

        candidates = []
        for acc in self.accounts.values():
            if acc.account_id in exclude:
                continue
            if acc.health_state in (GoogleAccountHealth.DISABLED, GoogleAccountHealth.ERROR):
                continue
            if not self.has_credential(acc) or acc.is_cooldown_active():
                continue
            # Reset cooldown if expired
            if acc.cooldown_until and not acc.is_cooldown_active():
                acc.cooldown_until = None
                if acc.health_state in (GoogleAccountHealth.QUOTA_BLOCKED, GoogleAccountHealth.RATE_LIMITED):
                    acc.health_state = GoogleAccountHealth.HEALTHY
            if not acc.is_eligible_for_model(agent.model):
                continue
            if acc.active_agents >= acc.max_concurrent:
                continue
            candidates.append(acc)

        if not candidates:
            # Check if any account will reset soon
            earliest = self.earliest_reset()
            msg = "No healthy Google accounts available."
            if earliest:
                msg += f" Earliest quota reset at {earliest}."
            raise RuntimeError(msg)

        # Scheduler scoring:
        # Prefer:
        # 1. Fewest active agents
        # 2. Lowest observed short-window usage (turns or tokens if available)
        # 3. Lowest observed weekly usage
        # 4. Longest time since last use
        def score(acc: GoogleAccountProfile):
            active_score = acc.active_agents * 10000
            short_usage = 0
            if acc.usage_5h and isinstance(acc.usage_5h, dict):
                short_usage = acc.usage_5h.get('pct', 0) * 100 or acc.usage_5h.get('turns', 0) * 10
            weekly_usage = 0
            if acc.usage_weekly and isinstance(acc.usage_weekly, dict):
                weekly_usage = acc.usage_weekly.get('pct', 0) * 50 or acc.usage_weekly.get('turns', 0) * 5
            error_penalty = 5000 if acc.health_state == GoogleAccountHealth.ERROR else 0
            return active_score + short_usage + weekly_usage + error_penalty

        candidates.sort(key=score)
        selected = candidates[0]
        selected.active_agents += 1
        selected.updated_at = now()
        self.save()
        return selected

    def release_slot(self, account_id: str):
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            acc.active_agents = max(0, acc.active_agents - 1)
            acc.updated_at = now()
            self.save()

    def classify_error(self, exc: Exception) -> Tuple[bool, int, Optional[str]]:
        """Classifies an error into retryable vs non-retryable.
        Returns (is_retryable, cooldown_seconds, parsed_reset_time_iso).
        """
        text = str(exc)
        text_lower = text.lower()

        # Non-retryable patterns
        non_retryable_terms = [
            'authentication revoked', 'invalid_grant', 'unauthorized',
            'permission denied', 'permission_denied', '403 forbidden',
            'malformed request', 'invalid argument', 'invalid_argument',
            'model unavailable', 'model not found', 'not configured',
            'arbitrary shell shims are unsupported', 'unsupported glm coding plan',
        ]
        for term in non_retryable_terms:
            if term in text_lower:
                return False, 0, None

        if any(term in text_lower for term in ('model_capacity_exhausted', 'capacity exhausted', 'prefill_queue_overloaded')):
            return False, 0, None  # Provider capacity is not an account quota.

        # Retryable quota / rate-limit patterns
        retryable_terms = [
            '429', 'resource_exhausted', 'resourceexhausted',
            'quota exceeded', 'quota-blocked', 'quota reached',
            'rate limit', 'ratelimit', 'too many requests',
            'individual quota exhausted', 'capacity exhausted',
            'model_capacity_exhausted', 'prefill_queue_overloaded'
        ]
        is_retryable = any(term in text_lower for term in retryable_terms)
        if not is_retryable:
            return False, 0, None

        # Attempt to parse reset duration or timestamp
        cooldown_seconds = 1800  # Default 30 mins
        parsed_iso = None

        # Pattern: "reset in 41m", "reset in 2 hours", "reset in 300s"
        m_reset = re.search(r'reset\s+in\s+(\d+)\s*(m|min|minutes|s|sec|seconds|h|hours|d|days)?', text, re.IGNORECASE)
        if m_reset:
            val = int(m_reset.group(1))
            unit = (m_reset.group(2) or 'm').lower()
            if unit.startswith('s'):
                cooldown_seconds = val
            elif unit.startswith('h'):
                cooldown_seconds = val * 3600
            elif unit.startswith('d'):
                cooldown_seconds = val * 86400
            else:
                cooldown_seconds = val * 60

        # Pattern: "retry after (\d+) seconds" or "retry-after: (\d+)"
        m_retry = re.search(r'retry[- ]after[:\s]+(\d+)', text, re.IGNORECASE)
        if m_retry:
            cooldown_seconds = int(m_retry.group(1))

        # Pattern: ISO timestamp "after 2026-09-20T17:00:00Z"
        m_iso = re.search(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)', text)
        if m_iso:
            parsed_iso = m_iso.group(1)
            try:
                dt = datetime.fromisoformat(parsed_iso.replace('Z', '+00:00'))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                parsed_iso = dt.isoformat()
                diff = (dt - datetime.now(timezone.utc)).total_seconds()
                cooldown_seconds = max(60, int(diff))
            except Exception:
                pass

        if not parsed_iso:
            target_dt = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
            parsed_iso = target_dt.isoformat()

        return True, cooldown_seconds, parsed_iso

    def mark_quota_blocked(
        self,
        account_id: str,
        error_text: str,
        cooldown_seconds: int = 1800,
        reset_time_iso: Optional[str] = None
    ):
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            acc.health_state = GoogleAccountHealth.QUOTA_BLOCKED
            acc.last_error = error_text[:500]
            if not reset_time_iso:
                dt = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
                reset_time_iso = dt.isoformat()
            acc.cooldown_until = reset_time_iso
            acc.updated_at = now()
            self.save()

    def mark_success(self, account_id: str, turn_tokens: int = 0):
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            acc.health_state = GoogleAccountHealth.HEALTHY
            acc.last_successful_use = now()
            acc.last_error = None
            acc.cooldown_until = None
            for attr, seconds in [('usage_5h', 18000), ('usage_weekly', 604800)]:
                usage = getattr(acc, attr) or {}
                cutoff = datetime.now(timezone.utc).timestamp() - seconds
                samples = [t for t in usage.get('observed_turns', []) if t > cutoff]
                samples.append(datetime.now(timezone.utc).timestamp())
                setattr(acc, attr, {'turns': len(samples), 'observed_turns': samples,
                                    'source': 'local completed turns; provider quota unknown'})
            acc.updated_at = now()
            self.save()

    @staticmethod
    def is_launch_error(text):
        return any(term in text.lower() for term in ('invalid model selection', 'requires --effort', 'unknown flag', 'unrecognized arguments'))

    def mark_error(self, account_id: str, error_text: str, is_fatal: bool = False):
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            authentication_error = any(term in error_text.lower() for term in
                ('authentication revoked', 'invalid_grant', 'unauthorized', 'invalid credentials', '401 unauthorized'))
            if (is_fatal or authentication_error) and not self.is_launch_error(error_text):
                acc.last_error = error_text[:500]
                acc.health_state = GoogleAccountHealth.ERROR
            else:
                acc.last_run_error = error_text[:500]
            acc.updated_at = now()
            self.save()

    def earliest_reset(self) -> Optional[str]:
        resets = []
        for acc in self.accounts.values():
            if acc.is_cooldown_active() and acc.cooldown_until:
                resets.append(acc.cooldown_until)
        if resets:
            resets.sort()
            return resets[0]
        return None

    def import_default_account(self, email: str = 'default@google.com') -> GoogleAccountProfile:
        """Imports existing default ~/.gemini configuration into an isolated profile."""
        account_id = 'default'
        if account_id in self.accounts:
            account_id = f"default_{uuid.uuid4().hex[:4]}"
        auth_dir = self.provision_account_dir(account_id)
        source_gemini = Path.home() / '.gemini'
        if source_gemini.exists():
            # Copy settings and projects if present
            src_cli_settings = source_gemini / 'antigravity-cli' / 'settings.json'
            dest_cli_settings = auth_dir / '.gemini' / 'antigravity-cli' / 'settings.json'
            if src_cli_settings.exists():
                shutil.copy2(src_cli_settings, dest_cli_settings)
            src_projects = source_gemini / 'config' / 'projects'
            dest_projects = auth_dir / '.gemini' / 'config' / 'projects'
            if src_projects.exists():
                shutil.copytree(src_projects, dest_projects, dirs_exist_ok=True)

        profile = GoogleAccountProfile(
            account_id=account_id,
            email=email,
            auth_dir=str(auth_dir.relative_to(self.data_dir)),
            model_eligibility=['*'],
            health_state=GoogleAccountHealth.UNKNOWN,
        )
        self.accounts[account_id] = profile
        self.save()
        return profile

    @staticmethod
    def _extract_email(blob: bytes) -> Optional[str]:
        try:
            import json, base64
            data = json.loads(blob.decode('utf-8'))
            id_tok = data.get('id_token') or data.get('token', {}).get('id_token')
            if id_tok:
                parts = id_tok.split('.')
                if len(parts) >= 2:
                    payload = parts[1] + '=' * (-len(parts[1]) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(payload.encode('utf-8')).decode('utf-8'))
                    return claims.get('email')
        except Exception:
            pass
        return None

    def sync_accounts(self):
        """Read-only refresh. A GET must never replace credentials or infer authentication."""
        return self.public()

    @asynccontextmanager
    async def credential_lease(self, account_id=None):
        # Antigravity uses a single Windows vault key even with separate HOME dirs.
        while True:
            await self.login_done.wait()
            await self.credential_lock.acquire()
            if not self.login_pending: break
            self.credential_lock.release()
        previous = None
        captured = False
        try:
            previous = WindowsKeyringHelper.read_credential()
            captured = True
            if account_id and not self.activate_account_credential(account_id):
                raise RuntimeError('Could not activate saved Google credential; capture its login again')
            yield
        finally:
            try:
                if captured and previous:
                    if not WindowsKeyringHelper.write_credential(previous[1], previous[0]):
                        raise RuntimeError('Could not restore the previous Google credential')
                elif captured and self.accounts:
                    WindowsKeyringHelper.delete_credential()
            finally:
                self.credential_lock.release()

    def retain_refreshed_credential(self, account_id):
        cred = WindowsKeyringHelper.read_credential()
        account = self.accounts[account_id]
        # Only associate refreshed credentials when identity matches this slot.
        if cred and self._extract_email(cred[1]) == account.email:
            path = self.resolve_auth_dir(account) / 'credential.dat'
            save_credential(path, cred[1])

    def cancel_login(self):
        if self.login_pending:
            if self._login_previous:
                if not WindowsKeyringHelper.write_credential(self._login_previous[1], self._login_previous[0]):
                    raise ValueError('Could not restore the previous Google credential')
            else:
                WindowsKeyringHelper.delete_credential()
        self._login_previous = None
        self.login_pending = None
        self.login_done.set()

    def save_account_credential(self, account_id: str) -> bool:
        """Captures the current active Windows credential blob into the account's folder."""
        if account_id not in self.accounts:
            return False
        if self.credential_lock.locked():
            raise ValueError('Google credential is in use by an active turn')
        acc = self.accounts[account_id]
        auth_dir = self.resolve_auth_dir(acc)
        auth_dir.mkdir(parents=True, exist_ok=True)
        cred = WindowsKeyringHelper.read_credential()
        if cred:
            username, blob = cred
            email = self._extract_email(blob)
            placeholder = acc.email.startswith('Account ') or acc.email == 'default@google.com'
            if not email or (not placeholder and email.casefold() != acc.email.casefold()):
                raise ValueError('Signed-in identity does not match this Google account profile')
            save_credential(auth_dir / 'credential.dat', blob)
            acc.health_state = GoogleAccountHealth.UNKNOWN
            email = self._extract_email(blob)
            if email:
                acc.email = email
            acc.updated_at = now()
            self.save()
            if self.login_pending == account_id: self.cancel_login()
            return True
        return False

    def activate_account_credential(self, account_id: str) -> bool:
        """Restores the account's credential blob into the Windows Credential Manager before running CLI."""
        if account_id not in self.accounts:
            return False
        acc = self.accounts[account_id]
        auth_dir = self.resolve_auth_dir(acc)
        cred_file = auth_dir / 'credential.dat'
        if cred_file.is_file():
            saved = cred_file.read_bytes()
            blob = unprotect(saved)
            if not saved.startswith(PREFIX): save_credential(cred_file, blob)
            return WindowsKeyringHelper.write_credential(blob)
        return False

    def prepare_login_environment(self, account_id: str, fresh_login: bool = True) -> Tuple[List[str], dict]:
        """Prepares the command and environment for an interactive terminal login session for a specific account."""
        if self.credential_lock.locked() or self.login_pending:
            raise ValueError('Google is in use. Finish the active turn or login before signing in.')
        if account_id not in self.accounts:
            raise ValueError(f"Account '{account_id}' not found")
        acc = self.accounts[account_id]
        auth_dir = self.resolve_auth_dir(acc)
        self.provision_account_dir(account_id)

        from harness.cli_runner import CLIRunner
        cli = CLIRunner(self.config)
        argv = cli.resolve('agy')  # Validate before changing the shared credential.
        previous = WindowsKeyringHelper.read_credential()
        cred_file = auth_dir / 'credential.dat'
        if cred_file.is_file() and not fresh_login:
            if not WindowsKeyringHelper.write_credential(unprotect(cred_file.read_bytes())):
                raise ValueError('Could not activate the selected credential')
        else:
            if previous:
                email = self._extract_email(previous[1])
                for profile in self.accounts.values():
                    path = self.resolve_auth_dir(profile) / 'credential.dat'
                    if email and profile.email == email and not path.is_file():
                        save_credential(path, previous[1])
                if not WindowsKeyringHelper.delete_credential():
                    raise ValueError('Could not prepare the Google sign-in credential slot')
        self._login_previous = previous

        env = os.environ.copy()
        # Clean team credentials
        for key in ('TEAM_TOKEN', 'TEAM_ENDPOINT', 'TEAM_OWNER_TOKEN', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'ANTIGRAVITY_API_KEY'):
            env.pop(key, None)
        # Isolate Google Antigravity config
        env['USERPROFILE'] = str(auth_dir)
        env['HOME'] = str(auth_dir)
        env['ANTIGRAVITY_APP_DATA_DIR'] = str(auth_dir / '.gemini' / 'antigravity')

        self.login_pending = account_id
        self.login_done.clear()
        return argv, env

