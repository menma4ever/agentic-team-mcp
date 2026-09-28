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
    AUTH_VERIFICATION_REQUIRED = 'auth-verification-required'
    TRANSIENT_ERROR = 'transient-error'
    ERROR = 'error'
    UNKNOWN = 'unknown'
    DISABLED = 'disabled'


def is_claude_model(model: Optional[str] = None) -> bool:
    if not model:
        return False
    m = model.lower()
    return 'claude' in m or 'opus' in m or 'sonnet' in m or 'haiku' in m


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
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    active_agents: int = 0
    claude_health_state: GoogleAccountHealth = GoogleAccountHealth.HEALTHY
    claude_cooldown_until: Optional[str] = None
    claude_last_error: Optional[str] = None
    claude_last_successful_use: Optional[str] = None
    claude_usage_5h: Optional[dict] = None
    claude_usage_weekly: Optional[dict] = None
    claude_input_tokens: int = 0
    claude_output_tokens: int = 0
    claude_cache_read_tokens: int = 0
    claude_active_agents: int = 0
    max_concurrent: int = 4
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)

    def effective_health(self, model: Optional[str] = None) -> GoogleAccountHealth:
        if self.health_state in (GoogleAccountHealth.DISABLED, GoogleAccountHealth.ERROR, GoogleAccountHealth.AUTH_VERIFICATION_REQUIRED):
            return self.health_state
        if is_claude_model(model):
            return self.claude_health_state if self.claude_health_state != GoogleAccountHealth.UNKNOWN else GoogleAccountHealth.HEALTHY
        return self.health_state

    def is_cooldown_active(self, model: Optional[str] = None) -> bool:
        target_cd = self.claude_cooldown_until if is_claude_model(model) else self.cooldown_until
        if not target_cd:
            return False
        try:
            dt = datetime.fromisoformat(target_cd.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < dt
        except (ValueError, TypeError):
            return False

    def remaining_cooldown_seconds(self, model: Optional[str] = None) -> int:
        target_cd = self.claude_cooldown_until if is_claude_model(model) else self.cooldown_until
        if not target_cd:
            return 0
        try:
            dt = datetime.fromisoformat(target_cd.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            diff = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(0, int(diff))
        except (ValueError, TypeError):
            return 0

    def remaining_cooldown_formatted(self, model: Optional[str] = None) -> str:
        sec = self.remaining_cooldown_seconds(model=model)
        if sec <= 0:
            return "Ready"
        hours = sec // 3600
        mins = (sec % 3600) // 60
        secs = sec % 60
        if hours > 0:
            return f"{hours}h {mins}m"
        elif mins > 0:
            return f"{mins}m {secs}s"
        else:
            return f"{secs}s"

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
        self._vault_baseline = None
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

                    # Auto-heal stale, expired or false-cooldown states:
                    if account.health_state in (GoogleAccountHealth.QUOTA_BLOCKED, GoogleAccountHealth.RATE_LIMITED, GoogleAccountHealth.TRANSIENT_ERROR):
                        if account.last_error and ('[agy] print timeout' in account.last_error or 'print timeout after' in account.last_error):
                            account.health_state = GoogleAccountHealth.HEALTHY
                            account.cooldown_until = None
                            account.last_error = None
                        elif account.last_error and ('82h26m52s' in account.last_error or account.last_error.startswith('API error (attempt ')):
                            account.health_state = GoogleAccountHealth.HEALTHY
                            account.cooldown_until = None
                            account.last_error = None
                        elif not account.is_cooldown_active(None):
                            account.health_state = GoogleAccountHealth.HEALTHY
                            account.cooldown_until = None
                            account.last_error = None
                    if account.claude_health_state in (GoogleAccountHealth.QUOTA_BLOCKED, GoogleAccountHealth.RATE_LIMITED, GoogleAccountHealth.TRANSIENT_ERROR):
                        if not account.is_cooldown_active('claude'):
                            account.claude_health_state = GoogleAccountHealth.HEALTHY
                            account.claude_cooldown_until = None
                            account.claude_last_error = None
            except Exception as exc:
                raise RuntimeError("Google account registry is unreadable; restore or repair it before starting") from exc
        else:
            self.accounts = {}
        self._registry_mtime = self.registry_file.stat().st_mtime if self.registry_file.exists() else 0

    def maybe_reload(self):
        if self.registry_file.exists():
            try:
                mtime = self.registry_file.stat().st_mtime
                if mtime > getattr(self, '_registry_mtime', 0):
                    self.load()
            except Exception:
                pass

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp = self.registry_file.with_suffix('.tmp')
        payload = {
            'accounts': [acc.model_dump() for acc in self.accounts.values()],
            'updated_at': now(),
        }
        temp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        temp.replace(self.registry_file)
        try:
            self._registry_mtime = self.registry_file.stat().st_mtime
        except Exception:
            pass

    def heal_expired_cooldowns(self) -> List[str]:
        """Checks all accounts and transitions any expired cooldowns or false blocks to HEALTHY.
        Returns list of healed account_ids.
        """
        healed = []
        changed = False
        for acc in self.accounts.values():
            if acc.health_state in (GoogleAccountHealth.QUOTA_BLOCKED, GoogleAccountHealth.RATE_LIMITED, GoogleAccountHealth.TRANSIENT_ERROR):
                if acc.last_error and (
                    '[agy] print timeout' in acc.last_error
                    or 'print timeout after' in acc.last_error
                    or '82h26m52s' in acc.last_error
                    or acc.last_error.startswith('API error (attempt ')
                ):
                    acc.health_state = GoogleAccountHealth.HEALTHY
                    acc.cooldown_until = None
                    acc.last_error = None
                    acc.updated_at = now()
                    healed.append(acc.account_id)
                    changed = True
                elif not acc.is_cooldown_active(None):
                    acc.health_state = GoogleAccountHealth.HEALTHY
                    acc.cooldown_until = None
                    acc.last_error = None
                    acc.updated_at = now()
                    healed.append(acc.account_id)
                    changed = True
            if acc.claude_health_state in (GoogleAccountHealth.QUOTA_BLOCKED, GoogleAccountHealth.RATE_LIMITED, GoogleAccountHealth.TRANSIENT_ERROR):
                if not acc.is_cooldown_active('claude'):
                    acc.claude_health_state = GoogleAccountHealth.HEALTHY
                    acc.claude_cooldown_until = None
                    acc.claude_last_error = None
                    acc.updated_at = now()
                    if acc.account_id not in healed:
                        healed.append(acc.account_id)
                    changed = True
        if changed:
            self.save()
        return healed

    def public(self, include_identity=True) -> List[dict]:
        """Public view of accounts for dashboard and MCP clients (no plaintext secrets)."""
        self.maybe_reload()
        self.heal_expired_cooldowns()
        result = []
        for acc in self.accounts.values():
            d = acc.model_dump()
            d['credential_saved'] = self.has_credential(acc)
            d['in_cooldown'] = acc.is_cooldown_active(None)
            d['claude_in_cooldown'] = acc.is_cooldown_active('claude')
            d['claude_health_state'] = acc.effective_health('claude').value
            for attr, seconds in [
                ('usage_5h', 18000),
                ('usage_weekly', 604800),
                ('claude_usage_5h', 18000),
                ('claude_usage_weekly', 604800),
            ]:
                usage = getattr(acc, attr) or {}
                if 'observed_turns' in usage:
                    cutoff = datetime.now(timezone.utc).timestamp() - seconds
                    turns = 0
                    in_tok = 0
                    out_tok = 0
                    cache_tok = 0
                    for s in usage['observed_turns']:
                        ts = s.get('t', 0) if isinstance(s, dict) else s
                        if ts > cutoff:
                            turns += 1
                            if isinstance(s, dict):
                                in_tok += s.get('in', 0)
                                out_tok += s.get('out', 0)
                                cache_tok += s.get('cache', 0)
                    if turns > 0 and in_tok == 0 and out_tok == 0 and cache_tok == 0:
                        is_claude_attr = 'claude' in attr
                        acc_in = acc.claude_input_tokens if is_claude_attr else acc.input_tokens
                        acc_out = acc.claude_output_tokens if is_claude_attr else acc.output_tokens
                        acc_cache = acc.claude_cache_read_tokens if is_claude_attr else acc.cache_read_tokens
                        if acc_in or acc_out or acc_cache:
                            in_tok = acc_in
                            out_tok = acc_out
                            cache_tok = acc_cache
                    d[attr] = {
                        'turns': turns,
                        'input_tokens': in_tok,
                        'output_tokens': out_tok,
                        'cache_read_tokens': cache_tok,
                        'total_tokens': in_tok + out_tok + cache_tok,
                        'source': 'local observations'
                    }
                else:
                    d[attr] = None
            d['remaining_cooldown_seconds'] = acc.remaining_cooldown_seconds(None)
            d['remaining_cooldown_formatted'] = acc.remaining_cooldown_formatted(None)
            d['claude_remaining_cooldown_seconds'] = acc.remaining_cooldown_seconds('claude')
            d['claude_remaining_cooldown_formatted'] = acc.remaining_cooldown_formatted('claude')
            d['input_tokens'] = acc.input_tokens
            d['output_tokens'] = acc.output_tokens
            d['cache_read_tokens'] = acc.cache_read_tokens
            d['claude_input_tokens'] = acc.claude_input_tokens
            d['claude_output_tokens'] = acc.claude_output_tokens
            d['claude_cache_read_tokens'] = acc.claude_cache_read_tokens
            if not include_identity:
                fields = {
                    'account_id', 'health_state', 'enabled', 'active_agents',
                    'max_concurrent', 'credential_saved', 'in_cooldown',
                    'remaining_cooldown_seconds', 'remaining_cooldown_formatted',
                    'input_tokens', 'output_tokens', 'cache_read_tokens',
                    'usage_5h', 'usage_weekly',
                    'claude_health_state', 'claude_in_cooldown', 'claude_active_agents',
                    'claude_remaining_cooldown_seconds', 'claude_remaining_cooldown_formatted',
                    'claude_input_tokens', 'claude_output_tokens', 'claude_cache_read_tokens',
                    'claude_usage_5h', 'claude_usage_weekly',
                }
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
        self.maybe_reload()
        self.heal_expired_cooldowns()
        exclude = set(exclude or [])
        agent_model = getattr(agent, 'model', None)
        is_claude = is_claude_model(agent_model)
        forced = getattr(agent, 'forced_auth_slot_id', None)
        if forced:
            if forced not in self.accounts:
                raise ValueError(f"Forced account '{forced}' is not registered in Google Auth Pool")
            acc = self.accounts[forced]
            if forced in exclude or acc.effective_health(agent_model) in (GoogleAccountHealth.DISABLED, GoogleAccountHealth.ERROR, GoogleAccountHealth.AUTH_VERIFICATION_REQUIRED):
                raise ValueError(f"Forced account '{forced}' is unavailable")
            total_active = acc.active_agents + acc.claude_active_agents
            if not acc.is_eligible_for_model(agent_model) or total_active >= acc.max_concurrent:
                raise ValueError(f"Forced account '{forced}' cannot accept this model/turn")
            if not self.has_credential(acc):
                raise ValueError(f"Forced account '{forced}' has no saved credential")
            if acc.is_cooldown_active(agent_model):
                cd_str = acc.claude_cooldown_until if is_claude else acc.cooldown_until
                raise ValueError(f"Forced account '{forced}' is quota-blocked until {cd_str}")
            if is_claude:
                acc.claude_active_agents += 1
            else:
                acc.active_agents += 1
            acc.updated_at = now()
            self.save()
            self.capture_vault_baseline()
            return acc

        # Sticky session / Account affinity:
        # Keep agent on its current account to maximize server-side prompt / KV caching.
        current_slot_id = getattr(agent, 'auth_slot_id', None)
        if current_slot_id and current_slot_id not in exclude and current_slot_id in self.accounts:
            current_acc = self.accounts[current_slot_id]
            total_active = current_acc.active_agents + current_acc.claude_active_agents
            if (current_acc.effective_health(agent_model) not in (GoogleAccountHealth.DISABLED, GoogleAccountHealth.ERROR, GoogleAccountHealth.AUTH_VERIFICATION_REQUIRED)
                    and self.has_credential(current_acc)
                    and not current_acc.is_cooldown_active(agent_model)
                    and current_acc.is_eligible_for_model(agent_model)
                    and total_active < current_acc.max_concurrent):
                if is_claude:
                    current_acc.claude_active_agents += 1
                else:
                    current_acc.active_agents += 1
                current_acc.updated_at = now()
                self.save()
                self.capture_vault_baseline()
                return current_acc

        candidates = []
        for acc in self.accounts.values():
            if acc.account_id in exclude:
                continue
            if acc.effective_health(agent_model) in (GoogleAccountHealth.DISABLED, GoogleAccountHealth.ERROR, GoogleAccountHealth.AUTH_VERIFICATION_REQUIRED):
                continue
            if not self.has_credential(acc) or acc.is_cooldown_active(agent_model):
                continue
            if not acc.is_eligible_for_model(agent_model):
                continue
            if (acc.active_agents + acc.claude_active_agents) >= acc.max_concurrent:
                continue
            candidates.append(acc)

        if not candidates:
            earliest = self.earliest_reset(model=agent_model)
            msg = "No healthy Google accounts available."
            if earliest:
                msg += f" Earliest quota reset at {earliest}."
            raise RuntimeError(msg)

        def score(acc: GoogleAccountProfile):
            active_count = acc.claude_active_agents if is_claude else acc.active_agents
            active_score = active_count * 10000 + (acc.active_agents + acc.claude_active_agents) * 2000
            u5 = acc.claude_usage_5h if is_claude else acc.usage_5h
            uw = acc.claude_usage_weekly if is_claude else acc.usage_weekly
            short_usage = 0
            if u5 and isinstance(u5, dict):
                short_usage = u5.get('pct', 0) * 100 or u5.get('turns', 0) * 10
            weekly_usage = 0
            if uw and isinstance(uw, dict):
                weekly_usage = uw.get('pct', 0) * 50 or uw.get('turns', 0) * 5
            error_penalty = 5000 if acc.effective_health(agent_model) == GoogleAccountHealth.ERROR else 0
            return active_score + short_usage + weekly_usage + error_penalty

        candidates.sort(key=score)
        selected = candidates[0]
        if is_claude:
            selected.claude_active_agents += 1
        else:
            selected.active_agents += 1
        selected.updated_at = now()
        self.save()
        self.capture_vault_baseline()
        return selected

    def release_slot(self, account_id: str, model: Optional[str] = None):
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            if is_claude_model(model):
                acc.claude_active_agents = max(0, acc.claude_active_agents - 1)
            else:
                acc.active_agents = max(0, acc.active_agents - 1)
            acc.updated_at = now()
            self.save()
        if self.total_active_agents() == 0:
            self.restore_vault_baseline()

    def total_active_agents(self) -> int:
        return sum(acc.active_agents + acc.claude_active_agents for acc in self.accounts.values())

    def capture_vault_baseline(self):
        if self._vault_baseline is None:
            self._vault_baseline = WindowsKeyringHelper.read_credential()

    def restore_vault_baseline(self):
        if self._vault_baseline:
            try:
                WindowsKeyringHelper.write_credential(self._vault_baseline[1], self._vault_baseline[0])
            except Exception:
                pass
            self._vault_baseline = None

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
            'eligibility check failed', 'not eligible for antigravity', 'verify your account',
            '[agy] print timeout', 'print timeout after',
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
        has_explicit_reset = False
        cooldown_seconds = 1800  # Default 30 mins
        parsed_iso = None

        # Pattern: "reset in 41m", "reset in 2 hours", "reset in 300s"
        m_reset = re.search(r'resets?\s+in\s+((?:\d+(?:\.\d+)?\s*(?:days?|hours?|minutes?|seconds?|min|sec|[dhms])\s*)+)', text, re.IGNORECASE)
        if m_reset:
            units = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
            parsed_secs = max(1, int(sum(float(value) * units[unit[0].lower()]
                for value, unit in re.findall(r'(\d+(?:\.\d+)?)\s*(days?|hours?|minutes?|seconds?|min|sec|[dhms])',
                                             m_reset.group(1), re.IGNORECASE))))
            # Honor the exact reset duration returned by Google's API (5h rolling or 7d weekly window).
            cooldown_seconds = min(parsed_secs, 604800)
            has_explicit_reset = True

        # Pattern: "retry after (\d+) seconds" or "retry-after: (\d+)"
        m_retry = re.search(r'retry[- ]after[:\s]+(\d+)', text, re.IGNORECASE)
        if m_retry:
            cooldown_seconds = min(int(m_retry.group(1)), 604800)
            has_explicit_reset = True

        # Pattern: ISO timestamp "after 2026-09-20T17:00:00Z"
        m_iso = re.search(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)', text)
        if m_iso:
            parsed_iso = m_iso.group(1)
            try:
                dt = datetime.fromisoformat(parsed_iso.replace('Z', '+00:00'))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                diff = (dt - datetime.now(timezone.utc)).total_seconds()
                cooldown_seconds = min(max(60, int(diff)), 604800)
                has_explicit_reset = True
            except Exception:
                pass

        if not parsed_iso or has_explicit_reset:
            target_dt = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
            parsed_iso = target_dt.isoformat()

        return True, cooldown_seconds, parsed_iso

    def mark_quota_blocked(
        self,
        account_id: str,
        error_text: str,
        cooldown_seconds: int = 1800,
        reset_time_iso: Optional[str] = None,
        model: Optional[str] = None,
    ):
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            if not reset_time_iso:
                dt = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
                reset_time_iso = dt.isoformat()
            if is_claude_model(model):
                acc.claude_health_state = GoogleAccountHealth.QUOTA_BLOCKED
                acc.claude_last_error = error_text[:500]
                acc.claude_cooldown_until = reset_time_iso
            else:
                acc.health_state = GoogleAccountHealth.QUOTA_BLOCKED
                acc.last_error = error_text[:500]
                acc.cooldown_until = reset_time_iso
            acc.updated_at = now()
            self.save()

    def mark_transient_error(
        self,
        account_id: str,
        error_text: str,
        cooldown_seconds: int = 60,
        model: Optional[str] = None,
    ):
        """Transient errors (temporary 429, network blips, 5xx) must not create multi-day cooldowns."""
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            secs = min(max(1, cooldown_seconds), 300)
            dt = datetime.now(timezone.utc) + timedelta(seconds=secs)
            if is_claude_model(model):
                acc.claude_health_state = GoogleAccountHealth.TRANSIENT_ERROR
                acc.claude_last_error = error_text[:500]
                acc.claude_cooldown_until = dt.isoformat()
            else:
                acc.health_state = GoogleAccountHealth.TRANSIENT_ERROR
                acc.last_error = error_text[:500]
                acc.cooldown_until = dt.isoformat()
            acc.updated_at = now()
            self.save()

    def mark_auth_verification_required(
        self,
        account_id: str,
        error_text: str
    ):
        """Flag account as requiring interactive browser or eligibility verification.
        Must NOT be treated as quota-blocked, and must not be scheduled until verified.
        """
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            acc.health_state = GoogleAccountHealth.AUTH_VERIFICATION_REQUIRED
            acc.last_error = error_text[:500]
            acc.cooldown_until = None
            acc.updated_at = now()
            self.save()

    def mark_success(self, account_id: str, turn_tokens: int = 0, input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0, record_turn: bool = True, model: Optional[str] = None):
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            acc.last_run_error = None
            is_claude = is_claude_model(model)
            if not record_turn:
                if not acc.is_cooldown_active(None):
                    acc.health_state = GoogleAccountHealth.HEALTHY
                    acc.last_error = None
                    acc.cooldown_until = None
                if not acc.is_cooldown_active('claude'):
                    acc.claude_health_state = GoogleAccountHealth.HEALTHY
                    acc.claude_last_error = None
                    acc.claude_cooldown_until = None
                acc.updated_at = now()
                self.save()
                return
            out_val = output_tokens or turn_tokens or 0
            if is_claude:
                acc.claude_health_state = GoogleAccountHealth.HEALTHY
                acc.claude_last_successful_use = now()
                acc.claude_last_error = None
                acc.claude_cooldown_until = None
                if input_tokens:
                    acc.claude_input_tokens += input_tokens
                if out_val:
                    acc.claude_output_tokens += out_val
                if cache_read_tokens:
                    acc.claude_cache_read_tokens += cache_read_tokens
                target_attrs = [('claude_usage_5h', 18000), ('claude_usage_weekly', 604800)]
            else:
                acc.health_state = GoogleAccountHealth.HEALTHY
                acc.last_successful_use = now()
                acc.last_error = None
                acc.cooldown_until = None
                if input_tokens:
                    acc.input_tokens += input_tokens
                if out_val:
                    acc.output_tokens += out_val
                if cache_read_tokens:
                    acc.cache_read_tokens += cache_read_tokens
                target_attrs = [('usage_5h', 18000), ('usage_weekly', 604800)]
            for attr, seconds in target_attrs:
                usage = getattr(acc, attr) or {}
                cutoff = datetime.now(timezone.utc).timestamp() - seconds
                samples = []
                for s in usage.get('observed_turns', []):
                    ts = s.get('t', 0) if isinstance(s, dict) else s
                    if ts > cutoff:
                        samples.append(s)
                samples.append({
                    't': datetime.now(timezone.utc).timestamp(),
                    'in': input_tokens,
                    'out': out_val,
                    'cache': cache_read_tokens,
                    'model': model or ('antigravity/claude-opus-4-6-thinking' if is_claude else 'antigravity/gemini-3.8-flash-high'),
                })
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
            text_lower = error_text.lower()
            if any(term in text_lower for term in ('eligibility check failed', 'not eligible for antigravity', 'verify your account')):
                acc.last_error = error_text[:500]
                acc.health_state = GoogleAccountHealth.AUTH_VERIFICATION_REQUIRED
                acc.cooldown_until = None
            else:
                authentication_error = any(term in text_lower for term in
                    ('authentication revoked', 'invalid_grant', 'unauthorized', 'invalid credentials', '401 unauthorized'))
                if (is_fatal or authentication_error) and not self.is_launch_error(error_text):
                    acc.last_error = error_text[:500]
                    acc.health_state = GoogleAccountHealth.ERROR
                else:
                    acc.last_run_error = error_text[:500]
            acc.updated_at = now()
            self.save()

    def earliest_reset(self, model: Optional[str] = None) -> Optional[str]:
        resets = []
        is_claude = is_claude_model(model)
        for acc in self.accounts.values():
            cd = acc.claude_cooldown_until if is_claude else acc.cooldown_until
            if acc.is_cooldown_active(model) and cd:
                resets.append(cd)
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

    @asynccontextmanager
    async def launch_lease(self, account_id: str):
        """Acquires credential lock ONLY during account credential activation and process launch."""
        while True:
            await self.login_done.wait()
            await self.credential_lock.acquire()
            if not self.login_pending: break
            self.credential_lock.release()
        try:
            if not self.activate_account_credential(account_id):
                raise RuntimeError('Could not activate saved Google credential; capture its login again')
            yield
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

    def save_account_credential(self, account_id: str, allow_replacement: bool = True) -> bool:
        """Captures the current active Windows credential blob into the account's folder.
        If allow_replacement is True, signing in with a different Google account replaces
        the account's email, clears any active quota-block/cooldown, and resets token counters.
        """
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
            if not email:
                raise ValueError('Could not extract email identity from Google credential')

            placeholder = acc.email.startswith('Account ') or acc.email == 'default@google.com'
            is_different = email.casefold() != acc.email.casefold()

            if is_different and not placeholder:
                if not allow_replacement:
                    raise ValueError('Signed-in identity does not match this Google account profile')
                # Check for collision with another registered account
                for other_id, other_acc in self.accounts.items():
                    if other_id != account_id and other_acc.email.casefold() == email.casefold():
                        raise ValueError(f"Google account '{email}' is already registered as '{other_id}'. Use that account or remove it first.")

            save_credential(auth_dir / 'credential.dat', blob)

            # If replacing an existing account or bringing in a fresh account, restore healthy state:
            if is_different or acc.health_state in (GoogleAccountHealth.QUOTA_BLOCKED, GoogleAccountHealth.RATE_LIMITED, GoogleAccountHealth.ERROR):
                acc.health_state = GoogleAccountHealth.HEALTHY
                acc.cooldown_until = None
                acc.last_error = None
                acc.last_run_error = None
                # Reset local usage observations for the new identity
                if is_different:
                    acc.usage_5h = None
                    acc.usage_weekly = None
                    acc.input_tokens = 0
                    acc.output_tokens = 0
                    acc.cache_read_tokens = 0
            else:
                acc.health_state = GoogleAccountHealth.UNKNOWN

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
            identity = self._extract_email(blob)
            if not identity or identity.casefold() != acc.email.casefold():
                raise RuntimeError('Saved Google credential identity does not match its account profile')
            if not saved.startswith(PREFIX): save_credential(cred_file, blob)
            if not WindowsKeyringHelper.write_credential(blob):
                return False
            active = WindowsKeyringHelper.read_credential()
            if not active or active[1] != blob:
                raise RuntimeError('Google credential activation could not be verified')
            return True
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

