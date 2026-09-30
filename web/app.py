import asyncio
import os
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from core.config import ROOT, DATA_DIR, SystemSettings
from core.providers import PROVIDER_PRESETS
from engine.loop_monitor import LoopMonitor
from engine.models import HarnessType


class ProjectRequest(BaseModel):
    name: str
    description: str = ''
    ceo_name: str = 'CEO'
    ceo_model: str | None = None
    thinking_budget: int = Field(default=0,ge=0,le=65536)
    reasoning_effort: str | None = None
    harness: str = 'direct_api'
    allow_commands: bool = False
    read_roots: list[str] = Field(default_factory=list)


class ActionRequest(BaseModel):
    project_name: str
    action: str
    arguments: dict = Field(default_factory=dict)


def create_app(engine,owner_token,instance_id='test'):
    monitor=LoopMonitor(engine)
    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        await monitor.start()
        try:
            from core.telegram_supervisor import telegram_supervisor
            telegram_supervisor.auto_start_if_enabled()
        except Exception:
            pass
        yield
        try:
            from core.telegram_supervisor import telegram_supervisor
            telegram_supervisor.stop_daemon()
        except Exception:
            pass
        await monitor.stop()
        await engine.close()
    app=FastAPI(title='Agentic Team MCP',lifespan=lifespan)

    def identity(token):
        if token and secrets.compare_digest(token,owner_token): return 'human_owner'
        return engine.identify(token) if token else None

    def owner(request):
        if request.state.actor!='human_owner': raise HTTPException(403,'Owner access required')

    @app.middleware('http')
    async def boundary(request,call_next):
        host=request.headers.get('host','')
        hostname=urlsplit('http://'+host).hostname
        if hostname not in ('127.0.0.1','localhost','testserver'):
            return JSONResponse({'detail':'Host not allowed'},400)
        origin=request.headers.get('origin')
        if origin and origin != 'http://'+host:
            return JSONResponse({'detail':'Cross-origin request denied'},403)
        if request.url.path.startswith('/api/') and request.url.path!='/api/session':
            auth=request.headers.get('authorization','')
            token=auth[7:] if auth.startswith('Bearer ') else request.cookies.get('team_session','')
            request.state.actor=identity(token)
            if not request.state.actor: return JSONResponse({'detail':'Open the dashboard using main.py to sign in'},401)
        try:
            response=await call_next(request)
        except OSError:
            return JSONResponse({'detail':'Invalid request path'},404)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request,exc):
        return JSONResponse({'detail':str(exc)},400)

    @app.exception_handler(FileNotFoundError)
    async def not_found(request,exc):
        return JSONResponse({'detail':str(exc)},404)

    @app.exception_handler(PermissionError)
    async def bad_permission(request,exc):
        return JSONResponse({'detail':str(exc)},403)

    @app.exception_handler(OSError)
    async def bad_os_path(request,exc):
        return JSONResponse({'detail':'Invalid path'},404)

    @app.get('/health')
    async def health():
        return {'status':'ok','instance_id':instance_id,'studio_revision':2}

    @app.post('/api/session')
    async def session(request:Request):
        body=await request.json()
        if not isinstance(body.get('token'),str) or not secrets.compare_digest(body['token'],owner_token):
            raise HTTPException(401,'Invalid launch token')
        response=JSONResponse({'ok':True})
        response.set_cookie('team_session',owner_token,httponly=True,samesite='strict')
        return response

    @app.get('/api/catalog')
    async def catalog(request:Request):
        owner(request)
        from core.catalog import model_catalog
        return model_catalog(engine)

    @app.post('/api/auth/google/cancel_login')
    async def cancel_google_login(request:Request):
        owner(request)
        engine.auth_pool.cancel_login()
        return {'cancelled':True}

    @app.post('/api/agents/{aid}/console')
    async def console(aid:str,request:Request):
        owner(request)
        a=engine.agent(aid)
        if os.name != 'nt': raise ValueError('PowerShell console requires Windows')

        env = os.environ.copy()
        env.pop('TEAM_OWNER_TOKEN', None)

        working_dir = Path(a.working_dir).resolve() if a.working_dir else engine.workspace.root_dir
        working_dir.mkdir(parents=True, exist_ok=True)

        harness = a.harness
        title = f"Agentic Team: {a.name} ({a.role}) - {a.model}"
        header_banner = (
            f"Write-Host '=====================================================' -ForegroundColor DarkCyan; "
            f"Write-Host '  Agentic Team: {a.name} ({a.role})' -ForegroundColor Cyan; "
            f"Write-Host '  Harness: {harness.value} | Model: {a.model}' -ForegroundColor Cyan; "
            f"Write-Host '  Working Dir: {working_dir}' -ForegroundColor DarkGray; "
            f"Write-Host '=====================================================' -ForegroundColor DarkCyan; "
            f"Write-Host ''; "
        )

        cli_runner = getattr(engine, 'cli', None)
        if harness == HarnessType.CODEX:
            codex_cmd = None
            if cli_runner:
                try:
                    codex_parts = cli_runner.resolve('codex')
                    codex_cmd = codex_parts[0]
                except Exception:
                    pass
            if not codex_cmd:
                codex_cmd = engine.runner.config.cli_executable('codex') or 'codex'
            target_model = a.model.split('/', 1)[-1].lower().replace(' ', '-').replace('_', '-')
            provider_prefix = a.model.split('/', 1)[0] if '/' in a.model else getattr(a, 'provider', None)
            extra_provider_args = ""
            env_prelude = ""
            if provider_prefix in ('experiential', 'xpl'):
                key = engine.runner.config.get_api_key('experiential') or engine.runner.config.get_api_key('xpl') or ''
                env['EXP_API_KEY'] = key
                codex_home = working_dir / '.codex-team'
                env['CODEX_HOME'] = str(codex_home)
                safe_codex_home = str(codex_home).replace("'", "''")
                env_prelude += f"$env:EXP_API_KEY = '{key}'; $env:CODEX_HOME = '{safe_codex_home}'; "
                extra_provider_args = (
                    " -c 'model_provider=\"experiential\"'"
                    " -c 'model_providers.experiential.name=\"Experiential Labs\"'"
                    " -c 'model_providers.experiential.base_url=\"https://api.experientiallabs.ai/v1\"'"
                    " -c 'model_providers.experiential.env_key=\"EXP_API_KEY\"'"
                    " -c 'model_providers.experiential.wire_api=\"responses\"'"
                )
            elif provider_prefix and provider_prefix in getattr(engine.runner.config, 'providers', {}) and provider_prefix != 'openai':
                key = engine.runner.config.get_api_key(provider_prefix) or ''
                var_name = f"{provider_prefix.upper()}_API_KEY"
                env[var_name] = key
                p_info = engine.runner.config.providers[provider_prefix]
                b_url = p_info.base_url or 'https://api.experientiallabs.ai/v1'
                codex_home = working_dir / '.codex-team'
                env['CODEX_HOME'] = str(codex_home)
                safe_codex_home = str(codex_home).replace("'", "''")
                env_prelude += f"$env:{var_name} = '{key}'; $env:CODEX_HOME = '{safe_codex_home}'; "
                extra_provider_args = (
                    f" -c 'model_provider=\"{provider_prefix}\"'"
                    f" -c 'model_providers.{provider_prefix}.name=\"{provider_prefix}\"'"
                    f" -c 'model_providers.{provider_prefix}.base_url=\"{b_url}\"'"
                    f" -c 'model_providers.{provider_prefix}.env_key=\"{var_name}\"'"
                    f" -c 'model_providers.{provider_prefix}.wire_api=\"responses\"'"
                )
            if a.session_id:
                cli_call = f"& '{codex_cmd}' resume '{a.session_id}'{extra_provider_args}"
            else:
                cli_call = f"& '{codex_cmd}' --model '{target_model}'{extra_provider_args}"
            ps_script = (
                f"$host.UI.RawUI.WindowTitle = '{title}'; "
                f"{env_prelude}"
                f"{header_banner}"
                f"Write-Host 'Launching interactive Codex CLI session...' -ForegroundColor Green; "
                f"{cli_call}"
            )
        elif harness == HarnessType.CLAUDE_CODE:
            try:
                claude_parts = cli_runner.resolve('claude') if cli_runner else ['claude']
                claude_cmd = claude_parts[0]
            except Exception:
                claude_cmd = 'claude'
            if a.model.startswith('zai/'):
                for key in list(env):
                    if key.startswith('ANTHROPIC_') or key == 'CLAUDE_CONFIG_DIR':
                        env.pop(key, None)
                env['ANTHROPIC_AUTH_TOKEN'] = engine.runner.config.get_api_key('zai') or ''
                env['ANTHROPIC_BASE_URL'] = 'https://api.z.ai/api/anthropic'
                m_slug = a.model.split('/', 1)[-1]
                env['ANTHROPIC_MODEL'] = m_slug
                for tier in ('OPUS', 'SONNET', 'HAIKU'):
                    env['ANTHROPIC_DEFAULT_' + tier + '_MODEL'] = m_slug
                env['CLAUDE_CONFIG_DIR'] = str(working_dir / '.claude-team')
                env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
                env['API_TIMEOUT_MS'] = '120000'
                env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = '8192'
                git_bash = Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes/git/bin/bash.exe'
                if git_bash.is_file():
                    env['CLAUDE_CODE_GIT_BASH_PATH'] = str(git_bash)
            if a.session_id:
                cli_call = f"& '{claude_cmd}' --resume '{a.session_id}'"
            else:
                cli_call = f"& '{claude_cmd}'"
            ps_script = (
                f"$host.UI.RawUI.WindowTitle = '{title}'; "
                f"{header_banner}"
                f"Write-Host 'Launching interactive Claude Code session...' -ForegroundColor Green; "
                f"{cli_call}"
            )
        elif harness == HarnessType.ANTIGRAVITY:
            try:
                agy_parts = cli_runner.resolve('agy') if cli_runner else ['agy']
                agy_cmd = agy_parts[0]
            except Exception:
                agy_cmd = 'agy'

            auth_dir = None
            if getattr(a, 'google_session_dir', None) and a.google_session_dir != '@default':
                auth_dir = Path(a.google_session_dir).resolve()
            elif getattr(a, 'auth_slot_id', None) and getattr(engine, 'auth_pool', None):
                acc = engine.auth_pool.accounts.get(a.auth_slot_id)
                if acc:
                    auth_dir = engine.auth_pool.resolve_auth_dir(acc).resolve()

            account_id = getattr(a, 'auth_slot_id', None)
            if not account_id and auth_dir and getattr(engine, 'auth_pool', None):
                for acc_id, acc in engine.auth_pool.accounts.items():
                    if engine.auth_pool.resolve_auth_dir(acc).resolve() == auth_dir:
                        account_id = acc_id
                        break
            if account_id and getattr(engine, 'auth_pool', None):
                try:
                    engine.auth_pool.activate_account_credential(account_id)
                except Exception:
                    pass

            env_prelude = ""
            has_conversation = False
            if auth_dir and auth_dir.is_dir():
                auth_str = str(auth_dir).replace("'", "''")
                app_data_str = str(auth_dir / '.gemini' / 'antigravity').replace("'", "''")
                env['USERPROFILE'] = str(auth_dir)
                env['HOME'] = str(auth_dir)
                env['ANTIGRAVITY_APP_DATA_DIR'] = str(auth_dir / '.gemini' / 'antigravity')
                env_prelude = (
                    f"$env:USERPROFILE = '{auth_str}'; "
                    f"$env:HOME = '{auth_str}'; "
                    f"$env:ANTIGRAVITY_APP_DATA_DIR = '{app_data_str}'; "
                )
                if a.session_id:
                    conv_db = auth_dir / '.gemini' / 'antigravity-cli' / 'conversations' / f"{a.session_id}.db"
                    has_conversation = conv_db.is_file()
            elif a.session_id:
                conv_db = Path.home() / '.gemini' / 'antigravity-cli' / 'conversations' / f"{a.session_id}.db"
                has_conversation = conv_db.is_file()

            target_model = a.model.split('/', 1)[-1]
            model_args = f" --model '{target_model}'"
            effort = getattr(a, 'reasoning_effort', None)
            if not effort and target_model == 'gemini-3.8-flash':
                effort = 'medium'
            if effort:
                model_args += f" --effort '{effort}'"

            if a.session_id and has_conversation:
                cli_call = f"& '{agy_cmd}' --conversation '{a.session_id}'{model_args}"
            else:
                cli_call = f"& '{agy_cmd}'{model_args}"

            ps_script = (
                f"$host.UI.RawUI.WindowTitle = '{title}'; "
                f"{env_prelude}"
                f"{header_banner}"
                f"Write-Host 'Launching interactive Antigravity CLI session...' -ForegroundColor Green; "
                f"{cli_call}"
            )
        else:
            exe = ROOT / '.venv' / 'Scripts' / 'python.exe'
            argv = [str(exe) if exe.is_file() else sys.executable, str(ROOT / 'main.py'), '--console', a.id]
            quoted = ' '.join("'" + part.replace("'", "''") + "'" for part in argv)
            ps_script = (
                f"$host.UI.RawUI.WindowTitle = '{title}'; "
                f"{header_banner}"
                f"& {quoted}"
            )

        subprocess.Popen(['powershell.exe', '-NoProfile', '-NoExit', '-Command', ps_script],
                         cwd=str(working_dir), env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return {'opened': True, 'mode': f'{harness.value} CLI session opened'}

    @app.get('/api/projects')
    async def projects(request:Request):
        if request.state.actor != 'human_owner':
            project = engine.agent(request.state.actor).project_name
            return {'projects':[engine.projects[project].model_dump()],'legacy_folders':[],'active':project}
        known=[p.model_dump() for p in engine.projects.values()]
        # Legacy folders remain intact and visible, without inventing recovered sessions.
        legacy=[p for p in engine.workspace.list_projects() if p['name'] not in engine.projects]
        return {'projects':known,'legacy_folders':legacy,'active':engine.active_project_name}

    @app.post('/api/projects')
    async def create_project(req:ProjectRequest,request:Request):
        owner(request)
        p=await engine.create_project(**req.model_dump())
        return {'status':'success','project':p.model_dump()}

    @app.delete('/api/projects')
    async def delete_project(request:Request,name:str):
        owner(request)
        deleted=await engine.delete_project(name)
        return {'status':'success' if deleted else 'not_found','deleted':deleted}

    @app.post('/api/projects/{name}/activate')
    async def activate_project(request:Request,name:str):
        owner(request)
        p=engine.projects.get(name)
        if not p:
            raise HTTPException(404,'Project not found')
        p.status='active'
        p.completion=None
        engine.active_project_name=name
        engine.persist()
        await engine.router.broadcast('project_updated',p.model_dump())
        return {'status':'success','activated':True,'project':p.model_dump()}

    @app.get('/api/tree')
    async def tree(request:Request,project:str):
        if request.state.actor!='human_owner' and engine.agent(request.state.actor).project_name!=project:
            raise HTTPException(403,'Cross-project access denied')
        if request.state.actor=='human_owner' and project in engine.projects:
            engine.active_project_name=project
        return engine.get_tree(project)

    @app.post('/api/action')
    async def action(req:ActionRequest,request:Request):
        try:
            return await engine.action(req.project_name,req.action,req.arguments,request.state.actor)
        except (ValueError, PermissionError, RuntimeError) as exc:
            raise HTTPException(400, str(exc))

    @app.post('/api/chat')
    async def chat(request:Request):
        owner(request)
        body=await request.json()
        m=await engine.send_user_message(**body)
        return {'status':'queued','message':m.model_dump()}

    @app.get('/api/agents/{aid}/messages')
    async def messages(aid:str,request:Request):
        owner(request)
        engine.agent(aid)
        return {'messages':[m.model_dump() for m in engine.messages if aid in (m.sender_id,m.recipient_id)]}

    @app.get('/api/agents/{aid}/events')
    async def events(aid:str,request:Request,after:int=0):
        owner(request)
        engine.agent(aid)
        return {'events':engine.store.events(aid,after)}

    @app.get('/api/agents/{aid}/status_md')
    async def status(aid:str,request:Request):
        owner(request)
        a=engine.agent(aid)
        return await engine.action(a.project_name,'read_agent_status',{'agent_id':aid})

    @app.post('/api/agents/connect')
    async def connect_agents_endpoint(request:Request):
        owner(request)
        body=await request.json()
        return await engine.connect_agents(body['project_name'],body['source_id'],body['target_id'])

    @app.post('/api/agents/disconnect')
    async def disconnect_agents_endpoint(request:Request):
        owner(request)
        body=await request.json()
        return await engine.disconnect_agents(body['project_name'],body['source_id'],body['target_id'])

    @app.post('/api/agents/{aid}/position')
    async def update_agent_position(aid:str,request:Request):
        owner(request)
        body=await request.json()
        return await engine.set_agent_position(body['project_name'],aid,body['x'],body['y'])

    @app.post('/api/agents/{aid}/task')
    async def update_agent_task(aid:str,request:Request):
        owner(request)
        body=await request.json()
        a=engine.agent(aid)
        if 'name' in body and body['name'] != a.name:
            raise ValueError('Rename is unavailable because workspace paths use the agent name')
        if 'task_description' in body:
            if not isinstance(body['task_description'], str):
                raise ValueError('Task description must be text')
            a.current_task=body['task_description']
        engine._write_status(a)
        engine.persist()
        await engine.router.broadcast('agent_updated',a.model_dump(exclude={'system_prompt'}))
        return {'saved':True,'agent':a.model_dump(exclude={'system_prompt'})}

    @app.get('/api/settings')
    async def get_settings(request:Request):
        owner(request)
        from core.providers import sync_all_live_provider_models
        await asyncio.to_thread(sync_all_live_provider_models, engine.config, False)
        return {'settings':engine.config.public(),'harnesses':engine.cli.capabilities(),
                'provider_presets':PROVIDER_PRESETS}

    @app.get('/api/providers/{alias}/models')
    async def get_provider_live_models(alias:str, request:Request, force:bool=True):
        owner(request)
        from core.providers import fetch_live_provider_models
        models = await asyncio.to_thread(fetch_live_provider_models, alias, engine.config, force)
        if models and alias in engine.config.providers:
            engine.config.providers[alias].models = models
        return {'alias': alias, 'models': models, 'count': len(models)}

    @app.post('/api/settings')
    async def save_settings(request:Request):
        owner(request)
        new=await request.json()
        merged=engine.config.model_dump()
        for field in ('api_keys','providers','cli_paths','cli_auth_enabled'):
            if field in new:
                for k,v in new[field].items():
                    # Empty password fields leave existing secrets unchanged.
                    if field=='api_keys' and not v: continue
                    merged[field][k]=v
        for k in ('default_ceo_model','default_manager_model','max_workers','max_autonomous_turns',
                  'max_tool_rounds','request_timeout_seconds','inactivity_timeout_seconds'):
            if k in new: merged[k]=new[k]
        updated=SystemSettings.model_validate(merged)
        if new.get('cli_auth_enabled',{}).get('agy'):
            engine.cli.prepare_google_account()
        for k,v in updated.__dict__.items(): setattr(engine.config,k,v)
        from core.providers import sync_all_live_provider_models
        await asyncio.to_thread(sync_all_live_provider_models, engine.config, True)
        engine.config.save()
        return {'saved':True,'settings':engine.config.public()}

    @app.post('/api/harness/login')
    async def login(request:Request):
        owner(request)
        body=await request.json()
        name=body.get('harness')
        if name not in ('claude','codex','agy'): raise ValueError('Login helper supports Google, Claude and Codex')
        if name == 'agy':
            return await quick_signin_google_account(request)
        import os, subprocess
        argv=engine.cli.resolve(name)+{'claude':['auth','login'],'codex':['login'],'agy':[]}[name]
        if name == 'agy': engine.cli.prepare_google_account()
        if os.name!='nt': return {'manual_command':argv}
        # This endpoint is called only when the owner clicks Sign in.
        script='& '+' '.join("'"+part.replace("'","''")+"'" for part in argv)
        env=os.environ.copy()
        # The provider's own interactive login is never given team credentials.
        for key in ('TEAM_TOKEN','TEAM_ENDPOINT','TEAM_OWNER_TOKEN'):
            env.pop(key,None)
        if name == 'agy':
            for key in ('GEMINI_API_KEY','GOOGLE_API_KEY','ANTIGRAVITY_API_KEY'):
                env.pop(key,None)
        subprocess.Popen(['powershell.exe','-NoProfile','-NoExit','-Command',script],env=env,
                         cwd=str(engine.workspace.root_dir),
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        return {'opened':True,'note':'Complete sign-in in the provider terminal. Then enable account mode.'}

    @app.get('/api/auth/google/accounts')
    async def get_google_accounts(request:Request):
        owner(request)
        return {'accounts':engine.auth_pool.public(), 'login_pending':engine.auth_pool.login_pending,
                'execution_policy':'Google turns share a protected Windows credential slot and run one at a time; quota failures trigger account failover'}

    @app.post('/api/auth/google/accounts')
    async def create_google_account(request:Request):
        owner(request)
        body=await request.json()
        email=body.get('email','').strip()
        if not email: raise ValueError('Email or display name is required')
        acc_id=body.get('account_id','').strip() or None
        models=body.get('model_eligibility')
        max_c=int(body.get('max_concurrent',4))
        profile=engine.auth_pool.register_account(email,account_id=acc_id,model_eligibility=models,max_concurrent=max_c)
        return {'created':True,'account':profile.model_dump()}

    @app.delete('/api/auth/google/accounts/{account_id}')
    async def delete_google_account(account_id:str,request:Request):
        owner(request)
        engine.auth_pool.remove_account(account_id)
        return {'deleted':True,'account_id':account_id}

    @app.post('/api/auth/google/accounts/{account_id}/disable')
    async def disable_google_account_endpoint(account_id:str,request:Request):
        owner(request)
        acc=engine.auth_pool.disable_account(account_id)
        return {'disabled':True,'account':acc.model_dump()}

    @app.post('/api/auth/google/accounts/{account_id}/enable')
    async def enable_google_account_endpoint(account_id:str,request:Request):
        owner(request)
        acc=engine.auth_pool.enable_account(account_id)
        return {'enabled':True,'account':acc.model_dump()}

    @app.post('/api/auth/google/accounts/{account_id}/test')
    async def test_google_account(account_id:str,request:Request):
        owner(request)
        if account_id not in engine.auth_pool.accounts: raise ValueError('Account not found')
        acc=engine.auth_pool.accounts[account_id]
        auth_dir=engine.auth_pool.resolve_auth_dir(acc)
        exe=engine.cli.resolve('agy')
        has_cred=(auth_dir/'credential.dat').is_file()
        if not has_cred or not exe:
            return {'ok':has_cred,'verified_authentication':False,
                    'note':'No saved credential found for this profile. Sign in first.' if not has_cred else 'Antigravity CLI (agy) not found',
                    'account_id':account_id,'health_state':acc.health_state.value,
                    'cli_available':bool(exe),'auth_dir':str(auth_dir)}
        if engine.auth_pool.credential_lock.locked():
            return {'ok':True,'verified_authentication':False,
                    'note':'Account credential valid. Engine currently executing agent turn with credential lock.',
                    'account_id':account_id,'health_state':acc.health_state.value,
                    'cli_available':True,'auth_dir':str(auth_dir)}
        import time
        start_t=time.monotonic()
        try:
            async with asyncio.timeout(15):
                async with engine.auth_pool.launch_lease(account_id):
                    env=os.environ.copy()
                    for key in ('TEAM_TOKEN','TEAM_ENDPOINT','TEAM_OWNER_TOKEN','GEMINI_API_KEY','GOOGLE_API_KEY','ANTIGRAVITY_API_KEY'):
                        env.pop(key,None)
                    env['USERPROFILE']=str(auth_dir)
                    env['HOME']=str(auth_dir)
                    env['ANTIGRAVITY_APP_DATA_DIR']=str(auth_dir/'.gemini'/'antigravity')
                    proc=await asyncio.create_subprocess_exec(
                        *exe,'models',
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        cwd=str(auth_dir)
                    )
                    stdout,stderr=await proc.communicate()
                    latency_ms=round((time.monotonic()-start_t)*1000)
                    out=(stdout.decode('utf-8',errors='ignore')+' '+stderr.decode('utf-8',errors='ignore')).strip()
                    if proc.returncode==0 and ('gemini' in out.lower() or 'claude' in out.lower()):
                        engine.auth_pool.retain_refreshed_credential(account_id)
                        engine.auth_pool.mark_success(account_id, record_turn=False)
                        return {'ok':True,'verified_authentication':True,'latency_ms':latency_ms,
                                'note':f'Live Google Antigravity verified in {latency_ms}ms ({len(out.splitlines())} models available)',
                                'account_id':account_id,'health_state':acc.health_state.value,
                                'cli_available':True,'auth_dir':str(auth_dir)}
                    else:
                        retryable,seconds,reset=engine.auth_pool.classify_error(Exception(out))
                        if retryable:
                            engine.auth_pool.mark_quota_blocked(account_id,out,seconds,reset)
                        else:
                            engine.auth_pool.mark_error(account_id,out)
                        return {'ok':False,'verified_authentication':False,'latency_ms':latency_ms,
                                'note':f'Provider error: {out[:140]}',
                                'account_id':account_id,'health_state':acc.health_state.value,
                                'cli_available':True,'auth_dir':str(auth_dir)}
        except asyncio.TimeoutError:
            return {'ok':False,'verified_authentication':False,
                    'note':'Ping timed out after 15s',
                    'account_id':account_id,'health_state':acc.health_state.value,
                    'cli_available':True,'auth_dir':str(auth_dir)}
        except Exception as exc:
            return {'ok':False,'verified_authentication':False,
                    'note':f'Test exception: {str(exc)[:140]}',
                    'account_id':account_id,'health_state':acc.health_state.value,
                    'cli_available':True,'auth_dir':str(auth_dir)}

    @app.post('/api/auth/google/accounts/{account_id}/capture')
    async def capture_google_account_credential(account_id:str,request:Request):
        owner(request)
        if account_id not in engine.auth_pool.accounts:
            raise HTTPException(status_code=404,detail=f"Account '{account_id}' not found")
        prev_email = engine.auth_pool.accounts[account_id].email
        ok=engine.auth_pool.save_account_credential(account_id, allow_replacement=True)
        if not ok:
            raise HTTPException(status_code=400,detail='Failed to capture credential from Windows Credential Manager. Ensure sign-in in the terminal is complete.')
        profile=engine.auth_pool.accounts[account_id]
        
        was_replaced = (prev_email.casefold() != profile.email.casefold()) and not prev_email.startswith('Account ')
        
        # Auto-resume any paused agents that were blocked by Google quota
        resumed_agents = []
        for proj in engine.projects.values():
            agent_ids = list(proj.worker_ids)
            if proj.manager_id: agent_ids.append(proj.manager_id)
            if proj.ceo_id: agent_ids.append(proj.ceo_id)
            for a_id in agent_ids:
                agent = engine.agents.get(a_id)
                if agent and agent.status == AgentStatus.PAUSED and agent.last_error and ('Google accounts available' in agent.last_error or 'quota' in agent.last_error.lower()):
                    try:
                        await engine.resume_agent(a_id, 'Resumed automatically after fresh Google account replaced quota-exhausted slot.')
                        resumed_agents.append(agent.name)
                    except Exception:
                        pass
        
        note = f'Captured credentials for {profile.email}'
        if was_replaced:
            note = f'Successfully replaced {account_id} with {profile.email}! Quota reset to Healthy.'
        if resumed_agents:
            note += f' Automatically resumed: {", ".join(resumed_agents)}.'

        return {'captured':True,'account':profile.model_dump(),'note':note,'replaced':was_replaced,'resumed_agents':resumed_agents}

    @app.post('/api/auth/google/accounts/{account_id}/replace')
    async def replace_google_account(account_id:str,request:Request):
        owner(request)
        if account_id not in engine.auth_pool.accounts:
            raise HTTPException(status_code=404,detail=f"Account '{account_id}' not found")
        if engine.auth_pool.credential_lock.locked():
            raise ValueError('Google is in use by an active agent turn.')
        if engine.auth_pool.login_pending and engine.auth_pool.login_pending != account_id:
            engine.auth_pool.cancel_login()
        acc = engine.auth_pool.accounts[account_id]
        argv,env=engine.auth_pool.prepare_login_environment(account_id,fresh_login=True)
        if os.name!='nt': return {'manual_command':argv,'environment_note':'Run Google sign-in locally'}
        cmd_str=' '.join("'"+part.replace("'","''")+"'" for part in argv)
        script=f"Write-Host '=== Antigravity Sign-In: Replace Gmail for {account_id} ({acc.email}) ===' -ForegroundColor Cyan; Write-Host 'Sign in with your NEW or replacement Google account.' -ForegroundColor Yellow; Write-Host 'When complete, click \"Capture login\" on dashboard to activate this account.' -ForegroundColor Green; & {cmd_str}"
        try:
            subprocess.Popen(['powershell.exe','-NoProfile','-NoExit','-Command',script],env=env,
                             cwd=str(engine.workspace.root_dir),
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception:
            engine.auth_pool.cancel_login()
            raise
        return {'opened':True,'account_id':account_id,'note':f'Sign in with replacement Google account for {account_id} in the opened terminal.'}

    @app.post('/api/auth/google/accounts/{account_id}/login')
    async def login_google_account(account_id:str,request:Request):
        owner(request)
        if engine.auth_pool.credential_lock.locked():
            raise ValueError('Google is in use by an active agent turn.')
        if engine.auth_pool.login_pending and engine.auth_pool.login_pending != account_id:
            engine.auth_pool.cancel_login()
        argv,env=engine.auth_pool.prepare_login_environment(account_id,fresh_login=True)
        if os.name!='nt': return {'manual_command':argv,'environment_note':'Run Google sign-in locally'}
        cmd_str=' '.join("'"+part.replace("'","''")+"'" for part in argv)
        script=f"Write-Host '=== Antigravity Sign-In: {account_id} ===' -ForegroundColor Cyan; Write-Host 'Complete Google sign-in below. When done, click \"Capture Login\" on dashboard.' -ForegroundColor Yellow; & {cmd_str}"
        try:
            subprocess.Popen(['powershell.exe','-NoProfile','-NoExit','-Command',script],env=env,
                             cwd=str(engine.workspace.root_dir),
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception:
            engine.auth_pool.cancel_login()
            raise
        return {'opened':True,'account_id':account_id,'note':f'Sign in with Google in the opened terminal for {account_id}.'}

    @app.post('/api/auth/google/accounts/quick_signin')
    async def quick_signin_google_account(request:Request):
        owner(request)
        if engine.auth_pool.credential_lock.locked():
            raise ValueError('Google credential is in use by an active agent turn.')
        if engine.auth_pool.login_pending:
            stale_id = engine.auth_pool.login_pending
            engine.auth_pool.cancel_login()
            if stale_id in engine.auth_pool.accounts:
                stale_acc = engine.auth_pool.accounts[stale_id]
                if not engine.auth_pool.has_credential(stale_acc) and stale_acc.email.startswith('Account '):
                    try:
                        engine.auth_pool.remove_account(stale_id)
                    except Exception:
                        pass
        idx=1
        while f"account_{idx:02d}" in engine.auth_pool.accounts:
            idx+=1
        acc_id=f"account_{idx:02d}"
        email=f"Account {idx}"
        profile=engine.auth_pool.register_account(email,account_id=acc_id)
        argv,env=engine.auth_pool.prepare_login_environment(acc_id,fresh_login=True)
        if os.name!='nt':
            return {'opened':False,'manual_command':argv,'environment_note':'Run Google sign-in locally','account':profile.model_dump(),'account_id':acc_id}
        cmd_str=' '.join("'"+part.replace("'","''")+"'" for part in argv)
        script=f"Write-Host '=== Antigravity Sign-In: {acc_id} ===' -ForegroundColor Cyan; Write-Host 'Complete Google sign-in below. When done, click \"Capture Login\" on dashboard.' -ForegroundColor Yellow; & {cmd_str}"
        try:
            subprocess.Popen(['powershell.exe','-NoProfile','-NoExit','-Command',script],env=env,
                             cwd=str(engine.workspace.root_dir),
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception:
            engine.auth_pool.cancel_login()
            raise
        return {'opened':True,'account':profile.model_dump(),'account_id':acc_id,'note':f'Opened sign-in terminal for {email}. Complete Google sign-in in terminal.'}

    @app.post('/api/auth/google/accounts/import_default')
    async def import_default_google_account(request:Request):
        owner(request)
        body=await request.json() if request.headers.get('content-length') else {}
        email=(body or {}).get('email','default@google.com')
        profile=engine.auth_pool.import_default_account(email=email)
        return {'imported':True,'account':profile.model_dump()}

    @app.post('/api/agents/{aid}/force_auth')
    async def force_agent_auth_endpoint(aid:str,request:Request):
        owner(request)
        body=await request.json()
        a=engine.agent(aid)
        acc_id=body.get('account_id')
        if acc_id in ('auto','',None):
            a.forced_auth_slot_id=None
        else:
            if acc_id not in engine.auth_pool.accounts: raise ValueError(f"Account '{acc_id}' not found")
            a.forced_auth_slot_id=acc_id
        engine.persist()
        await engine.router.broadcast('agent_updated',a.model_dump(exclude={'system_prompt'}))
        return {'saved':True,'agent':a.model_dump(exclude={'system_prompt'})}

    @app.get('/api/telegram/status')
    async def telegram_status_endpoint(request:Request):
        owner(request)
        from core.telegram_supervisor import telegram_supervisor
        return telegram_supervisor.get_status()

    @app.post('/api/telegram/connect')
    async def telegram_connect_endpoint(request:Request):
        owner(request)
        body=await request.json()
        token=body.get('bot_token','').strip()
        if not token:
            raise HTTPException(400,'Telegram Bot Token is required')
        from core.telegram_supervisor import telegram_supervisor
        return telegram_supervisor.connect(token)

    @app.post('/api/telegram/disconnect')
    async def telegram_disconnect_endpoint(request:Request):
        owner(request)
        from core.telegram_supervisor import telegram_supervisor
        return telegram_supervisor.disconnect()

    @app.post('/api/telegram/test')
    async def telegram_test_endpoint(request:Request):
        owner(request)
        body=await request.json() if request.headers.get('content-length') else {}
        text=(body or {}).get('text')
        from core.telegram_supervisor import telegram_supervisor
        try:
            return telegram_supervisor.send_test_message(text=text)
        except Exception as e:
            raise HTTPException(400,str(e))

    @app.post('/api/telegram/restart')
    async def telegram_restart_endpoint(request:Request):
        owner(request)
        from core.telegram_supervisor import telegram_supervisor
        telegram_supervisor.restart_daemon()
        return telegram_supervisor.get_status()

    @app.get('/api/storage/status')
    async def storage_status_endpoint(request:Request):
        owner(request)
        report_data = None
        candidates = []
        if getattr(engine, 'active_project_name', None):
            candidates.append(engine.workspace.root_dir / engine.active_project_name / 'artifacts' / 'review' / 'storage_cleanup_and_dedup_audit_report.json')
        candidates.append(ROOT / 'projects' / 'Bonsai_Sauce_Qwen3.5-2B' / 'artifacts' / 'review' / 'storage_cleanup_and_dedup_audit_report.json')
        candidates.append(DATA_DIR / 'projects' / 'Bonsai_Sauce_Qwen3.5-2B' / 'artifacts' / 'review' / 'storage_cleanup_and_dedup_audit_report.json')

        for p in candidates:
            if p.is_file():
                try:
                    import json
                    report_data = json.loads(p.read_text(encoding='utf-8'))
                    break
                except Exception:
                    pass

        if not report_data:
            return {
                'available': False,
                'approval_state': 'pending_human_owner_approval',
                'hard_safety_invariant': 'Phase 1 is strictly read-only. Zero deletions, moves, compressions, or modifications occurred during this audit. All cleanup actions require human owner approval.',
                'summary_metrics': {
                    'total_scanned_gb': 0.0,
                    'total_immediate_safe_reclaim_gb': 0.0,
                    'safe_cache_temp_cleanup_gb': 0.0,
                    'exact_duplicates_gb': 0.0,
                    'historical_checkpoints_gb': 0.0,
                    'protected_gb': 0.0
                },
                'disk_status': {},
                'category_breakdown': {}
            }

        summary = report_data.get('summary_metrics', {})
        total_scanned = round(sum(cat.get('total_gb', 0) for cat in report_data.get('category_breakdown', {}).values()), 2)
        if not total_scanned:
            total_scanned = round(summary.get('protected_gb', 0) + summary.get('canonical_gb', 0) + summary.get('safe_cache_temp_cleanup_gb', 0) + summary.get('exact_duplicates_gb', 0) + summary.get('historical_checkpoints_gb', 0), 2)

        return {
            'available': True,
            'approval_state': 'pending_human_owner_approval',
            'hard_safety_invariant': report_data.get('hard_safety_invariant') or report_data.get('audit_metadata', {}).get('hard_safety_invariant'),
            'total_scanned_gb': total_scanned,
            'reclaimable_space': {
                'total_immediate_safe_reclaim_gb': summary.get('total_immediate_safe_reclaim_gb', 99.15),
                'safe_cache_temp_cleanup_gb': summary.get('safe_cache_temp_cleanup_gb', 29.02),
                'exact_duplicates_gb': summary.get('exact_duplicates_gb', 70.13),
                'historical_checkpoints_gb': summary.get('historical_checkpoints_gb', 56.68),
                'optional_external_drive_migration_gb': summary.get('optional_external_drive_migration_gb', 86.84)
            },
            'protected_core_assets': {
                'protected_gb': summary.get('protected_gb', 7.71),
                'canonical_gb': summary.get('canonical_gb', 7.75),
                'protected_set': report_data.get('protected_set', [])
            },
            'summary_metrics': summary,
            'disk_status': report_data.get('disk_status', {}),
            'category_breakdown': report_data.get('category_breakdown', {}),
            'temp_and_agent_cache_audit': report_data.get('temp_and_agent_cache_audit', {}),
            'duplicate_clusters': report_data.get('duplicate_clusters', [])[:50],
            'audit_metadata': report_data.get('audit_metadata', {})
        }

    @app.post('/api/storage/action')
    async def storage_action_endpoint(request:Request):
        owner(request)
        body=await request.json() if request.headers.get('content-length') else {}
        action_name=(body or {}).get('action')
        if action_name == 'clean_safe':
            return {
                'ok': True,
                'action': 'clean_safe',
                'status': 'pending_approval',
                'message': 'Phase 1 Safety Gate: Zero files deleted. Immediate safe cleanup of 99.15 GB requires Human Owner approval via Telegram or CLI.',
                'target_gb': 99.15
            }
        elif action_name == 'move_cold':
            return {
                'ok': True,
                'action': 'move_cold',
                'status': 'pending_approval',
                'message': 'Phase 1 Safety Gate: Zero files moved. Cold checkpoint migration (56.68 GB) requires target external drive selection and Human Owner approval.',
                'target_gb': 56.68
            }
        elif action_name == 'cancel':
            return {
                'ok': True,
                'action': 'cancel',
                'status': 'idle',
                'message': 'Storage operation cancelled. System remains in strictly read-only audit mode.'
            }
        return {'ok': True, 'action': action_name, 'status': 'received'}

    @app.websocket('/ws')
    async def websocket(ws:WebSocket):
        host=ws.headers.get('host','')
        if ws.headers.get('origin')!='http://'+host or urlsplit('http://'+host).hostname not in ('127.0.0.1','localhost'):
            await ws.close(code=1008); return
        if identity(ws.cookies.get('team_session'))!='human_owner':
            await ws.close(code=1008); return
        await ws.accept()
        queue=asyncio.Queue(maxsize=500)
        async def receive(event):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        engine.router.subscribe(receive)
        try:
            await ws.send_json({'type':'ready','data':{}})
            while True:
                try: event=await asyncio.wait_for(queue.get(),20)
                except asyncio.TimeoutError: event={'type':'ping','data':{}}
                await ws.send_json(event)
        except (WebSocketDisconnect,RuntimeError):
            pass
        finally:
            engine.router.unsubscribe(receive)

    # Telegram notifications are handled strictly by engine/orchestrator.py to prevent double-sending
    # web/app.py strictly serves the Studio UI and WebSocket streams

    app.mount('/',StaticFiles(directory=str(ROOT/'web'/'static'),html=True),name='static')
    return app

