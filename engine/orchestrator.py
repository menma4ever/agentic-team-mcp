import asyncio
import hashlib
import math
import json
import random
import secrets
import uuid
import zipfile
import psutil
from pathlib import Path
from datetime import datetime, timezone
from core.config import DATA_DIR, settings
from core.workspace import WorkspaceManager, contained, safe_name
from core.prompts import CEO_SYSTEM_PROMPT, MANAGER_SYSTEM_PROMPT, WORKER_SYSTEM_PROMPT
from core.auth_pool import GoogleAuthPool
from engine.models import AgentNode, AgentStatus, HarnessType, Role, ProjectState, Message, now
from engine.store import Store
from engine.message_router import MessageRouter
from engine.actions import definitions, validate
from harness.direct_api import DirectAPIRunner


class AccountPoolPaused(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, data_dir=DATA_DIR, config=settings, runner=None, cli=None):
        self.data_dir = Path(data_dir).resolve()
        self.config = config
        self.workspace = WorkspaceManager(self.data_dir / 'projects')
        self.store = Store(self.data_dir / 'team.sqlite3')
        self.router = MessageRouter()
        self.auth_pool = GoogleAuthPool(self.data_dir, config)
        self.runner = runner or DirectAPIRunner(config)
        if cli is None:
            from harness.cli_runner import CLIRunner
            cli = CLIRunner(config)
        self.cli = cli
        self.pumps, self.active, self.locks = {}, {}, {}
        self.decisions = {}
        self.closing = False
        self.endpoint = ''
        saved = self.store.load()
        self.projects = {k: ProjectState(**v) for k,v in saved.get('projects', {}).items()}
        self.agents = {k: AgentNode(**v) for k,v in saved.get('agents', {}).items()}
        self.messages = [Message(**m) for m in saved.get('messages', [])]
        self.inboxes = saved.get('inboxes', {})
        self.contexts = saved.get('contexts', {})
        self.paused = saved.get('paused', {})
        self.inflight = saved.get('inflight', {})
        self.tokens = saved.get('tokens', {})
        for aid, msg in self.inflight.items():
            self.paused[aid] = msg
            if aid in self.agents:
                a = self.agents[aid]
                # Reconcile only a process whose PID AND birth time match our own saved run.
                # PID alone can point at an unrelated process after a restart.
                if a.pid and a.process_started_at:
                    try:
                        proc = psutil.Process(a.pid)
                        if abs(proc.create_time() - a.process_started_at) < .01:
                            family = proc.children(recursive=True) + [proc]
                            for member in family:
                                try: member.kill()
                                except psutil.NoSuchProcess: pass
                            psutil.wait_procs(family, timeout=5)
                    except psutil.NoSuchProcess:
                        pass
                a.status = AgentStatus.PAUSED
                a.pid = None
                a.last_error = 'Engine stopped during execution. Inspect artifacts, then Resume.'
        self.inflight = {}
        for account in self.auth_pool.accounts.values(): account.active_agents = 0
        self.auth_pool.save()
        for aid, context in self.contexts.items():
            # Interrupted tool calls have an unknown outcome; never replay them implicitly.
            pending = {}
            for msg in context:
                for call in msg.get('tool_calls', []):
                    pending[call['id']] = call
                if msg.get('role') == 'tool':
                    pending.pop(msg.get('tool_call_id'), None)
            for tid in pending:
                context.append({'role':'tool','tool_call_id':tid,'content':
                    '{"error":"Engine interrupted; outcome unknown. Inspect files before retrying."}'})
        self.persist()

    @property
    def active_project_name(self):
        return next(iter(self.projects), None)

    def persist(self):
        self.store.save({'projects':{k:v.model_dump() for k,v in self.projects.items()},
                         'agents':{k:v.model_dump() for k,v in self.agents.items()},
                         'messages':[m.model_dump() for m in self.messages],
                         'inboxes':self.inboxes,'contexts':self.contexts,'paused':self.paused,
                         'inflight':self.inflight,'tokens':self.tokens})

    async def start(self):
        for aid, queue in self.inboxes.items():
            if queue and aid not in self.paused:
                self.schedule(aid)

    async def close(self):
        self.closing = True
        for task in list(self.active.values()):
            task.cancel()
        await asyncio.gather(*list(self.pumps.values()), return_exceptions=True)
        self.persist()
        self.store.close()

    def agent(self, aid):
        if aid not in self.agents:
            raise ValueError('Agent not found')
        return self.agents[aid]

    def get_ceo(self, project):
        return self.agents.get(self.projects[project].ceo_id)

    def get_manager(self, project):
        return self.agents.get(self.projects[project].manager_id)

    def get_tree(self, project):
        p = self.projects.get(project)
        if not p:
            raise ValueError('Project not found')
        def public(a):
            if not a: return None
            return a.model_dump(exclude={'system_prompt'})
        return {'project_name':project,'project':p.model_dump(),
                'ceo':public(self.get_ceo(project)), 'manager':public(self.get_manager(project)),
                'workers':[public(a) for a in self.agents.values() if a.project_name == project
                    and a.role == Role.WORKER and a.status != AgentStatus.TERMINATED]}

    def token_for(self, aid):
        for token, identity in self.tokens.items():
            if identity == aid: return token
        token = secrets.token_urlsafe(32)
        self.tokens[token] = aid
        self.persist()
        return token

    def identify(self, token):
        return self.tokens.get(token)

    def _add_agent(self, project, name, role, model, harness='direct_api',
                   task_description='', thinking_budget=0, reasoning_effort=None, role_title='', parent_id=None):
        safe_name(name)
        if not model.strip():
            raise ValueError('Model is required')
        if any(a.name.casefold() == name.casefold() and a.project_name == project
               and a.status != AgentStatus.TERMINATED for a in self.agents.values()):
            raise ValueError('Agent name already exists')
        h = HarnessType(harness)
        if h != HarnessType.DIRECT_API:
            if not self.projects[project].allow_commands:
                raise PermissionError('Owner must enable local command execution for CLI agents')
            self.cli.validate(h, model, thinking_budget, reasoning_effort)
        w = self.workspace
        folder = w.create_worker_dir(project, name) if role == Role.WORKER else w.get_project_dir(project) / role.value.lower()
        folder.mkdir(exist_ok=True)
        prompts = {Role.CEO:CEO_SYSTEM_PROMPT,Role.MANAGER:MANAGER_SYSTEM_PROMPT,Role.WORKER:WORKER_SYSTEM_PROMPT}
        a = AgentNode(project_name=project, name=name, role=role, model=model, harness=h,
                      thinking_budget=thinking_budget, reasoning_effort=reasoning_effort,
                      working_dir=str(folder), current_task=task_description or 'Awaiting task',
                      parent_id=parent_id, system_prompt=prompts[role] + ('\nSpecialty: '+role_title if role_title else ''),
                      hat={Role.CEO:'crown',Role.MANAGER:'cap',Role.WORKER:'hardhat'}[role],
                      avatar_logo=model.split('/')[0])
        self.agents[a.id] = a
        self.inboxes[a.id] = []
        self.contexts[a.id] = []
        self._write_status(a)
        return a

    async def create_project(self, name, description='', ceo_name='CEO', ceo_model=None,
                             thinking_budget=0, reasoning_effort=None, harness='direct_api',
                             allow_commands=False, read_roots=None):
        if any(k.casefold() == name.casefold() for k in self.projects):
            raise ValueError('Project already exists')
        safe_name(name)
        safe_name(ceo_name)
        roots = [str(Path(p).resolve(strict=True)) for p in (read_roots or [])]
        if harness != 'direct_api':
            if not allow_commands: raise PermissionError('Enable local command execution for CLI agents')
            self.cli.validate(HarnessType(harness), ceo_model or self.config.default_ceo_model, thinking_budget, reasoning_effort)
        self.workspace.create_project(name, description)
        p = ProjectState(name=name,description=description,allow_commands=allow_commands,read_roots=roots)
        self.projects[name] = p
        ceo = self._add_agent(name,ceo_name,Role.CEO,ceo_model or self.config.default_ceo_model,
                              harness,thinking_budget=thinking_budget,reasoning_effort=reasoning_effort)
        p.ceo_id = ceo.id
        self.persist()
        await self.router.broadcast('project_created', p.model_dump())
        return p

    async def create_manager(self, project, **spec):
        p = self.projects[project]
        if p.manager_id:
            raise ValueError('Manager already exists; send it a message')
        a = self._add_agent(project,role=Role.MANAGER,parent_id=p.ceo_id,**spec)
        p.manager_id = a.id
        p.status = 'active'
        self.persist()
        await self.send(project,a.id,a.current_task,sender_id=p.ceo_id,kind='task')
        return a

    async def spawn_worker(self, project, **spec):
        p = self.projects[project]
        if not p.manager_id:
            raise ValueError('Create a manager before spawning workers')
        live = [a for a in self.agents.values() if a.project_name == project and
                a.role == Role.WORKER and a.status != AgentStatus.TERMINATED]
        if len(live) >= self.config.max_workers:
            raise ValueError('Worker limit reached')
        a = self._add_agent(project,role=Role.WORKER,parent_id=p.manager_id,**spec)
        p.worker_ids.append(a.id)
        self.persist()
        await self.send(project,a.id,a.current_task,sender_id=p.manager_id,kind='task')
        await self.router.broadcast('worker_spawned', a.model_dump(exclude={'system_prompt'}))
        return a

    def schedule(self, aid):
        if not self.closing and (aid not in self.pumps or self.pumps[aid].done()):
            self.pumps[aid] = asyncio.create_task(self._pump(aid))

    async def send(self, project, target_id, content, sender_id='human_owner', is_interrupt=False, kind='message'):
        target = self.agent(target_id)
        if target.project_name != project or target.status == AgentStatus.TERMINATED:
            raise ValueError('Target is not an active member of this project')
        sender = self.agent(sender_id) if sender_id not in ('human_owner','system') else None
        if sender and sender.project_name != project:
            raise PermissionError('Cross-project messaging denied')
        if target_id == sender_id:
            raise ValueError('Use wait_for_workers or finish this turn; self messaging is disabled')
        if is_interrupt and sender and {Role.WORKER:0,Role.MANAGER:1,Role.CEO:2}[sender.role] <= {Role.WORKER:0,Role.MANAGER:1,Role.CEO:2}[target.role]:
            raise PermissionError('Only owner or a supervisor can interrupt this agent')
        lock = self.locks.setdefault(target_id, asyncio.Lock())
        async with lock:
            msg = Message(project_name=project,sender_id=sender_id,
                sender_name=sender.name if sender else ('Owner' if sender_id == 'human_owner' else 'Engine'),
                sender_role=sender.role.value if sender else ('HUMAN' if sender_id == 'human_owner' else 'SYSTEM'),
                recipient_id=target.id,recipient_name=target.name,content=content,
                is_interrupt=is_interrupt,kind=kind)
            self.messages.append(msg)
            queue = self.inboxes[target.id]
            # Queue before cancellation so the pump sees the steering message.
            if is_interrupt: queue.insert(0,msg.model_dump())
            else: queue.append(msg.model_dump())
            if sender_id == 'human_owner':
                target.autonomous_turns = 0
            self.persist()
            active = self.active.get(target.id)
            if is_interrupt and active and not active.done():
                active.cancel()
                await asyncio.gather(active,return_exceptions=True)
            if target.id not in self.active:
                target.status = AgentStatus.QUEUED
            self.persist()
            self.schedule(target.id)
        await self.router.broadcast('new_message',msg.model_dump())
        return msg

    async def send_user_message(self, project_name, target_agent_id, content, is_interrupt=False):
        return await self.send(project_name,target_agent_id,content,is_interrupt=is_interrupt)

    def _write_status(self,a,details=''):
        p = Path(a.working_dir)
        if p.is_dir():
            root = self.workspace.get_project_dir(a.project_name)
            if not p.resolve().is_relative_to(root) or not (p/'status.md').resolve().is_relative_to(p.resolve()):
                a.last_error = 'Status path left the assigned workspace'
                return
            text = f'# {a.name}\nRole: {a.role.value}\nTask: {a.current_task}\nState: {a.status.value}\n'
            text += f'Last activity: {a.last_heartbeat}\nRun: {a.run_id or "none"}\nPID: {a.pid or "none"}\n'
            text += f'Error: {a.last_error or "none"}\n\n{details}\n'
            (p/'status.md').write_text(text,encoding='utf-8')

    async def emit(self,a,kind,data):
        a.update_heartbeat()
        if kind == 'process_started':
            a.pid = data['pid']
            a.process_started_at = data.get('created_at')
        safe = json.loads(self.config.redact(json.dumps(data,default=str)))
        event = {'type':kind,'data':safe,'timestamp':now(),'run_id':a.run_id,'model':a.model,'harness':a.harness.value}
        eid = self.store.event(a.id,event)
        self.persist()
        await self.router.broadcast('agent_event',{'agent_id':a.id,'id':eid,**event})

    async def _parent_notice(self,a,text,kind='report'):
        if a.parent_id:
            await self.send(a.project_name,a.parent_id,text,sender_id=a.id,kind=kind)
        else:
            m = Message(project_name=a.project_name,sender_id=a.id,sender_name=a.name,
                        sender_role=a.role.value,recipient_id='human_owner',recipient_name='Owner',
                        content=text,kind=kind)
            self.messages.append(m)
            self.persist()
            await self.router.broadcast('new_message',m.model_dump())

    async def _pump(self,aid):
        a = self.agent(aid)
        while self.inboxes[aid] and not self.closing and a.status != AgentStatus.TERMINATED:
            if a.pending_configuration:
                await self._apply_configuration(a, a.pending_configuration)
            raw = self.inboxes[aid].pop(0)
            self.inflight[aid] = raw
            msg = Message(**raw)
            for stored in self.messages:
                if stored.id == msg.id:
                    stored.read = True
            a.status = AgentStatus.WORKING
            a.run_id = uuid.uuid4().hex
            a.last_error = None
            a.update_heartbeat()
            a.autonomous_turns += 1
            self.decisions.pop(aid,None)
            self.persist()
            self._write_status(a)
            await self.router.broadcast('agent_updated',a.model_dump(exclude={'system_prompt'}))
            child = asyncio.create_task(self._turn(a,msg))
            self.active[aid] = child
            try:
                result = await child
                a.handoff_pending = None
                m = Message(project_name=a.project_name,sender_id=aid,sender_name=a.name,
                    sender_role=a.role.value,recipient_id=msg.sender_id if msg.sender_id == 'human_owner' else 'human_owner',
                    recipient_name='Owner',content=result or 'Turn finished.',kind='reply')
                self.messages.append(m)
                await self.router.broadcast('new_message',m.model_dump())
                decision = self.decisions.get(aid)
                if a.role == Role.WORKER and msg.kind in ('task','resume') and decision != 'reported':
                    a.last_error = 'Worker ended without report_result; completion is unverified.'
                    a.status = AgentStatus.FAILED
                    await self._parent_notice(a,f'{a.last_error}\nWorker response: {result}')
                elif decision == 'escalated':
                    a.status = AgentStatus.PAUSED
                else:
                    a.status = AgentStatus.PAUSED if aid in self.paused else AgentStatus.IDLE
                if a.role == Role.MANAGER and not decision and self.projects[a.project_name].status == 'active':
                    busy = any(x.parent_id == aid and (self.inboxes[x.id] or x.id in self.active)
                               for x in self.agents.values() if x.status != AgentStatus.TERMINATED)
                    if not busy:
                        await self.send(a.project_name,aid,
                            'The goal is still active. Inspect results, delegate next work, finish_project, or escalate.',
                            sender_id='system',kind='continue')
            except asyncio.CancelledError:
                if a.status != AgentStatus.TERMINATED:
                    self.paused[aid] = raw
                    a.status = AgentStatus.PAUSED
                    a.last_error = 'Turn interrupted. Resume inspects existing work before continuing.'
                await self.emit(a,'interrupted',{'message_id':msg.id})
                if a.pending_configuration: break
            except AccountPoolPaused as exc:
                self.paused[aid] = raw
                a.status = AgentStatus.PAUSED
                a.last_error = str(exc)
                await self.emit(a,'auth_blocked',{'error':a.last_error})
                await self._parent_notice(a, f'{a.name} paused: {a.last_error}', kind='escalation')
                break
            except Exception as exc:
                a.status = AgentStatus.FAILED
                a.last_error = self.config.redact(exc)
                await self.emit(a,'error',{'error':a.last_error})
                await self._parent_notice(a,f'Execution failed for {a.name}: {a.last_error}',kind='escalation')
            finally:
                self.active.pop(aid,None)
                self.inflight.pop(aid,None)
                if a.pending_configuration:
                    await self._apply_configuration(a, a.pending_configuration)
                a.pid = None
                a.process_started_at = None
                self._write_status(a)
                self.persist()
                await self.router.broadcast('agent_updated',a.model_dump(exclude={'system_prompt'}))

    async def _turn(self,a,msg):
        if a.autonomous_turns > self.config.max_autonomous_turns:
            raise RuntimeError('Autonomous turn limit reached. Owner/supervisor must review before continuing.')
        async def emit(kind,data):
            await self.emit(a,kind,data)
        async def execute(name,args):
            return await self.action(a.project_name,name,args,a.id)
        if a.handoff_pending:
            msg = msg.model_copy(update={'content': a.handoff_pending + '\n\n' + msg.content})
        if a.harness == HarnessType.DIRECT_API:
            context = self.contexts[a.id]
            if not context:
                context.append({'role':'system','content':a.system_prompt + '\nProject: '+self.projects[a.project_name].description})
            # Close unknown tool outcomes after cancellation without replaying any action.
            pending = {c['id'] for m in context for c in m.get('tool_calls',[])}
            answered = {m.get('tool_call_id') for m in context if m['role'] == 'tool'}
            for tid in pending-answered:
                context.append({'role':'tool','tool_call_id':tid,'content':'{"error":"Interrupted; inspect artifacts before retrying."}'})
            context.append({'role':'user','content':msg.formatted_text()})
            self.persist()
            return await self.runner.generate_response(a,context,definitions(a.role.value),execute,emit)

        if a.harness == HarnessType.ANTIGRAVITY:
            return await self._google_turn(a, msg, emit)

        return await self.cli.execute_task(a,msg.formatted_text(),Path(a.working_dir),emit,
                    endpoint=self.endpoint,token=self.token_for(a.id),
                    allow_commands=self.projects[a.project_name].allow_commands)

    def file_path(self,a,path,write=False):
        root = self.workspace.get_project_dir(a.project_name)
        value = Path(path)
        if value.is_absolute():
            if write:
                raise PermissionError('Writes require project-relative paths')
            resolved = value.resolve()
            if not any(resolved.is_relative_to(Path(p).resolve()) for p in self.projects[a.project_name].read_roots):
                raise PermissionError('Path is outside owner-granted read roots')
            return resolved
        target = contained(root,path)
        if write or a.role == Role.WORKER:
            allowed = [Path(a.working_dir).resolve(),(root/'shared').resolve(),(root/'artifacts').resolve()]
            if not any(target.is_relative_to(p) for p in allowed):
                raise PermissionError('Use own workspace, shared/ or artifacts/')
        return target

    def evidence(self,a,paths):
        result=[]
        for name in paths:
            p=self.file_path(a,name)
            if not p.is_file(): raise ValueError(f'Artifact does not exist: {name}')
            result.append({'path':name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
        return result

    async def action(self,project,name,args,actor_id='human_owner'):
        if project not in self.projects: raise ValueError('Project not found')
        actor = None if actor_id == 'human_owner' else self.agent(actor_id)
        if actor and actor.project_name != project: raise PermissionError('Cross-project action denied')
        if actor and actor.status == AgentStatus.TERMINATED: raise PermissionError('Agent terminated')
        role = actor.role.value if actor else 'HUMAN'
        validate(role,name,args)
        p = self.projects[project]
        if name == 'get_team_tree': return self.get_tree(project)
        if name == 'list_capabilities':
            return {'providers':{k:v.model_dump() for k,v in self.config.providers.items()},
                    'configured_key_providers':[k for k,v in self.config.api_keys.items() if v],
                    'harnesses':self.cli.capabilities(),'command_execution':p.allow_commands,
                    'google_auth_pool':self.auth_pool.public(include_identity=actor is None)}
        if name in ('create_manager','spawn_worker'):
            a = await (self.create_manager(project,**args) if name == 'create_manager' else self.spawn_worker(project,**args))
            return {'agent_id':a.id,'state':a.status.value,'working_dir':a.working_dir}
        if name == 'send_team_message':
            m = await self.send(project,args['target_agent_id'],args['message'],sender_id=actor_id,
                                is_interrupt=args.get('is_interrupt',False),kind='task' if actor and actor.role != Role.WORKER else 'message')
            return {'message_id':m.id,'queued':True}
        if name == 'read_agent_status':
            a=self.agent(args['agent_id'])
            if a.project_name != project: raise PermissionError('Cross-project read denied')
            path=Path(a.working_dir)/'status.md'
            if not path.resolve().is_relative_to(self.workspace.get_project_dir(project)):
                raise PermissionError('Status path left the project')
            return {'content':path.read_text(encoding='utf-8') if path.exists() else 'No report'}
        if name == 'resume_agent':
            a=self.agent(args['target_agent_id'])
            if a.project_name != project: raise PermissionError('Cross-project resume denied')
            if actor:
                rank = {Role.CEO: 2, Role.MANAGER: 1, Role.WORKER: 0}
                if rank[actor.role] <= rank[a.role]:
                    raise PermissionError('Only owner or a supervisor can resume this agent')
            if a.status == AgentStatus.TERMINATED: raise ValueError('Agent is terminated')
            raw=self.paused.pop(a.id,None)
            if raw is None:
                if a.status not in (AgentStatus.FAILED,AgentStatus.BLOCKED_LOOP): raise ValueError('No paused/failed assignment')
                text=a.current_task
            else: text=raw['content']
            a.autonomous_turns=0
            await self.send(project,a.id,'Inspect existing files; continue without repeating completed side effects.\n'+text,
                            sender_id=actor_id,kind='resume')
            return {'resumed':True}
        if name == 'terminate_worker':
            return await self.terminate_worker(project,**args)
        # Owner may inspect project files through the CEO's scope.
        a=actor or self.get_ceo(project)
        if name in ('read_file','write_file','list_files'):
            path=self.file_path(a,args['path'],write=name=='write_file')
            if name=='list_files':
                if not path.is_dir(): raise ValueError('Not a directory')
                return {'files':[{'name':x.name,'directory':x.is_dir()} for x in list(path.iterdir())[:500]]}
            if name=='read_file':
                if not path.is_file(): raise ValueError(f'File not found: {args["path"]}')
                if path.stat().st_size>2_000_000: raise ValueError('File too large; split or inspect with a worker command')
                return {'content':path.read_text(encoding='utf-8')}
            if len(args['content'].encode())>2_000_000: raise ValueError('File too large')
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(args['content'],encoding='utf-8')
            return {'path':args['path'],'bytes':path.stat().st_size}
        if name=='run_command':
            if not p.allow_commands: raise PermissionError('Owner has not enabled local command execution')
            if a.harness != HarnessType.DIRECT_API:
                raise ValueError('Use your CLI native command tool; nested engine commands are disabled')
            return await self.cli.run_command(a,args['argv'],args.get('timeout_seconds',120),
                                             lambda k,d:self.emit(a,k,d))
        if name=='update_status':
            a.current_task=args['stage']; a.update_heartbeat()
            self._write_status(a,args.get('details','')); self.persist()
            return {'updated':True}
        if name=='report_result':
            artifacts=self.evidence(a,args['artifacts'])
            a.last_result={**args,'artifacts':artifacts,'timestamp':now(),'run_id':a.run_id,'model':a.model,'harness':a.harness.value}
            if args['outcome'] == 'completed':
                self.paused.pop(a.id,None)
            else:
                self.paused[a.id] = {'content': a.current_task}
            self.decisions[a.id]='reported'
            self.persist()
            await self._parent_notice(a,json.dumps(a.last_result,ensure_ascii=False))
            return {'turn_complete':True,'summary':args['summary']}
        if name=='wait_for_workers':
            outstanding=[x.id for x in self.agents.values() if x.project_name==project and x.role==Role.WORKER and
                         (x.id in self.active or self.inboxes[x.id])]
            if not outstanding: raise ValueError('No outstanding workers; take the next action, finish_project or escalate')
            self.decisions[a.id]='waiting'
            return {'turn_complete':True,'summary':'Waiting for worker results.','workers':outstanding}
        if name=='escalate':
            self.decisions[a.id]='escalated'
            self.paused[a.id]={'content':a.current_task}
            await self._parent_notice(a,args['issue_summary'],kind='escalation')
            self.persist()
            return {'turn_complete':True,'summary':'Escalated: '+args['issue_summary']}
        if name=='finish_project':
            outstanding=[x.id for x in self.agents.values() if x.project_name==project and x.role==Role.WORKER and
                         (self.inboxes[x.id] or (x.id in self.active and self.decisions.get(x.id)!='reported'))]
            if outstanding: raise ValueError('Workers still executing')
            p.completion={'summary':args['summary'],'artifacts':self.evidence(a,args['artifacts']),
                          'reviewed_by':a.id,'timestamp':now()}
            p.status='completed'
            self.decisions[a.id]='completed'
            self.persist()
            return {'turn_complete':True,'summary':args['summary']}
        if name=='reconfigure_agent':
            return await self.reconfigure_agent(project, actor_id=actor_id, **args)
        if name=='connect_agents':
            return await self.connect_agents(project,args['source_id'],args['target_id'])
        if name=='disconnect_agents':
            return await self.disconnect_agents(project,args['source_id'],args['target_id'])
        if name=='list_google_accounts':
            return {'accounts':self.auth_pool.public(include_identity=actor is None)}
        if name=='force_agent_auth':
            target=self.agent(args['target_agent_id'])
            if target.project_name!=project: raise PermissionError('Cross-project auth update denied')
            if target.status == AgentStatus.TERMINATED: raise ValueError('Agent is terminated')
            if actor:
                rank = {Role.CEO: 2, Role.MANAGER: 1, Role.WORKER: 0}
                if actor.id != target.id and rank[actor.role] <= rank[target.role]:
                    raise PermissionError('Only owner or a supervisor can change this agent account')
            acc_id=args.get('account_id')
            if acc_id in ('auto','',None):
                target.forced_auth_slot_id=None
            else:
                if acc_id not in self.auth_pool.accounts: raise ValueError(f"Account '{acc_id}' not found in Google Auth Pool")
                target.forced_auth_slot_id=acc_id
                # auth_slot_id belongs to the executing lease. Apply this
                # preference at the next acquisition, never relabel a live turn.
            self.persist()
            await self.router.broadcast('agent_updated',target.model_dump(exclude={'system_prompt'}))
            return {'updated':True,'agent_id':target.id,'forced_auth_slot_id':target.forced_auth_slot_id}
        raise ValueError('Action is not implemented')

    async def terminate_worker(self,project,worker_id,cleanup_folder=True):
        a=self.agent(worker_id)
        if a.project_name!=project or a.role!=Role.WORKER: raise ValueError('Worker not found')
        a.status=AgentStatus.TERMINATED
        self.inboxes[a.id]=[]
        active=self.active.get(a.id)
        if active:
            active.cancel()
            await asyncio.gather(active,return_exceptions=True)
        pump=self.pumps.get(a.id)
        if pump and pump is not asyncio.current_task():
            await asyncio.gather(pump,return_exceptions=True)
        # Reassert terminal state after the cancelled turn's cleanup.
        a.status=AgentStatus.TERMINATED
        self.paused.pop(a.id,None)
        self.tokens={k:v for k,v in self.tokens.items() if v!=a.id}
        archive=None
        deleted=False
        if cleanup_folder:
            root=self.workspace.get_project_dir(project)
            folder=contained(root,Path('workers')/safe_name(a.name))
            if folder.resolve()!=Path(a.working_dir).resolve(): raise ValueError('Worker folder identity mismatch')
            if folder.exists():
                dest=contained(root,'artifacts/archives')
                dest.mkdir(exist_ok=True)
                archive=dest/(a.id+'-'+uuid.uuid4().hex[:8]+'.zip')
                with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
                    for f in folder.rglob('*'):
                        if f.is_file() and f.resolve().is_relative_to(folder.resolve()):
                            z.write(f,str(f.relative_to(folder)))
                deleted=self.workspace.delete_worker_dir(project,a.name)
        self.persist()
        await self.router.broadcast('worker_terminated',{'worker_id':a.id,'folder_deleted':deleted})
        return {'stopped':True,'folder_deleted':deleted,'archive':str(archive) if archive else None}

    async def connect_agents(self, project, source_id, target_id):
        src = self.agent(source_id)
        dst = self.agent(target_id)
        if src.project_name != project or dst.project_name != project:
            raise PermissionError('Cross-project connection denied')
        if src.status == AgentStatus.TERMINATED or dst.status == AgentStatus.TERMINATED:
            raise ValueError('Cannot connect terminated agents')
        if source_id == target_id:
            raise ValueError('Cannot connect an agent to itself')
        if target_id not in src.connections:
            src.connections.append(target_id)
        self.persist()
        await self.router.broadcast('agents_connected', {'project_name': project, 'source_id': source_id, 'target_id': target_id})
        return {'connected': True, 'source_id': source_id, 'target_id': target_id}

    async def disconnect_agents(self, project, source_id, target_id):
        src = self.agent(source_id)
        dst = self.agent(target_id)
        if src.project_name != project or dst.project_name != project:
            raise PermissionError('Cross-project disconnect denied')
        if target_id in src.connections:
            src.connections.remove(target_id)
        if source_id in dst.connections:
            dst.connections.remove(source_id)
        self.persist()
        await self.router.broadcast('agents_disconnected', {'project_name': project, 'source_id': source_id, 'target_id': target_id})
        return {'disconnected': True, 'source_id': source_id, 'target_id': target_id}

    async def set_agent_position(self, project, agent_id, x, y):
        a = self.agent(agent_id)
        if a.project_name != project:
            raise PermissionError('Cross-project position update denied')
        if x is None and y is None:
            a.position = None
        else:
            if not all(math.isfinite(float(v)) and 0 <= float(v) <= 20000 for v in (x, y)):
                raise ValueError('Canvas coordinates must be finite and between 0 and 20000')
            a.position = {'x': float(x), 'y': float(y)}
        self.persist()
        await self.router.broadcast('agent_positioned', {'project_name': project, 'agent_id': agent_id, 'position': a.position})
        return {'saved': True, 'position': a.position}


    async def _google_turn(self, a, msg, emit):
        used = set()
        # Reserve the global credential for the complete CLI turn, including retries.
        a.status = AgentStatus.QUEUED
        await self.emit(a, 'auth_waiting', {'reason':'Waiting for the shared Google credential'})
        async with self.auth_pool.credential_lease():
            a.status = AgentStatus.WORKING
            await self.emit(a, 'auth_ready', {})
            while True:
                slot = None
                if self.auth_pool.accounts:
                    try:
                        slot = self.auth_pool.acquire_slot(a, exclude=list(used))
                    except (ValueError, RuntimeError) as exc:
                        raise AccountPoolPaused(str(exc)) from exc
                try:
                    if slot:
                        used.add(slot.account_id)
                        if not self.auth_pool.activate_account_credential(slot.account_id):
                            raise RuntimeError('Saved Google credential could not be activated; capture login again')
                        # The native conversation remains in the same HOME on failover.
                        # Account selection changes only the credential, never the session directory.
                        if not a.google_session_dir:
                            old = self.auth_pool.accounts.get(a.auth_slot_id)
                            a.google_session_dir = '@default' if a.session_id and not old else str(self.auth_pool.resolve_auth_dir(old or slot))
                        a.auth_slot_id = slot.account_id
                    self.persist()
                    auth_dir = Path(a.google_session_dir) if a.google_session_dir and a.google_session_dir != '@default' else None
                    result = await self.cli.execute_task(a, msg.formatted_text(), Path(a.working_dir), emit,
                        endpoint=self.endpoint, token=self.token_for(a.id),
                        allow_commands=self.projects[a.project_name].allow_commands, auth_dir=auth_dir)
                    if slot: self.auth_pool.mark_success(slot.account_id)
                    a.handoff_pending = None
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    retryable, seconds, reset = self.auth_pool.classify_error(exc)
                    if not slot or not retryable:
                        if slot: self.auth_pool.mark_error(slot.account_id, str(exc))
                        raise
                    self.auth_pool.mark_quota_blocked(slot.account_id, str(exc), seconds, reset)
                    await self.emit(a, 'auth_failover', {'previous_account': slot.account_id,
                        'cooldown_seconds': seconds, 'reset_time': reset,
                        'session_id': a.session_id, 'session_directory_retained': True})
                    # Re-enter the saved native conversation; never blindly replay tool operations.
                    msg = msg.model_copy(update={'content': 'Continue after an account quota interruption. '
                        'Inspect your latest tool results and artifacts before retrying any operation.\n' + msg.content})
                finally:
                    if slot:
                        try: self.auth_pool.retain_refreshed_credential(slot.account_id)
                        finally: self.auth_pool.release_slot(slot.account_id)

    async def reconfigure_agent(self, project, target_agent_id, model, harness,
                                mode='after_turn', actor_id='human_owner'):
        a = self.agent(target_agent_id)
        if a.project_name != project: raise PermissionError('Cross-project change denied')
        if a.status == AgentStatus.TERMINATED: raise ValueError('Agent is terminated')
        if actor_id != 'human_owner':
            actor = self.agent(actor_id)
            rank = {Role.CEO: 2, Role.MANAGER: 1, Role.WORKER: 0}
            if a.id != actor.id and rank[actor.role] <= rank[a.role]:
                raise PermissionError('Only owner or a supervisor can change this agent')
            if a.id == actor.id and mode == 'interrupt':
                raise ValueError('Self changes must wait for the current turn')
        if mode not in ('after_turn', 'interrupt'): raise ValueError('Invalid change mode')
        h = HarnessType(harness)
        if not model.strip(): raise ValueError('Model is required')
        if h != HarnessType.DIRECT_API:
            if not self.projects[project].allow_commands: raise PermissionError('CLI commands are disabled')
            self.cli.validate(h, model, 0, None)
        spec = {'model': model.strip(), 'harness': h.value}
        lock = self.locks.setdefault(a.id, asyncio.Lock())
        async with lock:
            a.pending_configuration = spec
            self.persist()
            task = self.active.get(a.id)
            busy = task and not task.done()
            if busy and mode == 'interrupt':
                task.cancel()
                pump = self.pumps.get(a.id)
                if pump: await asyncio.shield(pump)
            elif not busy:
                await self._apply_configuration(a, spec)
        if busy and mode == 'interrupt':
            await self.action(project, 'resume_agent', {'target_agent_id': a.id}, actor_id)
        return {'agent_id': a.id, 'pending': bool(a.pending_configuration),
                'model': model, 'harness': h.value, 'handoff': 'Durable history and workspace retained; native sessions are archived'}

    async def _apply_configuration(self, a, spec):
        if (a.model, a.harness.value) == (spec['model'], spec['harness']):
            a.pending_configuration = None
            self.persist()
            return
        stamp = uuid.uuid4().hex
        folder = Path(a.working_dir) / '.handoffs'
        folder.mkdir(exist_ok=True)
        archive = folder / (stamp + '.json')
        recent_events = self.store.db.execute('SELECT payload FROM events WHERE agent_id=? ORDER BY id DESC LIMIT 100', (a.id,)).fetchall()
        record = {'agent': a.model_dump(), 'messages': [m.model_dump() for m in self.messages
                  if a.id in (m.sender_id, m.recipient_id)], 'context': self.contexts.get(a.id, []),
                  'recent_events': [json.loads(r[0]) for r in reversed(recent_events)],
                  'inbox': self.inboxes.get(a.id, []), 'paused': self.paused.get(a.id)}
        archive.write_text(self.config.redact(json.dumps(record, ensure_ascii=False, indent=2)), encoding='utf-8')
        old = {'model': a.model, 'harness': a.harness.value, 'session_id': a.session_id,
               'archive': str(archive), 'timestamp': now()}
        a.session_history.append(old)
        recent = '\n'.join(m.formatted_text() for m in self.messages
            if a.id in (m.sender_id, m.recipient_id))[-16000:]
        a.handoff_pending = ('Runtime changed. You are the SAME logical agent with the SAME files, role and team. '
            'Do not restart completed work. Inspect unknown tool outcomes before repeating operations. '
            'Prior native session is archived; hidden reasoning is not transferable.\n'
            f'Full prior conversation, context, latest events and queue: {archive}\n'
            f'Current assignment: {a.current_task}\nLast result: {a.last_result}\nRecent conversation:\n{recent}')
        a.model, a.harness = spec['model'], HarnessType(spec['harness'])
        a.session_id = None
        a.google_session_dir = None
        a.auth_slot_id = None
        a.reasoning_effort = None
        a.thinking_budget = 0
        self.contexts[a.id] = []
        a.pending_configuration = None
        self.persist()
        await self.emit(a, 'configuration_changed', {'previous': old, 'model': a.model, 'harness': a.harness.value})
        await self.router.broadcast('agent_updated', a.model_dump(exclude={'system_prompt'}))
