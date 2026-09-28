import asyncio
import json
from core.config import settings


class DirectAPIRunner:
    def __init__(self, config=settings, completion=None):
        self.config = config
        self.completion = completion

    async def generate_response(self, agent, conversation_history, tools, execute, emit):
        config = self.config
        if '/' not in agent.model:
            alias = getattr(agent, 'provider', None)
            if alias:
                alias = alias.lower()
            if not alias or alias not in config.providers:
                for p_alias, p_profile in config.providers.items():
                    if agent.model in p_profile.models or agent.model.lower() in [m.lower() for m in p_profile.models]:
                        alias = p_alias
                        break
            if not alias and agent.model in ('GPT-6 Sol', 'gpt-6-sol'):
                alias = 'openai'
            if not alias:
                raise ValueError('Use provider/model, for example a configured alias/model')
            model_id = agent.model
        else:
            alias, model_id = agent.model.split('/', 1)
        profile = config.providers.get(alias)
        provider = profile.adapter if profile else alias
        if (alias == 'deepseek' or provider == 'deepseek') and model_id.lower() in ('deepseek-v4.1-flash', 'deepseek-4.1-flash'):
            model_id = 'deepseek-flash'
        model = provider + '/' + model_id
        key = config.get_api_key(alias)
        local = provider in ('ollama', 'ollama_chat')
        if not key and not local and self.completion is None:
            raise RuntimeError(f'No API key configured for {alias}')
        kwargs = {'model': model, 'messages': conversation_history, 'tools': tools,
                  'max_tokens': config.max_output_tokens, 'timeout': config.request_timeout_seconds,
                  'num_retries': 0}
        if key:
            kwargs['api_key'] = key
        if profile and profile.base_url:
            kwargs['api_base'] = profile.base_url
        if agent.reasoning_effort:
            if agent.reasoning_effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
                raise ValueError('Supported reasoning effort levels are low, medium, high, xhigh, max')
            kwargs['reasoning_effort'] = agent.reasoning_effort
            kwargs['allowed_openai_params'] = ['reasoning_effort']
        elif agent.thinking_budget:
            if provider in ('anthropic', 'gemini'):
                kwargs['thinking'] = {'type':'enabled','budget_tokens':agent.thinking_budget}
                kwargs['max_tokens'] = max(kwargs['max_tokens'], agent.thinking_budget + 2048)
            elif provider in ('deepseek', 'openai', 'openrouter', 'qwen', 'minimax', 'moonshot') or (profile and profile.adapter == 'openai'):
                pass
            else:
                raise ValueError('Use reasoning_effort for this provider; numeric thinking budget is unsupported')
        completion = self.completion
        if completion is None:
            try:
                import litellm
            except ImportError as exc:
                raise RuntimeError('Install requirements.txt in the application environment') from exc
            litellm.suppress_debug_info = True
            litellm.drop_params = True
            try:
                import aiohttp
                import litellm.llms.custom_httpx.aiohttp_transport as aio_trans
                if not getattr(aio_trans, '_threaded_patched', False):
                    orig_init = aio_trans.LiteLLMAiohttpTransport.__init__
                    def new_init(self, client, ssl_verify=None, owns_session=True, session_factory=None):
                        def threaded_factory():
                            return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()))
                        orig_init(self, client=threaded_factory, ssl_verify=ssl_verify, owns_session=owns_session, session_factory=threaded_factory)
                    aio_trans.LiteLLMAiohttpTransport.__init__ = new_init
                    aio_trans._threaded_patched = True
            except Exception:
                pass
            completion = litellm.acompletion
        repeats = {}
        for step in range(config.max_tool_rounds):
            async with asyncio.timeout(config.request_timeout_seconds):
                response = await completion(**kwargs)
            message = response.choices[0].message
            raw = message.model_dump(exclude_none=True) if hasattr(message, 'model_dump') else dict(message)
            saved = {'role':'assistant', 'content':raw.get('content')}
            # GLM thinking models require the preceding reasoning on tool turns.
            reasoning = raw.get('reasoning_content')
            if reasoning is None:
                reasoning = (raw.get('provider_specific_fields') or {}).get('reasoning_content')
            if reasoning is not None:
                saved['reasoning_content'] = reasoning
            calls = raw.get('tool_calls') or []
            if calls:
                saved['tool_calls'] = calls
            conversation_history.append(saved)
            await emit('model_response', {'text':saved['content'], 'tool_count':len(calls),
                                         'usage':getattr(response, 'usage', None).model_dump()
                                         if hasattr(getattr(response,'usage',None),'model_dump') else None})
            if not calls:
                if not saved['content']:
                    raise RuntimeError('Provider returned neither text nor tool calls')
                return saved['content']
            terminal = None
            for call in calls:
                f = call['function']
                signature = f['name'] + f['arguments']
                repeats[signature] = repeats.get(signature, 0) + 1
                if terminal is not None:
                    value = {'error':'Turn ended by an earlier tool; action was not executed'}
                elif repeats[signature] > 3:
                    raise RuntimeError('Repeated identical tool call; requesting supervisor guidance')
                else:
                    try:
                        args = json.loads(f['arguments'])
                        value = await execute(f['name'], args)
                    except (ValueError, PermissionError, OSError) as exc:
                        value = {'error':config.redact(exc)}
                conversation_history.append({'role':'tool','tool_call_id':call['id'],
                                             'content':json.dumps(value, ensure_ascii=False)})
                await emit('tool_result', {'tool':f['name'],'result':value})
                if value.get('turn_complete'):
                    terminal = value
            if terminal:
                return terminal.get('summary', 'Waiting for the next event.')
        raise RuntimeError('Tool round limit reached; requesting supervisor guidance')


direct_api_runner = DirectAPIRunner()

