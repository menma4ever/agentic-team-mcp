import os
import shutil
from pathlib import Path
from typing import Dict, Optional
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get('AGENTIC_TEAM_DATA', ROOT)).resolve()
SETTINGS_FILE = DATA_DIR / 'settings.json'


class Provider(BaseModel):
    adapter: str = 'openai'
    base_url: Optional[str] = None
    models: list[str] = Field(default_factory=list)


class SystemSettings(BaseModel):
    api_keys: Dict[str, str] = Field(default_factory=dict)
    providers: Dict[str, Provider] = Field(default_factory=dict)
    cli_paths: Dict[str, str] = Field(default_factory=lambda: {
        'claude': 'claude', 'codex': 'codex', 'agy': 'agy', 'gemini': 'gemini',
        'hermes': 'hermes', 'openclaw': 'openclaw'})
    cli_auth_enabled: Dict[str, bool] = Field(default_factory=dict)
    default_ceo_model: str = 'antigravity/claude-opus-4-6-thinking'
    default_manager_model: str = 'antigravity/gemini-3.8-flash-high'
    inactivity_timeout_seconds: int = Field(default=1800, ge=10)
    heartbeat_interval_seconds: int = Field(default=15, ge=1)
    max_tool_rounds: int = Field(default=40, ge=1, le=200)
    max_autonomous_turns: int = Field(default=100, ge=1, le=1000)
    max_workers: int = Field(default=12, ge=1, le=100)
    request_timeout_seconds: int = Field(default=600, ge=1)
    max_output_tokens: int = Field(default=8192, ge=256)

    @classmethod
    def load(cls):
        if SETTINGS_FILE.exists():
            return cls.model_validate_json(SETTINGS_FILE.read_text(encoding='utf-8-sig'))
        return cls()

    def save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = SETTINGS_FILE.with_suffix('.tmp')
        temp.write_text(self.model_dump_json(indent=2), encoding='utf-8')
        temp.replace(SETTINGS_FILE)

    def public(self):
        result = self.model_dump()
        result['api_keys'] = {k: bool(v) for k, v in self.api_keys.items()}
        return result

    def get_api_key(self, provider):
        envs = {
            'gemini': ['GEMINI_API_KEY', 'GOOGLE_API_KEY'],
            'qwen': ['DASHSCOPE_API_KEY', 'QWEN_API_KEY'],
            'moonshot': ['MOONSHOT_API_KEY', 'KIMI_API_KEY'],
            'kimi': ['KIMI_API_KEY', 'MOONSHOT_API_KEY'],
            'minimax': ['MINIMAX_API_KEY'],
            'siliconflow': ['SILICONFLOW_API_KEY', 'SILICON_API_KEY'],
            'together': ['TOGETHER_API_KEY', 'TOGETHERAI_API_KEY'],
            'zai': ['ZAI_API_KEY', 'GLM_API_KEY'],
            'experiential': ['EXPERIENTIALLABS_API_KEY', 'XPL_API_KEY', 'EXPERIENTIAL_API_KEY'],
            'xpl': ['XPL_API_KEY', 'EXPERIENTIALLABS_API_KEY', 'EXPERIENTIAL_API_KEY'],
        }
        return self.api_keys.get(provider) or next((os.environ[k] for k in
            envs.get(provider, [provider.upper() + '_API_KEY']) if os.environ.get(k)), None)

    def cli_executable(self, name):
        configured = self.cli_paths.get(name, name)
        # Explicit executable paths must not depend on the launcher's PATH/PATHEXT.
        explicit = Path(configured)
        if explicit.is_absolute() and explicit.is_file() and os.access(explicit, os.X_OK):
            return str(explicit)
        if name == 'codex' and os.name == 'nt':
            local_app_data = Path(os.environ.get('LOCALAPPDATA', ''))
            candidates = sorted(list((local_app_data / 'OpenAI' / 'Codex' / 'bin').glob('*/codex.exe')), key=lambda x: x.stat().st_mtime, reverse=True)
            if candidates:
                return str(candidates[0])
        path = shutil.which(configured)
        if not path and name == 'agy' and os.name == 'nt':
            candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'agy' / 'bin' / 'agy.exe'
            if candidate.is_file():
                path = str(candidate)
        return path

    def check_cli_available(self, name):
        path = self.cli_executable(name)
        return {'available': path is not None, 'path': path,
                'auth_enabled': self.cli_auth_enabled.get(name, False)}

    def redact(self, value):
        text = str(value)
        keys_to_redact = set(v for v in self.api_keys.values() if v)
        for p in ('openai', 'anthropic', 'gemini', 'openrouter', 'groq', 'deepseek',
                  'qwen', 'minimax', 'moonshot', 'siliconflow', 'together', 'mistral', 'zai',
                  'experiential', 'xpl'):
            k = self.get_api_key(p)
            if k:
                keys_to_redact.add(k)
        for key in keys_to_redact:
            if key and len(key) >= 4:
                text = text.replace(key, '[REDACTED]')
        return text


settings = SystemSettings.load()

