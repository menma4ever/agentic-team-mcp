import copy
from core.providers import PROVIDER_PRESETS, sync_all_live_provider_models
from engine.models import HarnessType

class ProviderCatalog:
    def __init__(self, config=None):
        self.config = config
        self.providers = copy.deepcopy(PROVIDER_PRESETS)
        if config:
            self.load_from_config(config)

    def load_from_config(self, config):
        if 'providers' in config:
            for k, v in config['providers'].items():
                if k in self.providers:
                    self.providers[k].update(v)
                else:
                    self.providers[k] = v
        sync_all_live_provider_models(config)
        if 'providers' in config:
            for k, v in config['providers'].items():
                if k in self.providers:
                    self.providers[k].update(v)
                else:
                    self.providers[k] = v

    def list_all(self):
        if self.config:
            sync_all_live_provider_models(self.config)
            if 'providers' in self.config:
                for k, v in self.config['providers'].items():
                    if k in self.providers:
                        self.providers[k].update(v)
                    else:
                        self.providers[k] = v
        return self.providers

    def get(self, key):
        return self.providers.get(key)


def model_catalog(engine):
    """Returns the unified model catalog for the workspace model/harness picker."""
    config = getattr(engine, 'config', None) or getattr(getattr(engine, 'runner', None), 'config', None)
    providers_list = []

    # 1. Google account (Antigravity CLI)
    agy_models = [
        {'id': 'antigravity/gemini-3.8-flash-high', 'recommended_harness': 'antigravity', 'source': 'Primary Google Workhorse'},
        {'id': 'antigravity/claude-opus-4-6-thinking', 'recommended_harness': 'antigravity', 'source': 'Free Extra Worker (Claude Opus via Google Pool)'},
        {'id': 'antigravity/claude-sonnet-4-6', 'recommended_harness': 'antigravity', 'source': 'Google Antigravity'},
        {'id': 'antigravity/gemini-3.8-flash-low', 'recommended_harness': 'antigravity', 'source': 'Google Antigravity'},
        {'id': 'antigravity/gemini-3.8-flash', 'recommended_harness': 'antigravity', 'source': 'Google Antigravity'},
        {'id': 'antigravity/gemini-2.5-pro', 'recommended_harness': 'antigravity', 'source': 'Google Antigravity'},
    ]
    providers_list.append({
        'id': 'agy',
        'label': 'Google account (Antigravity)',
        'setup_required': False,
        'models': agy_models
    })

    # 2. OpenAI / Codex CLI
    codex_models = [
        {'id': 'openai/gpt-6.1-sol', 'recommended_harness': 'codex', 'source': 'Codex CLI / OpenAI'},
        {'id': 'openai/gpt-6-sol', 'recommended_harness': 'codex', 'source': 'Codex CLI / OpenAI'},
        {'id': 'openai/gpt-5.6-sol', 'recommended_harness': 'codex', 'source': 'Codex CLI / OpenAI'},
        {'id': 'openai/gpt-4o', 'recommended_harness': 'codex', 'source': 'Codex CLI / OpenAI'},
        {'id': 'openai/o3-mini', 'recommended_harness': 'codex', 'source': 'Codex CLI / OpenAI'},
    ]
    providers_list.append({
        'id': 'codex',
        'label': 'OpenAI account (Codex)',
        'setup_required': False,
        'models': codex_models
    })

    # 3. Claude Code
    claude_models = [
        {'id': 'claude-3-7-sonnet-20250219', 'recommended_harness': 'claude_code', 'source': 'Anthropic Claude Code'},
        {'id': 'claude-3-5-haiku-20241022', 'recommended_harness': 'claude_code', 'source': 'Anthropic Claude Code'},
    ]
    providers_list.append({
        'id': 'claude',
        'label': 'Claude Code',
        'setup_required': False,
        'models': claude_models
    })

    # 4. Z.ai (GLM)
    zai_models = [
        {'id': 'zai/glm-5.3', 'recommended_harness': 'claude_code', 'source': 'Z.ai (GLM)'},
        {'id': 'zai/glm-5.3-flash', 'recommended_harness': 'claude_code', 'source': 'Z.ai (GLM)'},
        {'id': 'zai/glm-5', 'recommended_harness': 'claude_code', 'source': 'Z.ai (GLM)'},
        {'id': 'zai/glm-4.7', 'recommended_harness': 'claude_code', 'source': 'Z.ai (GLM)'},
    ]
    providers_list.append({
        'id': 'zai',
        'label': 'Z.ai (GLM)',
        'setup_required': not (config and config.get_api_key('zai')),
        'models': zai_models
    })

    # 5. Experiential Labs
    exp_models = []
    if config and hasattr(config, 'providers') and 'experiential' in config.providers:
        for m in config.providers['experiential'].models:
            exp_models.append({'id': f'experiential/{m}', 'recommended_harness': 'codex', 'source': 'Experiential Labs'})
    if not exp_models:
        exp_models = [
            {'id': 'experiential/claude-opus-latest', 'recommended_harness': 'codex', 'source': 'Experiential Labs'},
            {'id': 'experiential/claude-sonnet-latest', 'recommended_harness': 'codex', 'source': 'Experiential Labs'},
        ]
    providers_list.append({
        'id': 'experiential',
        'label': 'Experiential Labs',
        'setup_required': not (config and (config.get_api_key('experiential') or config.get_api_key('xpl'))),
        'models': exp_models
    })

    # 6. Partner APIs (DeepSeek, OpenRouter, SiliconFlow, Together)
    for p_id, p_label in [('deepseek', 'DeepSeek API'), ('openrouter', 'DeepSeek via OpenRouter'),
                          ('siliconflow', 'DeepSeek via SiliconFlow'), ('together', 'DeepSeek via Together')]:
        p_models = []
        if config and hasattr(config, 'providers') and p_id in config.providers:
            for m in config.providers[p_id].models:
                p_models.append({'id': f'{p_id}/{m}', 'recommended_harness': 'direct_api', 'source': p_label})
        elif p_id in PROVIDER_PRESETS:
            for m in PROVIDER_PRESETS[p_id].get('models', []):
                p_models.append({'id': f'{p_id}/{m}', 'recommended_harness': 'direct_api', 'source': p_label})
        has_key = bool(config and config.get_api_key(p_id))
        providers_list.append({
            'id': p_id,
            'label': p_label,
            'setup_required': not has_key,
            'models': p_models
        })

    # 7. Add active workspace agents' models if not present
    if hasattr(engine, 'agents'):
        for a in engine.agents.values():
            if not getattr(a, 'model', None):
                continue
            pid = 'agy' if a.harness == HarnessType.ANTIGRAVITY else (
                'codex' if a.harness == HarnessType.CODEX and not a.model.startswith('experiential/') else (
                    'claude' if a.harness == HarnessType.CLAUDE_CODE and not a.model.startswith('zai/') else a.model.split('/')[0]
                )
            )
            prov = next((p for p in providers_list if p['id'] == pid), None)
            if prov and not any(m['id'] == a.model for m in prov['models']):
                prov['models'].insert(0, {'id': a.model, 'recommended_harness': a.harness.value, 'source': 'Active Agent in Workspace'})

    return {'providers': providers_list}
