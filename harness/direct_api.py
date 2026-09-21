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
            raise ValueError('Use provider/model, for example a configured alias/model')
        alias, model_id = agent.model.split('/', 1)
        profile = config.providers.get(alias)
        provider = profile.adapter if profile else alias
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
            kwargs['reasoning_effort'] = agent.reasoning_effort
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

