import asyncio
import codecs
import json
import os
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

    def capabilities(self):
        result = []
        for name in ('agy','claude','codex','gemini','hermes','openclaw'):
            available = self.config.check_cli_available(name)
            result.append({'name':name,**available,'adapter_supported':name in ('claude','codex','agy'),
                           'label': 'Gemini — Google account (Antigravity CLI)' if name == 'agy' else name,
                           'harness': {'agy':'antigravity','claude':'claude_code','gemini':'gemini_cli'}.get(name,name),
                           'model_examples': [],
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
        if name == 'claude' and '/' in model and not model.startswith(('anthropic/', 'zai/')):
            raise ValueError('Claude CLI requires an Anthropic-compatible model name')
        if name == 'claude' and model.startswith('zai/'):
            if model.split('/', 1)[1].lower() not in ('glm-5.3', 'glm-5.3-flash'):
                raise ValueError('Unsupported GLM Coding Plan model')
            if not self.config.get_api_key('zai'):
                raise ValueError('Configure the Z.ai Coding Plan key first')
        if name == 'codex' and '/' in model and not model.startswith('openai/'):
            raise ValueError('Codex CLI account adapter requires an OpenAI model name; use direct_api for custom providers')
        if name == 'agy':
            if '/' in model and model.split('/',1)[0] not in ('antigravity','google','gemini'):
                raise ValueError('Google account models use antigravity/<native model slug>')
            if reasoning_effort not in (None, '', 'low', 'medium', 'high'):
                raise ValueError('Antigravity supports low, medium or high reasoning effort')
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
            elif not self.config.cli_auth_enabled.get('claude',False):
                key=self.config.get_api_key('anthropic')
                if not key: raise ValueError('Configure Anthropic key or enable existing Claude account login')
                env['ANTHROPIC_API_KEY']=key
            else:
                env.pop('ANTHROPIC_API_KEY',None)
                env.pop('ANTHROPIC_AUTH_TOKEN',None)
        elif name == 'codex':
            args += ['exec','--json','--skip-git-repo-check','--ignore-user-config','--model',model,
                     '-c','approval_policy="never"','-c','sandbox_mode="workspace-write"',
                     '-c','mcp_servers.agentic_team.command='+json.dumps(sys.executable),
                     '-c','mcp_servers.agentic_team.args='+json.dumps(bridge),
                     '-c','mcp_servers.agentic_team.env_vars=["TEAM_ENDPOINT","TEAM_TOKEN"]']
            if agent.reasoning_effort:
                args += ['-c','model_reasoning_effort='+json.dumps(agent.reasoning_effort)]
            if agent.session_id: args += ['resume',agent.session_id,'-']
            else: args += ['-']
            prompt=agent.system_prompt+'\n\n'+task_prompt
            if not self.config.cli_auth_enabled.get('codex',False):
                raise ValueError('Enable Codex account mode and sign in through Settings first')
            env.pop('OPENAI_API_KEY',None)
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

    async def _process(self, aid, argv, cwd, emit, env=None, stdin=None, timeout=600, parse=None):
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
            readers=[asyncio.create_task(read(proc.stdout,'stdout')),asyncio.create_task(read(proc.stderr,'stderr'))]
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

    async def execute_task(self,agent,task_prompt,working_dir,emit,endpoint='',token='',allow_commands=False,auth_dir=None):
        if not allow_commands: raise PermissionError('Local command execution is disabled')
        if not endpoint or not token: raise RuntimeError('Team MCP connection is unavailable')
        argv,env,prompt=self.build(agent,task_prompt,working_dir,endpoint,token,auth_dir=auth_dir)
        final=[]
        errors=[]
        def parse(line):
            try: event=json.loads(line)
            except ValueError: return
            if not isinstance(event,dict): return
            if agent.harness == HarnessType.ANTIGRAVITY:
                payload=event.get(event.get('event',''),{})
                if not isinstance(payload,dict): payload={}
                conversation_id=event.get('conversation_id') or payload.get('conversation_id')
                if conversation_id: agent.session_id=conversation_id
                if event.get('event')=='result':
                    if payload.get('status') != 'SUCCESS':
                        errors.append(str(payload.get('error') or 'Antigravity ended with '+str(payload.get('status'))))
                    elif isinstance(payload.get('response'),str):
                        final.append(payload['response'])
                return
            if event.get('session_id'): agent.session_id=event['session_id']
            if event.get('type')=='thread.started': agent.session_id=event.get('thread_id')
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
        await self._process(agent.id,argv,working_dir,on_event,env,prompt,
                            None if agent.harness == HarnessType.CLAUDE_CODE else self.config.request_timeout_seconds,parse)
        if errors: raise RuntimeError('; '.join(errors))
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

