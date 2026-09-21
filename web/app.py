import asyncio
import os
import secrets
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from core.config import ROOT, SystemSettings
from core.providers import PROVIDER_PRESETS
from engine.loop_monitor import LoopMonitor


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
        yield
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
        response=await call_next(request)
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
        import sys
        exe = ROOT / '.venv' / 'Scripts' / 'python.exe'
        argv = [str(exe) if exe.is_file() else sys.executable, str(ROOT/'main.py'), '--console', a.id]
        quoted = ' '.join("'" + part.replace("'", "''") + "'" for part in argv)
        subprocess.Popen(['powershell.exe','-NoProfile','-NoExit','-Command','& '+quoted],
            cwd=str(ROOT),creationflags=subprocess.CREATE_NEW_CONSOLE)
        return {'opened':True,'mode':'Managed session; typed messages are sent as Human Owner'}

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

    @app.get('/api/tree')
    async def tree(request:Request,project:str):
        if request.state.actor!='human_owner' and engine.agent(request.state.actor).project_name!=project:
            raise HTTPException(403,'Cross-project access denied')
        return engine.get_tree(project)

    @app.post('/api/action')
    async def action(req:ActionRequest,request:Request):
        return await engine.action(req.project_name,req.action,req.arguments,request.state.actor)

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
        return {'settings':engine.config.public(),'harnesses':engine.cli.capabilities(),
                'provider_presets':PROVIDER_PRESETS}

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
        argv=engine.cli.resolve(name)+{'claude':['auth','login'],'codex':['login','--device-auth'],'agy':[]}[name]
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
                'execution_policy':'Google turns are serialized because the CLI shares one Windows credential'}

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
        return {'ok':(auth_dir/'credential.dat').is_file(),'verified_authentication':False,
                'note':'Local credential and executable check only; does not test provider quota or login',
                'account_id':account_id,'health_state':acc.health_state.value,
                'cli_available':bool(exe),'auth_dir':str(auth_dir)}

    @app.post('/api/auth/google/accounts/{account_id}/capture')
    async def capture_google_account_credential(account_id:str,request:Request):
        owner(request)
        ok=engine.auth_pool.save_account_credential(account_id)
        if not ok:
            raise HTTPException(status_code=400,detail='Failed to capture credential from Windows Credential Manager. Ensure sign-in in the terminal is complete.')
        profile=engine.auth_pool.accounts[account_id]
        return {'captured':True,'account':profile.model_dump(),'note':f'Captured credentials for {profile.email}'}

    @app.post('/api/auth/google/accounts/{account_id}/login')
    async def login_google_account(account_id:str,request:Request):
        owner(request)
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
        if engine.auth_pool.credential_lock.locked() or engine.auth_pool.login_pending:
            raise ValueError('Google is in use. Finish its active turn or sign-in first.')
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

    app.mount('/',StaticFiles(directory=str(ROOT/'web'/'static'),html=True),name='static')
    return app

