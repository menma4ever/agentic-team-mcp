"""Use configured and observed models rather than a versioned marketing palette."""
import json
from pathlib import Path

def model_catalog(engine):
    providers = {}
    def add(provider, label, model, harness, source):
        p = providers.setdefault(provider, {'id':provider,'label':label,'models':[]})
        if model and not any(m['id']==model for m in p['models']):
            p['models'].append({'id':model,'recommended_harness':harness,'source':source})
    for alias, spec in engine.config.providers.items():
        models = spec.get('models',[]) if isinstance(spec,dict) else spec.models
        for model in models:
            add(alias, 'DeepSeek API' if alias=='deepseek' else alias, alias+'/'+model, 'claude_code' if alias=='zai' else 'direct_api', 'Configured model')
    for name,label,h in [('agy','Google account','antigravity'),('codex','OpenAI account','codex'),('claude','Claude account','claude_code')]:
        if engine.config.cli_auth_enabled.get(name): add(name,label,None,h,'Account enabled')
    for a in engine.agents.values():
        provider = {'antigravity':'agy','codex':'codex','claude_code':'claude'}.get(a.harness.value, a.model.split('/')[0])
        if a.model.startswith('zai/'): provider='zai'
        add(provider, {'agy':'Google account','codex':'OpenAI account','claude':'Claude account'}.get(provider,provider),
            a.model,a.harness.value,'Used in this workspace; availability not verified')
    # List partner connections even before setup; never present them as signed in.
    for alias, label in [('deepseek','DeepSeek API'),('openrouter','DeepSeek via OpenRouter'),
                         ('siliconflow','DeepSeek via SiliconFlow'),('together','DeepSeek via Together')]:
        add(alias,label,None,'direct_api','Connection option')
        providers[alias]['label'] = label
        providers[alias]['setup_required'] = not (alias in engine.config.providers and engine.config.get_api_key(alias))
    cache = Path.home()/'.codex/models_cache.json'
    try:
        data=json.loads(cache.read_text(encoding='utf-8'))
        for m in data.get('models',[]):
            slug=m.get('slug') or m.get('id')
            if slug: add('codex','OpenAI account',slug,'codex','Local Codex model catalog')
    except (OSError,ValueError,TypeError): pass
    return {'providers':list(providers.values()),'note':'Saved and locally discovered models. Enter an exact model ID if needed; availability is checked on execution.'}
