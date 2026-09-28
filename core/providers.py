"""Connection presets contain public configuration only, never credentials."""

PROVIDER_PRESETS = {
    'deepseek': {
        'label': 'DeepSeek (Official)',
        'alias': 'deepseek',
        'adapter': 'openai',
        'base_url': 'https://api.deepseek.com',
        'models': ['deepseek-flash', 'deepseek-chat', 'deepseek-reasoner'],
        'note': 'Direct DeepSeek API. Supports DeepSeek-V4.1-Flash (deepseek-flash), DeepSeek-V3 (chat) and DeepSeek-R1 (reasoner).',
        'key_url': 'https://platform.deepseek.com/api_keys',
    },
    'qwen': {
        'label': 'Alibaba Qwen (DashScope)',
        'alias': 'qwen',
        'adapter': 'openai',
        'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'models': [
            'qwen-max',
            'qwen-plus',
            'qwen-turbo',
            'qwen2.5-72b-instruct',
            'qwen2.5-coder-32b-instruct',
            'qwen2.5-coder-7b-instruct',
            'qvq-72b-preview',
        ],
        'note': 'Alibaba DashScope OpenAI-compatible endpoint. Access to Qwen 2.5, Qwen-Max, and Qwen-Coder models.',
        'key_url': 'https://dashscope.console.aliyun.com/apiKey',
    },
    'minimax': {
        'label': 'MiniMax API',
        'alias': 'minimax',
        'adapter': 'openai',
        'base_url': 'https://api.minimax.chat/v1',
        'models': [
            'MiniMax-Text-01',
            'abab6.5s-chat',
            'abab6.5t-chat',
            'abab5.5-chat',
        ],
        'note': 'MiniMax large-context foundation models, including MiniMax-Text-01 with up to 4M context.',
        'key_url': 'https://platform.minimaxi.com/user-center/basic-information/interface-key',
    },
    'moonshot': {
        'label': 'Moonshot AI (Kimi)',
        'alias': 'moonshot',
        'adapter': 'openai',
        'base_url': 'https://api.moonshot.cn/v1',
        'models': [
            'moonshot-v1-8k',
            'moonshot-v1-32k',
            'moonshot-v1-128k',
            'moonshot-v1-auto',
            'kimi-latest',
        ],
        'note': 'Moonshot AI / Kimi long-context conversational models with tool-calling capabilities.',
        'key_url': 'https://platform.moonshot.cn/console/api-keys',
    },
    'siliconflow': {
        'label': 'SiliconFlow (SiliconCloud)',
        'alias': 'siliconflow',
        'adapter': 'openai',
        'base_url': 'https://api.siliconflow.cn/v1',
        'models': [
            'deepseek-ai/DeepSeek-V3',
            'deepseek-ai/DeepSeek-R1',
            'Qwen/Qwen2.5-72B-Instruct',
            'Qwen/Qwen2.5-Coder-32B-Instruct',
            'THUDM/glm-4-9b-chat',
        ],
        'note': 'Fast inference for open-weights models (DeepSeek-V3, DeepSeek-R1, Qwen2.5, GLM-4).',
        'key_url': 'https://cloud.siliconflow.cn/account/ak',
    },
    'openrouter': {
        'label': 'OpenRouter (200+ Models Aggregator)',
        'alias': 'openrouter',
        'adapter': 'openrouter',
        'base_url': 'https://openrouter.ai/api/v1',
        'models': [
            'deepseek/deepseek-r1',
            'deepseek/deepseek-chat',
            'qwen/qwen-2.5-72b-instruct',
            'qwen/qwen-2.5-coder-32b-instruct',
            'minimax/minimax-01',
            'moonshotai/moonshot-v1-32k',
            'anthropic/claude-3.7-sonnet',
            'openai/gpt-4o',
            'meta-llama/llama-3.3-70b-instruct',
        ],
        'note': 'Universal API routing to DeepSeek, Qwen, MiniMax, Kimi, Claude, GPT, and 200+ models with a single balance.',
        'key_url': 'https://openrouter.ai/keys',
    },
    'zai': {
        'label': 'GLM / Z.ai API',
        'alias': 'zai',
        'adapter': 'openai',
        'base_url': 'https://api.z.ai/api/coding/paas/v4',
        'models': ['glm-5.3', 'glm-4.7', 'GLM-5.3-Flash'],
        'note': 'Uses your Z.ai Coding Plan API key and quota. Supports GLM-4 and GLM-5 series.',
        'key_url': 'https://z.ai/manage-apikey/apikey-list',
    },
    'experiential': {
        'label': 'Experiential Labs',
        'alias': 'experiential',
        'adapter': 'openai',
        'base_url': 'https://api.experientiallabs.ai/v1',
        'models': ['gpt-6-sol', 'claude-opus-5.5', 'gpt-5.6-sol', 'gemini-3.8-flash', 'deepseek-v4-pro'],
        'note': 'Experiential Labs unified frontier model endpoint.',
        'key_url': 'https://api.experientiallabs.ai',
    },
    'gemini': {
        'label': 'Google Gemini API key',
        'alias': 'gemini',
        'adapter': 'gemini',
        'base_url': '',
        'models': [
            'gemini-2.5-pro',
            'gemini-2.5-flash',
            'gemini-2.0-flash-thinking-exp-01-21',
        ],
        'note': 'Uses a Gemini API key. For your Google AI subscription, use Gemini — Google account above.',
        'key_url': 'https://aistudio.google.com/apikey',
    },
    'anthropic': {
        'label': 'Anthropic (Claude API)',
        'alias': 'anthropic',
        'adapter': 'anthropic',
        'base_url': 'https://api.anthropic.com/v1',
        'models': [
            'claude-opus-4-6',
            'claude-sonnet-4-6',
            'claude-3-7-sonnet-20250219',
            'claude-3-5-haiku-20241022',
        ],
        'note': 'Official Anthropic Claude API key with support for extended thinking.',
        'key_url': 'https://console.anthropic.com/settings/keys',
    },
    'openai': {
        'label': 'OpenAI API',
        'alias': 'openai',
        'adapter': 'openai',
        'base_url': 'https://api.openai.com/v1',
        'models': [
            'gpt-6-sol',
            'gpt-4o',
            'gpt-4o-mini',
            'o1',
            'o3-mini',
        ],
        'note': 'Official OpenAI API key. Supports reasoning models (o1, o3-mini) and multimodal models.',
        'key_url': 'https://platform.openai.com/api-keys',
    },
    'groq': {
        'label': 'Groq (Ultra-Fast LPU)',
        'alias': 'groq',
        'adapter': 'groq',
        'base_url': 'https://api.groq.com/openai/v1',
        'models': [
            'deepseek-r1-distill-llama-70b',
            'llama-3.3-70b-versatile',
            'qwen-2.5-32b',
        ],
        'note': 'Ultra-fast inference powered by Groq LPU hardware.',
        'key_url': 'https://console.groq.com/keys',
    },
    'together': {
        'label': 'Together AI',
        'alias': 'together',
        'adapter': 'openai',
        'base_url': 'https://api.together.xyz/v1',
        'models': [
            'deepseek-ai/DeepSeek-R1',
            'deepseek-ai/DeepSeek-V3',
            'Qwen/Qwen2.5-72B-Instruct-Turbo',
            'Qwen/Qwen2.5-Coder-32B-Instruct',
        ],
        'note': 'Fast cloud inference platform for open models.',
        'key_url': 'https://api.together.ai/settings/api-keys',
    },
    'mistral': {
        'label': 'Mistral AI',
        'alias': 'mistral',
        'adapter': 'openai',
        'base_url': 'https://api.mistral.ai/v1',
        'models': [
            'mistral-large-latest',
            'codestral-latest',
            'mistral-small-latest',
        ],
        'note': 'Official Mistral API for Mistral Large and Codestral.',
        'key_url': 'https://console.mistral.ai/api-keys',
    },
    'ollama': {
        'label': 'Ollama (Local Offline)',
        'alias': 'ollama',
        'adapter': 'ollama_chat',
        'base_url': 'http://localhost:11434',
        'models': [
            'deepseek-r1:14b',
            'deepseek-r1:8b',
            'qwen2.5:14b',
            'qwen2.5-coder:14b',
            'qwen2.5-coder:32b',
            'llama3.3:70b',
        ],
        'note': 'Local models running via Ollama on your machine. Zero cost, no external API keys required.',
        'key_url': 'https://ollama.com',
    },
}

import json
import os
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_LIVE_MODELS_CACHE: dict[str, tuple[float, list[str]]] = {}
_CACHE_TTL_SECONDS = 300.0


def fetch_live_provider_models(alias: str, config=None, force: bool = False) -> list[str]:
    """Queries the live provider /models endpoint or CLI to retrieve real available models."""
    from core.config import settings as default_settings
    cfg = config or default_settings
    now_ts = time.time()

    if not force and alias in _LIVE_MODELS_CACHE:
        cached_ts, cached_models = _LIVE_MODELS_CACHE[alias]
        if now_ts - cached_ts < _CACHE_TTL_SECONDS and cached_models:
            return list(cached_models)

    # 1. Google Antigravity CLI (agy models)
    if alias in ('agy', 'google', 'antigravity'):
        agy_path = cfg.cli_executable('agy') or os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.exe")
        if agy_path and os.path.isfile(agy_path):
            try:
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                r = subprocess.run([agy_path, 'models'], capture_output=True, text=True, timeout=6, creationflags=flags)
                if r.returncode == 0 and r.stdout:
                    models = []
                    for line in r.stdout.splitlines():
                        parts = line.strip().split()
                        if parts and not parts[0].startswith('['):
                            slug = parts[0].strip()
                            if slug and slug not in models:
                                models.append(slug)
                    if models:
                        _LIVE_MODELS_CACHE[alias] = (now_ts, models)
                        return models
            except Exception:
                pass
        fallback_agy = ['gemini-3.8-flash-high', 'claude-opus-4-6-thinking', 'claude-sonnet-4-6', 'gemini-3.1-pro-high']
        return fallback_agy

    # 2. Codex CLI cache (used for both 'codex' and 'openai' when Codex CLI login is present)
    codex_models = []
    if alias in ('codex', 'openai'):
        cache = Path.home() / '.codex/models_cache.json'
        if cache.is_file():
            try:
                data = json.loads(cache.read_text(encoding='utf-8'))
                for m in data.get('models', []):
                    slug = m.get('slug') or m.get('id')
                    if slug and slug not in codex_models:
                        codex_models.append(str(slug))
            except Exception:
                pass
        if alias == 'codex' and codex_models:
            _LIVE_MODELS_CACHE[alias] = (now_ts, codex_models)
            return codex_models

    # 3. HTTP API Provider (/models or /v1/models)
    spec = cfg.providers.get(alias)
    preset = PROVIDER_PRESETS.get(alias, {})
    base_url = ''
    if spec:
        base_url = (spec.get('base_url') if isinstance(spec, dict) else spec.base_url) or ''
    if not base_url:
        base_url = preset.get('base_url', '')
    base_url = base_url.rstrip('/')

    api_key = cfg.get_api_key(alias)
    if not api_key and alias == 'experiential':
        api_key = cfg.get_api_key('xpl')
    elif not api_key and alias == 'xpl':
        api_key = cfg.get_api_key('experiential')

    if alias == 'gemini' and api_key and not base_url:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            req = urllib.request.Request(url, headers={"User-Agent": "AgenticTeam/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                models = [
                    m['name'].split('/')[-1]
                    for m in data.get('models', [])
                    if isinstance(m, dict) and m.get('name')
                ]
                if models:
                    _LIVE_MODELS_CACHE[alias] = (now_ts, models)
                    return models
        except Exception:
            pass

    if api_key and base_url:
        candidate_urls = [base_url + '/models']
        if not base_url.endswith('/v1') and not base_url.endswith('/v4'):
            candidate_urls.append(base_url + '/v1/models')
        for url in candidate_urls:
            try:
                headers = {
                    'Authorization': f'Bearer {api_key}',
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01',
                    'User-Agent': 'AgenticTeam/1.0',
                }
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    items = data.get('data') or data.get('models') or []
                    models = list(codex_models)
                    for item in items:
                        if isinstance(item, dict):
                            raw_name = item.get('name')
                            raw_id = item.get('id') or item.get('slug')
                            if raw_name and isinstance(raw_name, str) and not raw_name.startswith('models/'):
                                norm_name = raw_name.strip().lower().replace(' ', '-')
                                if norm_name and norm_name not in models:
                                    models.append(norm_name)
                            if raw_id and str(raw_id) not in models:
                                models.append(str(raw_id))
                        elif isinstance(item, str) and item not in models:
                            models.append(item)
                    if models:
                        _LIVE_MODELS_CACHE[alias] = (now_ts, models)
                        if alias in cfg.providers and not isinstance(cfg.providers[alias], dict):
                            cfg.providers[alias].models = models
                        return models
            except Exception:
                continue

    if codex_models:
        _LIVE_MODELS_CACHE[alias] = (now_ts, codex_models)
        if alias in cfg.providers and not isinstance(cfg.providers[alias], dict):
            cfg.providers[alias].models = codex_models
        return codex_models

    if spec:
        existing = spec.get('models', []) if isinstance(spec, dict) else spec.models
        if existing:
            return list(existing)
    return list(preset.get('models', []))


def sync_all_live_provider_models(config=None, force: bool = False) -> dict[str, list[str]]:
    """Concurrently queries live /models endpoints for all configured providers and updates config."""
    from core.config import settings as default_settings
    cfg = config or default_settings
    aliases = set(cfg.providers.keys()) | set(k for k, v in cfg.api_keys.items() if v) | {'agy', 'codex'}
    results: dict[str, list[str]] = {}

    def _worker(a: str):
        return a, fetch_live_provider_models(a, cfg, force=force)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for a, models in pool.map(_worker, aliases):
            if models:
                results[a] = models
                if a in cfg.providers and not isinstance(cfg.providers[a], dict):
                    cfg.providers[a].models = models
    return results

