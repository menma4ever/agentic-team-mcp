import asyncio
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import psutil
from pathlib import Path
from core.config import ROOT, settings
from core.workspace import contained
from engine.models import HarnessType


class CLIRunner:
    def __init__(self, config=settings, google_projects_dir=None):
        self.config = config
        self.active_processes = {}
        self.google_projects_dir = Path(google_projects_dir) if google_projects_dir else Path.home()/'.gemini/config/projects'

    def resolve(self, name):
        path = self.config.cli_executable(name)
        if not path:
            raise ValueError(f'{name} is not installed or its path is not configured')
        if Path(path).suffix.lower() in ('.cmd', '.ps1', '.bat'):
            entries = {'codex':'@openai/codex/bin/codex.js',
                       'gemini':'@google/gemini-cli/dist/index.js',
                       'openclaw':'openclaw/openclaw.mjs'}
            entry = Path(path).parent/'node_modules'/entries.get(name, '')
            node = shutil.which('node')
            if name not in entries or not entry.is_file() or not node:
                raise ValueError('Configure a native executable; arbitrary shell shims are unsupported')
            return [node,str(entry)]
        return [path]

    def check_auth_status(self, name):
        if name == 'codex':
            try:
                r = self.resolve('codex')
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                res = subprocess.run(r + ['login', 'status'], capture_output=True, text=True, timeout=2, creationflags=flags)
                text = (res.stdout or res.stderr or '').strip()
                return {'logged_in': res.returncode == 0, 'status_text': text or ('Logged in' if res.returncode == 0 else 'Not logged in')}
            except Exception:
                return {'logged_in': False, 'status_text': 'Not logged in'}
        elif name == 'claude':
            try:
                r = self.resolve('claude')
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                res = subprocess.run(r + ['auth', 'status'], capture_output=True, text=True, timeout=2, creationflags=flags)
                if res.returncode == 0 and res.stdout:
                    d = json.loads(res.stdout)
                    logged_in = bool(d.get('loggedIn'))
                    return {'logged_in': logged_in, 'status_text': 'Logged in' if logged_in else 'Not logged in'}
            except Exception:
                pass
            return {'logged_in': False, 'status_text': 'Not logged in'}
        return None

    def capabilities(self):
        result = []
        for name in ('agy','claude','codex','gemini','hermes','openclaw'):
            available = self.config.check_cli_available(name)
            auth_status = self.check_auth_status(name) if available['available'] and name in ('codex', 'claude') else None
            examples = {
                'agy': ['antigravity/gemini-3.8-flash-high (workhorse)', 'antigravity/claude-opus-4-6-thinking (separate 5h free power)', 'antigravity/claude-sonnet-4-6', 'antigravity/gemini-3.8-flash-low'],
                'claude': ['claude-3-7-sonnet-20250219', 'claude-3-5-haiku-20241022 (junior/cheap)', 'zai/glm-5.3', 'zai/glm-5.3-flash (unthrottled)'],
                'codex': ['openai/GPT-6 Sol', 'openai/gpt-4o', 'openai/o3-mini'],
            }.get(name, [])
            result.append({'name':name,**available,'adapter_supported':name in ('claude','codex','agy'),
                           'label': 'Gemini — Google account (Antigravity CLI)' if name == 'agy' else name,
                           'harness': {'agy':'antigravity','claude':'claude_code','gemini':'gemini_cli'}.get(name,name),
                           'model_examples': examples,
                           'auth_status': auth_status,
                           'note': 'Streaming, sessions and team MCP' if name in ('claude','codex','agy')
                           else 'Install/configure a supported adapter before use; not silently substituted'})
        return result

    def validate(self, harness, model, thinking_budget=0, reasoning_effort=None):
        name = {'claude_code':'claude','gemini_cli':'gemini','antigravity':'agy'}.get(harness.value,harness.value)
        if name not in ('claude','codex','agy'):
            raise ValueError(f'{name} adapter is not verified. For a Google account use antigravity; otherwise use direct_api, claude_code or codex.')
        self.resolve(name)
        if thinking_budget:
            raise ValueError('CLI adapters use reasoning_effort, not a numeric thinking budget')
        if reasoning_effort not in (None, '', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise ValueError('Supported reasoning effort levels are low, medium, high, xhigh, or max')
        if name == 'claude' and '/' in model and not model.startswith(('anthropic/', 'zai/', 'experiential/', 'xpl/')):
            raise ValueError('Claude CLI requires an Anthropic-compatible model name')
        if name == 'claude' and model.startswith('zai/'):
            if model.split('/', 1)[1].lower() not in ('glm-5.3', 'glm-5.3-flash'):
                raise ValueError('Unsupported GLM Coding Plan model')
            if not self.config.get_api_key('zai'):
                raise ValueError('Configure the Z.ai Coding Plan key first')
        if name == 'claude' and model.startswith(('experiential/', 'xpl/')):
            if not self.config.get_api_key('experiential') and not self.config.get_api_key('xpl'):
                raise ValueError('Configure Experiential Labs API key first')
        if name == 'codex' and '/' in model and not model.startswith('openai/'):
            prefix = model.split('/', 1)[0]
            if prefix in ('experiential', 'xpl'):
                if not self.config.get_api_key('experiential') and not self.config.get_api_key('xpl'):
                    raise ValueError('Configure Experiential Labs API key first')
            elif prefix in self.config.providers:
                if not self.config.get_api_key(prefix):
                    raise ValueError(f'Configure {prefix} API key first')
            else:
                raise ValueError('Codex CLI requires an OpenAI, Experiential Labs, or configured provider model name')
        if name == 'agy':
            if '/' in model and model.split('/',1)[0] not in ('antigravity','google','gemini','claude'):
                raise ValueError('Google account models use antigravity/<native model slug>')
            if reasoning_effort not in (None, '', 'low', 'medium', 'high', 'xhigh'):
                raise ValueError('Antigravity supports low, medium, high, or xhigh reasoning effort')
            if not self.config.cli_auth_enabled.get('agy',False):
                raise ValueError('Sign in with Google in Connections, then enable Google account mode')

    def build(self, agent, task_prompt, working_dir, endpoint, token, auth_dir=None):
        self.validate(agent.harness,agent.model,agent.thinking_budget,agent.reasoning_effort)
        name={HarnessType.CLAUDE_CODE:'claude',HarnessType.CODEX:'codex',HarnessType.ANTIGRAVITY:'agy'}[agent.harness]
        args=self.resolve(name)
        env=os.environ.copy()
        env['TEAM_ENDPOINT']=endpoint
        env['TEAM_TOKEN']=token
        # Child receives its own scoped identity, never the owner bearer token.
        env.pop('TEAM_OWNER_TOKEN',None)
        env.pop('CLAUDECODE',None)
        model=agent.model.split('/',1)[-1]
        bridge=[str(ROOT/'main.py'),'--mcp']
        prompt=task_prompt
        if name=='claude':
            mcp={'mcpServers':{'agentic_team':{'command':sys.executable,'args':bridge}}}
            args += ['-p','--output-format','stream-json','--verbose','--model',model,
                     '--append-system-prompt',agent.system_prompt,
                     '--strict-mcp-config','--mcp-config',json.dumps(mcp),
                     '--permission-mode','dontAsk','--allowedTools','Read,Edit,Write,Glob,Grep,Bash,mcp__agentic_team__*']
            if agent.session_id: args += ['--resume',agent.session_id]
            else:
                args += ['--session-id',str(uuid.uuid4())]
            if agent.reasoning_effort: args += ['--effort',agent.reasoning_effort]
            if agent.model.startswith('zai/'):
                # Coding Plan via its supported Claude Code harness, never API-balance fallback.
                for key in list(env):
                    if key.startswith('ANTHROPIC_') or key == 'CLAUDE_CONFIG_DIR':
                        env.pop(key, None)
                env['ANTHROPIC_AUTH_TOKEN'] = self.config.get_api_key('zai')
                env['ANTHROPIC_BASE_URL'] = 'https://api.z.ai/api/anthropic'
                env['ANTHROPIC_MODEL'] = model
                for tier in ('OPUS', 'SONNET', 'HAIKU'):
                    env['ANTHROPIC_DEFAULT_'+tier+'_MODEL'] = model
                env['CLAUDE_CONFIG_DIR'] = str(Path(working_dir)/'.claude-team')
                env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
                env['API_TIMEOUT_MS'] = '120000'
                env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = '8192'
                git_bash = Path(os.environ.get('LOCALAPPDATA', ''))/'hermes/git/bin/bash.exe'
                if git_bash.is_file():
                    env['CLAUDE_CODE_GIT_BASH_PATH'] = str(git_bash)
            elif agent.model.startswith(('experiential/', 'xpl/')) or getattr(agent, 'provider', None) in ('experiential', 'xpl'):
                for key in list(env):
                    if key.startswith('ANTHROPIC_') or key == 'CLAUDE_CONFIG_DIR':
                        env.pop(key, None)
                key = self.config.get_api_key('experiential') or self.config.get_api_key('xpl')
                env['ANTHROPIC_AUTH_TOKEN'] = key
                env['ANTHROPIC_BASE_URL'] = 'https://api.experientiallabs.ai'
                env['ANTHROPIC_MODEL'] = model
                for tier in ('OPUS', 'SONNET', 'HAIKU'):
                    env['ANTHROPIC_DEFAULT_'+tier+'_MODEL'] = model
                env['CLAUDE_CONFIG_DIR'] = str(Path(working_dir)/'.claude-team')
                env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
                env['API_TIMEOUT_MS'] = '120000'
                env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = '8192'
                git_bash = Path(os.environ.get('LOCALAPPDATA', ''))/'hermes/git/bin/bash.exe'
                if git_bash.is_file():
                    env['CLAUDE_CODE_GIT_BASH_PATH'] = str(git_bash)
            elif not self.config.cli_auth_enabled.get('claude',False):
                key=self.config.get_api_key('anthropic')
                if not key: raise ValueError('Configure Anthropic key or enable existing Claude account login')
                env['ANTHROPIC_API_KEY']=key
            else:
                env.pop('ANTHROPIC_API_KEY',None)
                env.pop('ANTHROPIC_AUTH_TOKEN',None)
        elif name == 'codex':
            target_model = model.lower().replace(' ', '-').replace('_', '-')
            provider_prefix = agent.model.split('/', 1)[0] if '/' in agent.model else getattr(agent, 'provider', None)
            is_custom_provider = False
            custom_provider_key = None
            custom_base_url = None
            provider_id = None

            if provider_prefix in ('experiential', 'xpl'):
                is_custom_provider = True
                provider_id = 'experiential'
                custom_provider_key = self.config.get_api_key('experiential') or self.config.get_api_key('xpl')
                custom_base_url = 'https://api.experientiallabs.ai/v1'
                if not custom_provider_key:
                    raise ValueError('Configure Experiential Labs API key first')
            elif provider_prefix and provider_prefix in self.config.providers and provider_prefix != 'openai':
                is_custom_provider = True
                provider_id = provider_prefix
                provider_info = self.config.providers[provider_prefix]
                custom_base_url = provider_info.base_url or 'https://api.experientiallabs.ai/v1'
                custom_provider_key = self.config.get_api_key(provider_prefix)
                if not custom_provider_key:
                    raise ValueError(f'Configure {provider_prefix} API key first')

            args += ['exec','--json','--skip-git-repo-check',
                     '--dangerously-bypass-approvals-and-sandbox',
                     '--model',target_model,
                     '-c','mcp_servers.agentic_team.command='+json.dumps(sys.executable),
                     '-c','mcp_servers.agentic_team.args='+json.dumps(bridge),
                     '-c','mcp_servers.agentic_team.env_vars=["TEAM_ENDPOINT","TEAM_TOKEN"]']

            if is_custom_provider:
                codex_home = Path(working_dir) / '.codex-team'
                codex_home.mkdir(parents=True, exist_ok=True)
                env_key_var = f"{provider_id.upper()}_API_KEY"
                env[env_key_var] = custom_provider_key
                env['CODEX_HOME'] = str(codex_home)
                config_lines = [
                    f'model = "{target_model}"',
                    f'model_provider = "{provider_id}"',
                ]
                if agent.reasoning_effort:
                    config_lines.append(f'model_reasoning_effort = "{agent.reasoning_effort}"')
                config_lines += [
                    f'',
                    f'[model_providers.{provider_id}]',
                    f'name = "{provider_id}"',
                    f'base_url = "{custom_base_url}"',
                    f'env_key = "{env_key_var}"',
                    f'wire_api = "responses"'
                ]
                (codex_home / 'config.toml').write_text('\n'.join(config_lines) + '\n', encoding='utf-8')
            else:
                args += ['--ignore-user-config']
                if not self.config.cli_auth_enabled.get('codex',False):
                    raise ValueError('Enable Codex account mode and sign in through Settings first')
                env.pop('OPENAI_API_KEY',None)

            if agent.reasoning_effort:
                args += ['-c','model_reasoning_effort='+json.dumps(agent.reasoning_effort)]
            if agent.last_error and "no rollout found" in agent.last_error:
                agent.session_id = None
            has_session = False
            if agent.session_id:
                if is_custom_provider:
                    has_session = (codex_home / 'thread_history_1.sqlite').is_file()
                else:
                    has_session = True
            if agent.session_id and has_session:
                args += ['resume',agent.session_id,'-']
            else:
                agent.session_id = None
                args += ['-']
            prompt=agent.system_prompt+'\n\n'+task_prompt
        else:
            args, env, prompt = self.build_google(agent, task_prompt, working_dir, args, env, auth_dir=auth_dir)
        return args, env, prompt

    def prepare_google_account(self, path=None):
        """Called only when the owner enables/signs in to this integration."""
        path = Path(path) if path else Path.home()/'.gemini/antigravity-cli/settings.json'
        data = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        # Google account mode must not silently use an independently billed API key.
        data.pop('modelProvider', None)
        # Preserve all existing ask/deny rules and unrelated user settings.
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name+'.agentic-team.tmp')
        temp.write_text(json.dumps(data,indent=2),encoding='utf-8')
        temp.replace(path)

    def google_project(self, agent, folder, projects_dir=None):
        # Native CLI projects hold workspace roots and project-scoped permissions.
        project_id = str(uuid.uuid5(uuid.NAMESPACE_URL,'agentic-team:'+str(folder)+':'+agent.id))
        target_dir = Path(projects_dir) if projects_dir else self.google_projects_dir
        target_dir.mkdir(parents=True,exist_ok=True)
        path = target_dir/(project_id+'.json')
        data = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {
            'id':project_id,'name':'Agentic Team / '+agent.project_name+' / '+agent.name,
            'projectResources':{'resources':[{'folderUri':'file://'+folder.as_posix()}]},
        }
        if data.get('id') != project_id:
            raise ValueError('Unexpected Antigravity project identity')
        permissions = data.setdefault('permissionGrants',{}).setdefault('permissionGrants',{})
        allowed = permissions.setdefault('allow',[])
        # build() is reached only after the owner opts in to CLI/command execution.
        for rule in ('mcp(agentic_team/*)','command(*)','write_file(*)'):
            if rule not in allowed: allowed.append(rule)
        temp = path.with_suffix('.agentic-team.tmp')
        temp.write_text(json.dumps(data,indent=2),encoding='utf-8')
        temp.replace(path)
        return project_id

    def build_google(self, agent, task_prompt, working_dir, args, env, auth_dir=None):
        folder = Path(working_dir).resolve()
        config_dir = contained(folder,'.agents')
        config_dir.mkdir(exist_ok=True)
        path = contained(folder,'.agents/mcp_config.json')
        data = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        data.setdefault('mcpServers', {})['agentic_team'] = {
            'command':sys.executable,'args':[str(ROOT/'main.py'),'--agent-mcp'],
        }
        # No credentials on disk or in argv: the scoped MCP child inherits env.
        temp = contained(folder,'.agents/mcp_config.agentic-team.tmp')
        temp.write_text(json.dumps(data,indent=2),encoding='utf-8')
        temp.replace(path)
        projects_dir = None
        if auth_dir:
            auth_path = Path(auth_dir).resolve()
            env['USERPROFILE'] = str(auth_path)
            env['HOME'] = str(auth_path)
            env['ANTIGRAVITY_APP_DATA_DIR'] = str(auth_path / '.gemini' / 'antigravity')
            projects_dir = auth_path / '.gemini' / 'config' / 'projects'
            self.prepare_google_account(auth_path / '.gemini' / 'antigravity-cli' / 'settings.json')
        project_id = self.google_project(agent, folder, projects_dir=projects_dir)
        for key in ('GEMINI_API_KEY','GOOGLE_API_KEY','ANTIGRAVITY_API_KEY'):
            env.pop(key,None)
        args += ['--input-format','stream-json','--output-format','stream-json',
                 '--project',project_id,
                 '--disable-slash-commands','--model',agent.model.split('/',1)[-1],
                 '--print-timeout',str(self.config.request_timeout_seconds)+'s']
        if agent.session_id: args += ['--conversation',agent.session_id]
        effort = agent.reasoning_effort
        if not effort and agent.model.split('/',1)[-1] == 'gemini-3.8-flash':
            effort = 'medium'  # This native slug requires an explicit effort.
        if effort: args += ['--effort',effort]
        text = (agent.system_prompt+'\n\nYou are operating as a member of Agentic Team. '
                'Use the agentic_team MCP tools for delegation, project files, status and reports. '
                'Paths for those tools are relative to the team project, not this CLI working folder. '
                'Use the team tools instead of native subagents. If a native tool is denied, report the blocker.\n\n'+task_prompt)
        prompt = json.dumps({'event':'user','message':{'content':text}},ensure_ascii=False)+'\n'
        return args,env,prompt

    async def terminate_process(self, agent_id):
        proc=self.active_processes.get(agent_id)
        if not proc or proc.returncode is not None: return False
        if os.name=='nt':
            command=str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32'/'taskkill.exe')
            killer=await asyncio.create_subprocess_exec(command,'/PID',str(proc.pid),'/T','/F',
                stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            await killer.wait()
        else:
            import signal
            try: os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError: pass
        try:
            await asyncio.wait_for(proc.wait(),10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        return True

    async def _process(self, aid, argv, cwd, emit, env=None, stdin=None, timeout=600, parse=None, monitor=None, launched_event=None):
        options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        proc=await asyncio.create_subprocess_exec(*argv,cwd=str(cwd),env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,**options)
        self.active_processes[aid]=proc
        output=[]
        total=0
        async def read(stream,label):
            nonlocal total
            buffer=''
            decoder=codecs.getincrementaldecoder('utf-8')(errors='replace')
            while True:
                chunk=await stream.read(4096)
                text=self.config.redact(decoder.decode(chunk,final=not chunk))
                if not chunk and not text: break
                total+=len(text)
                if total>8_000_000: raise RuntimeError('Command output limit exceeded')
                output.append(text)
                await emit('output',{'stream':label,'text':text})
                if parse:
                    buffer+=text
                    while '\n' in buffer:
                        line,buffer=buffer.split('\n',1)
                        parse(line)
                if not chunk: break
            if parse and buffer: parse(buffer)
        readers=[]
        try:
            await emit('process_started',{'pid':proc.pid,'created_at':psutil.Process(proc.pid).create_time()})
            if launched_event and not launched_event.is_set():
                async def _delayed_launch():
                    await asyncio.sleep(2.0)
                    if not launched_event.is_set():
                        launched_event.set()
                asyncio.create_task(_delayed_launch())
            readers=[asyncio.create_task(read(proc.stdout,'stdout')),asyncio.create_task(read(proc.stderr,'stderr'))]
            if monitor:
                readers.append(asyncio.create_task(monitor(proc)))
            if stdin is not None:
                proc.stdin.write(stdin.encode('utf-8'))
                await proc.stdin.drain()
                proc.stdin.close()
            async with asyncio.timeout(timeout):
                await asyncio.gather(*readers)
                await proc.wait()
            await emit('process_exit',{'pid':proc.pid,'exit_code':proc.returncode})
            if proc.returncode:
                raise RuntimeError(f'Process exited {proc.returncode}: '+''.join(output)[-2000:])
            return ''.join(output)
        finally:
            if proc.returncode is None:
                await asyncio.shield(self.terminate_process(aid))
            for task in readers:
                if not task.done(): task.cancel()
            await asyncio.gather(*readers,return_exceptions=True)
            self.active_processes.pop(aid,None)

    async def execute_task(self,agent,task_prompt,working_dir,emit,endpoint='',token='',allow_commands=False,auth_dir=None,google_account=None,previous_google_quota=None,launched_event=None):
        if not allow_commands: raise PermissionError('Local command execution is disabled')
        if not endpoint or not token: raise RuntimeError('Team MCP connection is unavailable')
        argv,env,prompt=self.build(agent,task_prompt,working_dir,endpoint,token,auth_dir=auth_dir)
        final=[]
        errors=[]
        monitor = None
        verified = False
        completed_response = False
        current_error_step = False
        stale_quota = False
        if agent.harness == HarnessType.ANTIGRAVITY:
            # A unique file prevents an old run's quota error from triggering a new failover.
            log_dir = Path(auth_dir) if auth_dir else Path(working_dir) / '.agents'
            log_dir = log_dir / '.gemini' / 'antigravity-cli' / 'log'
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / ('team-' + uuid.uuid4().hex + '.log')
            argv += ['--log-file', str(log_path)]

            async def watch_google(proc):
                nonlocal verified
                offset = 0
                pending = b''
                while True:
                    finished = proc.returncode is not None
                    try:
                        with log_path.open('rb') as log:
                            log.seek(offset)
                            chunk = log.read(65536)
                            offset += len(chunk)
                    except FileNotFoundError:
                        chunk = b''
                    pending += chunk
                    lines = pending.split(b'\n')
                    pending = lines.pop()
                    if finished and not chunk and pending:
                        lines.append(pending)
                        pending = b''
                    for raw in lines:
                        line = raw.decode('utf-8', errors='replace')
                        identity = re.search(r'server_oauth\.go:\d+\] applyAuthResult: email=([^,\s]+)', line)
                        if identity and google_account:
                            if identity.group(1).casefold() != google_account['email'].casefold():
                                raise RuntimeError('Google CLI authenticated as a different account; stopped to protect account selection')
                            if not verified:
                                verified = True
                                if launched_event and not launched_event.is_set():
                                    launched_event.set()
                                await emit('auth_verified', {'account_id': google_account['account_id'],
                                    'source': 'native_cli_login'})
                        # Read the CLI's own error record, never arbitrary prompt/tool text.
                        failure = re.search(r'(?:run\.go:\d+\] Run: attempt \d+ failed \(|errorreport\.go:\d+\] (?:agent executor error: )?(?:generating and executing: )?)(.*)', line)
                        if failure:
                            detail = failure.group(1)
                            if re.search(r'individual quota (?:reached|exhausted)|quota exceeded', detail, re.I):
                                detail = re.sub(r'\), retrying in .*$', '', detail)
                                await emit('auth_quota_detected', {'account_id': google_account['account_id'] if google_account else None,
                                    'source': 'native_cli_retry'})
                                raise RuntimeError(self.config.redact(detail))
                    if finished and not chunk:
                        break
                    await asyncio.sleep(.2)
            monitor = watch_google
        turn_input_tokens = 0
        turn_output_tokens = 0
        turn_cache_tokens = 0
        has_step_usage = False
        def parse(line):
            nonlocal completed_response, current_error_step, stale_quota, turn_input_tokens, turn_output_tokens, turn_cache_tokens, has_step_usage
            try: event=json.loads(line)
            except ValueError: return
            if not isinstance(event,dict): return
            if agent.harness == HarnessType.ANTIGRAVITY:
                payload=event.get(event.get('event',''),{})
                if not isinstance(payload,dict): payload={}
                conversation_id=event.get('conversation_id') or payload.get('conversation_id')
                if conversation_id: agent.session_id=conversation_id
                if event.get('event') == 'step_update':
                    if payload.get('step_type') == 'error_message': current_error_step = True
                    if payload.get('step_type') == 'agent_response' and payload.get('state') == 'DONE':
                        completed_response = True
                    if payload.get('state') == 'DONE' and payload.get('usage') and isinstance(payload['usage'], dict):
                        u = payload['usage']
                        turn_input_tokens += int(u.get('input_tokens') or 0)
                        turn_output_tokens += int(u.get('output_tokens') or 0) + int(u.get('thinking_tokens') or 0)
                        turn_cache_tokens += int(u.get('cache_read_tokens') or 0)
                        has_step_usage = True
                        agent.last_turn_usage = {
                            'input_tokens': turn_input_tokens,
                            'output_tokens': turn_output_tokens,
                            'cache_read_tokens': turn_cache_tokens,
                        }
                if event.get('event')=='result':
                    if not has_step_usage and payload.get('usage') and isinstance(payload['usage'], dict):
                        agent.last_turn_usage = payload['usage']
                    if payload.get('status') != 'SUCCESS':
                        error = str(payload.get('error') or 'Antigravity ended with '+str(payload.get('status')))
                        # AGY permanently carries any historical turn's error in a resumed
                        # conversation's cumulative `result` summary despite new completed turns.
                        # Accept the completed response whenever this invocation produced a
                        # fresh completed response with no error step in this run. The log
                        # monitor independently catches any real native quota failure.
                        if (completed_response and not current_error_step
                                and isinstance(payload.get('response'), str) and payload['response']):
                            stale_quota = True
                            final.append(payload['response'])
                        else:
                            errors.append(error)
                    elif isinstance(payload.get('response'),str):
                        final.append(payload['response'])
                return
            if event.get('session_id'): agent.session_id=event['session_id']
            if event.get('type')=='thread.started': agent.session_id=event.get('thread_id')
            if event.get('usage') and isinstance(event['usage'], dict):
                agent.last_turn_usage = event['usage']
            if event.get('type')=='result':
                if event.get('is_error'): errors.append(event.get('result','CLI reported error'))
                elif event.get('result'): final.append(event['result'])
            if event.get('type')=='item.completed':
                item=event.get('item',{})
                if item.get('type')=='agent_message': final.append(item.get('text',''))
            if event.get('type') in ('turn.failed','error'):
                errors.append(str(event.get('error',event)))
        async def on_event(kind,data):
            if kind=='process_started': agent.pid=data['pid']
            await emit(kind,data)
        try:
            await self._process(agent.id,argv,working_dir,on_event,env,prompt,
                                None if agent.harness == HarnessType.CLAUDE_CODE else self.config.request_timeout_seconds,parse,
                                monitor=monitor,launched_event=launched_event)
        except RuntimeError as exc:
            if "no rollout found" in str(exc):
                agent.session_id = None
            # Preserve structured provider errors; an output tail can omit the quota/reset.
            if errors:
                raise RuntimeError('; '.join(errors)) from exc
            raise
        if errors: raise RuntimeError('; '.join(errors))
        if google_account and not verified:
            raise RuntimeError('Google CLI finished without confirming the selected login identity')
        if stale_quota:
            await emit('auth_stale_quota_ignored', {'session_id': agent.session_id,
                'reason': 'Previous quota error repeated alongside a new completed response'})
        if not final: raise RuntimeError('CLI returned no final message; inspect streamed session events')
        return '\n'.join(final)

    async def run_command(self,agent,argv,timeout,emit):
        if not argv or not all(isinstance(x,str) and x for x in argv):
            raise ValueError('argv must be a nonempty argument array')
        env = os.environ.copy()
        venv_scripts = str(ROOT/'.venv'/'Scripts')
        python_dir = str(Path(sys.executable).resolve().parent)
        env['PATH'] = venv_scripts + os.pathsep + python_dir + os.pathsep + env.get('PATH', '')
        out=await self._process(agent.id,argv,Path(agent.working_dir),emit,env=env,timeout=timeout)
        return {'exit_code':0,'output':out[-50000:]}

