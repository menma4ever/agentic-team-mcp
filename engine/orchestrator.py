import asyncio
import hashlib
import logging
import math
import json
import random
import secrets
import uuid
import zipfile
import psutil
from typing import Optional, List, Dict, Any, Set, Tuple
from pathlib import Path
from datetime import datetime, timezone
from core.config import DATA_DIR, settings
from core.workspace import WorkspaceManager, contained, safe_name
from core.prompts import CEO_SYSTEM_PROMPT, MANAGER_SYSTEM_PROMPT, WORKER_SYSTEM_PROMPT
from core.auth_pool import GoogleAuthPool, GoogleAccountHealth
from engine.models import AgentNode, AgentStatus, HarnessType, Role, ProjectState, Message, now
from engine.store import Store
from engine.message_router import MessageRouter
from engine.actions import definitions, validate
from harness.direct_api import DirectAPIRunner

logger = logging.getLogger(__name__)


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
        self._quota_watchdog_task = None
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
        if getattr(self, '_active_project_name', None) in self.projects:
            return self._active_project_name
        for name, p in reversed(list(self.projects.items())):
            if getattr(p, 'status', 'active') == 'active':
                return name
        return next(iter(reversed(list(self.projects))), None)

    @active_project_name.setter
    def active_project_name(self, value):
        if value in self.projects:
            self._active_project_name = value

    def persist(self):
        self.store.save({'projects':{k:v.model_dump() for k,v in self.projects.items()},
                         'agents':{k:v.model_dump() for k,v in self.agents.items()},
                         'messages':[m.model_dump() for m in self.messages],
                         'inboxes':self.inboxes,'contexts':self.contexts,'paused':self.paused,
                         'inflight':self.inflight,'tokens':self.tokens})

    async def start(self):
        # Auto-resume agents whose turns were paused solely due to an engine restart
        restart_paused = []
        for aid, a in list(self.agents.items()):
            if a.status == AgentStatus.PAUSED and a.last_error == 'Engine stopped during execution. Inspect artifacts, then Resume.':
                if aid in self.paused:
                    restart_paused.append(aid)
        for aid in restart_paused:
            try:
                a = self.agents[aid]
                raw = self.paused.pop(aid, None)
                text = raw['content'] if raw and 'content' in raw else a.current_task
                a.status = AgentStatus.WORKING
                a.last_error = None
                a.autonomous_turns = 0
                await self.send(a.project_name, a.id,
                                'Inspect existing files; continue without repeating completed side effects.\n' + text,
                                sender_id='system', kind='resume')
            except Exception:
                pass
        for aid, queue in self.inboxes.items():
            if queue and aid not in self.paused:
                self.schedule(aid)
        if not self._quota_watchdog_task or self._quota_watchdog_task.done():
            self._quota_watchdog_task = asyncio.create_task(self._quota_watchdog_loop())

    async def close(self):
        self.closing = True
        if self._quota_watchdog_task and not self._quota_watchdog_task.done():
            self._quota_watchdog_task.cancel()
        for task in list(self.active.values()):
            task.cancel()
        await asyncio.gather(*list(self.pumps.values()), return_exceptions=True)
        self.persist()
        self.store.close()

    async def resume_agent(self, target_agent_id: str, message: Optional[str] = None) -> dict:
        """Helper to resume an agent using system authority."""
        a = self.agent(target_agent_id)
        return await self.action(a.project_name, 'resume_agent', {
            'target_agent_id': target_agent_id,
            'message': message
        }, actor_id='system')

    async def _quota_watchdog_loop(self):
        """Monitors Google account cooldowns, heals expired accounts,
        and automatically resumes agents that were paused waiting for Google quota.
        """
        while not self.closing:
            try:
                await asyncio.sleep(10)
                if not getattr(self, 'auth_pool', None) or not self.auth_pool.accounts:
                    continue

                # 1. Transition expired cooldowns to HEALTHY
                healed = self.auth_pool.heal_expired_cooldowns()
                if healed:
                    logger.info(f"Google accounts {healed} cooldown expired; restored to HEALTHY")

                # 2. Check for available Google capacity (up to max_concurrent per account)
                has_capacity = any(
                    acc.health_state == GoogleAccountHealth.HEALTHY
                    and not acc.is_cooldown_active()
                    and acc.active_agents < acc.max_concurrent
                    for acc in self.auth_pool.accounts.values()
                )
                if not has_capacity:
                    continue

                # 3. Find paused agents waiting for Google quota / accounts
                for aid, a in list(self.agents.items()):
                    if a.status == AgentStatus.PAUSED and a.last_error and (
                        'google accounts available' in a.last_error.lower()
                        or 'quota' in a.last_error.lower()
                        or 'auth_blocked' in a.last_error.lower()
                    ):
                        try:
                            logger.info(f"Auto-resuming agent {a.name} ({aid}): healthy Google account available in pool")
                            await self.resume_agent(aid, 'Resumed automatically after Google account quota cooldown expired or slot became available.')
                            await self.router.broadcast('agent_updated', a.model_dump(exclude={'system_prompt'}))
                        except Exception as exc:
                            logger.warning(f"Failed to auto-resume {a.name}: {exc}")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Error in quota watchdog loop: {exc}")

    def get_watchdog(self, project=None):
        from core.telegram_supervisor import telegram_supervisor
        running = telegram_supervisor.is_running()
        proj_key = project or self.active_project_name or 'global'
        p = self.projects.get(proj_key) if proj_key else None
        ceo = self.get_ceo(proj_key) if proj_key and proj_key in self.projects else None
        manager = self.get_manager(proj_key) if proj_key and proj_key in self.projects else None
        connections = [c.id for c in (ceo, manager) if c]
        saved_pos = getattr(self, 'watchdog_positions', {}).get(proj_key)
        return AgentNode(
            id='system_root_watchdog',
            project_name=proj_key,
            name='Root_Watchdog',
            role=Role.WATCHDOG,
            model='antigravity/gemini-3.8-flash-high',
            harness=HarnessType.ANTIGRAVITY,
            thinking_budget=0,
            reasoning_effort='high',
            status=AgentStatus.WORKING if running else AgentStatus.IDLE,
            current_task='Supervising team & Telegram human bridge' if running else 'Telegram bridge standby',
            working_dir='',
            created_at=p.created_at if p else now(),
            last_heartbeat=now(),
            avatar_logo='gemini',
            hat='officer',
            parent_id=None,
            connections=connections,
            position=saved_pos,
            session_id='watchdog-session',
            last_error=telegram_supervisor.last_error
        )

    def agent(self, aid, project=None):
        if aid == 'system_root_watchdog':
            return self.get_watchdog(project or self.active_project_name)
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
                'watchdog':public(self.get_watchdog(project)),
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
        if role == Role.MANAGER:
            model = 'antigravity/gemini-3.8-flash-high'
            harness = 'antigravity'
            reasoning_effort = reasoning_effort or 'high'
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

    async def delete_project(self, name: str) -> bool:
        target_name = None
        for k in self.projects:
            if k.casefold() == name.casefold():
                target_name = k
                break
        if not target_name:
            deleted_dir = self.workspace.delete_project_dir(name)
            return deleted_dir

        p = self.projects[target_name]
        project_agents = [a for a in list(self.agents.values()) if a.project_name == target_name]
        for a in project_agents:
            a.status = AgentStatus.TERMINATED
            if a.id in self.pumps and not self.pumps[a.id].done():
                self.pumps[a.id].cancel()
            self.agents.pop(a.id, None)

        del self.projects[target_name]
        if self.active_project_name == target_name:
            self.active_project_name = next(iter(self.projects.keys()), None)

        self.workspace.delete_project_dir(target_name)
        self.persist()
        await self.router.broadcast('project_deleted', {'name': target_name})
        return True

    async def create_manager(self, project, **spec):
        p = self.projects[project]
        if p.manager_id:
            raise ValueError('Manager already exists; send it a message')
        spec['model'] = 'antigravity/gemini-3.8-flash-high'
        spec['harness'] = 'antigravity'
        if not spec.get('reasoning_effort'):
            spec['reasoning_effort'] = 'high'
        a = self._add_agent(project,role=Role.MANAGER,parent_id=p.ceo_id,**spec)
        p.manager_id = a.id
        p.status = 'active'
        self.persist()
        await self.send(project,a.id,a.current_task,sender_id=p.ceo_id,kind='task')
        return a

    async def spawn_worker(self, project, actor_id=None, **spec):
        p = self.projects[project]
        is_ceo_direct = (actor_id == p.ceo_id) or (not p.manager_id and p.ceo_id)
        parent_id = p.ceo_id if is_ceo_direct else p.manager_id
        if not parent_id:
            raise ValueError('Create a manager or CEO before spawning workers')
        live = [a for a in self.agents.values() if a.project_name == project and
                a.role == Role.WORKER and a.status != AgentStatus.TERMINATED]
        if len(live) >= self.config.max_workers:
            raise ValueError('Worker limit reached')
        if not spec.get('model'):
            spec['model'] = 'antigravity/gemini-3.8-flash-high'
            spec['harness'] = 'antigravity'
        elif spec.get('model') in ('antigravity/gemini-3.8-flash-high', 'antigravity/claude-opus-4-6-thinking') and not spec.get('harness'):
            spec['harness'] = 'antigravity'
        a = self._add_agent(project,role=Role.WORKER,parent_id=parent_id,**spec)
        p.worker_ids.append(a.id)
        self.persist()
        await self.send(project,a.id,a.current_task,sender_id=parent_id,kind='task')
        await self.router.broadcast('worker_spawned', a.model_dump(exclude={'system_prompt'}))
        return a

    def schedule(self, aid):
        if not self.closing and (aid not in self.pumps or self.pumps[aid].done()):
            self.pumps[aid] = asyncio.create_task(self._pump(aid))

    async def send(self, project, target_id, content, sender_id='human_owner', is_interrupt=False, kind='message'):
        target = self.agent(target_id, project=project)
        if target_id != 'system_root_watchdog' and (target.project_name != project or target.status == AgentStatus.TERMINATED):
            raise ValueError('Target is not an active member of this project')
        sender = self.agent(sender_id, project=project) if sender_id not in ('human_owner','system') else None
        if sender and sender.id != 'system_root_watchdog' and sender.project_name != project:
            raise PermissionError('Cross-project messaging denied')
        if target_id == sender_id:
            raise ValueError('Use wait_for_workers or finish this turn; self messaging is disabled')
        if target_id == 'system_root_watchdog':
            msg = Message(project_name=project,sender_id=sender_id,
                sender_name=sender.name if sender else ('Owner' if sender_id == 'human_owner' else 'Engine'),
                sender_role=sender.role.value if sender else ('HUMAN' if sender_id == 'human_owner' else 'SYSTEM'),
                recipient_id=target.id,recipient_name=target.name,content=content,
                is_interrupt=is_interrupt,kind=kind)
            self.messages.append(msg)
            self.persist()
            await self.router.broadcast('new_message',msg.model_dump())
            is_escalation = kind == 'escalation'
            is_completion = kind in ('finish_project', 'completion')
            has_alert_flag = any(flag in content for flag in ('[ALERT_OWNER]', '[ACTION_REQUIRED]', '[NOTIFY_OWNER]'))

            if sender and sender_id not in ('human_owner', 'system') and (is_escalation or is_completion or has_alert_flag):
                try:
                    from core.telegram_supervisor import telegram_supervisor
                    telegram_supervisor.send_owner_notification(
                        f"🛡️ <b>{sender.name} ({sender.role.value}) → Human Owner</b>\n"
                        f"<b>Project:</b> <code>{project}</code>\n\n{content}"
                    )
                except Exception:
                    pass
            elif sender_id in ('human_owner', 'system'):
                asyncio.create_task(self._process_watchdog_message(project, content))
            return msg
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

    async def _process_watchdog_message(self, project, content):
        try:
            from core.watchdog_brain import watchdog_brain
            reply, _ = watchdog_brain.generate_response('studio_chat', content, project_name=project)
            reply_msg = Message(project_name=project, sender_id='system_root_watchdog',
                sender_name='Root_Watchdog', sender_role=Role.WATCHDOG.value,
                recipient_id='human_owner', recipient_name='Owner', content=reply,
                kind='message')
            self.messages.append(reply_msg)
            self.persist()
            await self.router.broadcast('new_message', reply_msg.model_dump())
        except Exception:
            pass

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
                if a.role == Role.MANAGER and not decision and self.projects[a.project_name].status == 'active' and a.status != AgentStatus.PAUSED and aid not in self.paused:
                    busy = any(x.parent_id == aid and (self.inboxes[x.id] or x.id in self.active)
                               for x in self.agents.values() if x.status != AgentStatus.TERMINATED)
                    is_standdown = any(phrase in (str(result) or '').lower() or phrase in str(a.current_task).lower()
                                       for phrase in ('stand-down', 'stand down', 'holding pattern', 'awaiting supervisory', 'escalation hold', 'awaiting clarification', 'parked', 'awaiting fresh credentials', 'awaiting credentials', 'awaiting', 'escalation'))
                    if is_standdown:
                        a.status = AgentStatus.PAUSED
                        self.paused[aid] = raw
                        await self.emit(a, 'holding_pattern', {'task': a.current_task})
                    elif not busy:
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
                heal_res = await self.auto_diagnose_and_heal_agent(a.id, retry_turn=True)
                if not heal_res.get('healed'):
                    await self._parent_notice(a, f'{a.name} paused: {a.last_error}', kind='escalation')
                break
            except Exception as exc:
                a.status = AgentStatus.FAILED
                a.last_error = self.config.redact(exc)
                await self.emit(a,'error',{'error':a.last_error})
                heal_res = await self.auto_diagnose_and_heal_agent(a.id, retry_turn=True)
                if not heal_res.get('healed'):
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
        prompts = {Role.CEO: CEO_SYSTEM_PROMPT, Role.MANAGER: MANAGER_SYSTEM_PROMPT, Role.WORKER: WORKER_SYSTEM_PROMPT}
        if a.role in prompts:
            specialty_suffix = ''
            if 'Specialty:' in (a.system_prompt or ''):
                specialty_suffix = '\nSpecialty:' + a.system_prompt.split('Specialty:', 1)[1]
            a.system_prompt = prompts[a.role] + specialty_suffix
        async def emit(kind,data):
            await self.emit(a,kind,data)
        async def execute(name,args):
            return await self.action(a.project_name,name,args,a.id)
        if a.handoff_pending:
            msg = msg.model_copy(update={'content': a.handoff_pending + '\n\n' + msg.content})
        if a.harness == HarnessType.DIRECT_API:
            context = self.contexts[a.id]
            sys_content = a.system_prompt + '\nProject: ' + self.projects[a.project_name].description
            if not context:
                context.append({'role':'system','content':sys_content})
            elif context[0].get('role') == 'system':
                context[0]['content'] = sys_content
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
        actor = None if actor_id in ('human_owner', 'system') else self.agent(actor_id, project=project)
        if actor and actor.id != 'system_root_watchdog' and actor.project_name != project: raise PermissionError('Cross-project action denied')
        if actor and actor.status == AgentStatus.TERMINATED: raise PermissionError('Agent terminated')
        role = actor.role.value if (actor and actor.id != 'system_root_watchdog') else 'HUMAN'
        validate(role,name,args)
        p = self.projects[project]
        if name == 'get_team_tree': return self.get_tree(project)
        if name == 'list_capabilities':
            return {'providers':{k:v.model_dump() for k,v in self.config.providers.items()},
                    'configured_key_providers':[k for k,v in self.config.api_keys.items() if v],
                    'harnesses':self.cli.capabilities(),'command_execution':p.allow_commands,
                    'google_auth_pool':self.auth_pool.public(include_identity=actor is None),
                    'model_routing_guidance':{
                        'primary_workhorse': 'antigravity/gemini-3.8-flash-high (primary powerhouse; strongest model; default for manager and critical workers)',
                        'free_extra_workers': 'antigravity/claude-opus-4-6-thinking (independent 5-hour rolling quota bucket per Google account; Claude 4.6 Opus via Google Auth Pool)',
                        'external_models_policy': 'ALL other external models (zai/* GLM, deepseek/*, direct anthropic/*, openai/* GPT/Codex) REQUIRE explicit Human Owner permission before use.'
                    }}
        if name in ('create_manager','spawn_worker'):
            a = await (self.create_manager(project,**args) if name == 'create_manager' else self.spawn_worker(project,actor_id=actor_id,**args))
            return {'agent_id':a.id,'state':a.status.value,'working_dir':a.working_dir,'parent_id':a.parent_id}
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
            if isinstance(raw, dict):
                text=args.get('message') or raw.get('content') or a.current_task or 'Resume previous workflow and status check.'
            else:
                text=args.get('message') or a.current_task or 'Resume previous workflow and status check.'
            a.autonomous_turns=0
            await self.send(project,a.id,'Inspect existing files; continue without repeating completed side effects.\n'+text,
                            sender_id=actor_id,kind='resume')
            return {'resumed':True}
        if name == 'terminate_worker':
            return await self.terminate_worker(project,**args)
        if name in ('optimize_agent', 'heal_agent'):
            target_id = args.get('target_agent_id') or args.get('agent_id')
            return await self.auto_diagnose_and_heal_agent(target_id)
        if name == 'optimize_fleet':
            results = []
            for ag in list(self.agents.values()):
                if ag.project_name == project and ag.status in (AgentStatus.FAILED, AgentStatus.PAUSED):
                    res = await self.auto_diagnose_and_heal_agent(ag.id)
                    results.append({'agent_id': ag.id, 'name': ag.name, 'result': res})
            return {'fleet_optimized': True, 'healed_count': sum(1 for r in results if r['result'].get('healed')), 'results': results}
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
            try:
                from core.telegram_supervisor import telegram_supervisor
                telegram_supervisor.send_owner_notification(
                    f"🚨 <b>Escalation from {a.name} ({a.role.value})</b>\n"
                    f"<b>Project:</b> <code>{project}</code>\n\n{args['issue_summary']}"
                )
            except Exception:
                pass
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
            if a.role == Role.MANAGER and p.ceo_id:
                await self._parent_notice(a, f"Project completed by Manager:\nSummary: {args['summary']}\nArtifacts: {args['artifacts']}")
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
        src = self.agent(source_id, project=project)
        dst = self.agent(target_id, project=project)
        if (src.id != 'system_root_watchdog' and src.project_name != project) or (dst.id != 'system_root_watchdog' and dst.project_name != project):
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
        src = self.agent(source_id, project=project)
        dst = self.agent(target_id, project=project)
        if (src.id != 'system_root_watchdog' and src.project_name != project) or (dst.id != 'system_root_watchdog' and dst.project_name != project):
            raise PermissionError('Cross-project disconnect denied')
        if target_id in src.connections:
            src.connections.remove(target_id)
        if source_id in dst.connections:
            dst.connections.remove(source_id)
        self.persist()
        await self.router.broadcast('agents_disconnected', {'project_name': project, 'source_id': source_id, 'target_id': target_id})
        return {'disconnected': True, 'source_id': source_id, 'target_id': target_id}

    async def set_agent_position(self, project, agent_id, x, y):
        if not hasattr(self, 'watchdog_positions'):
            self.watchdog_positions = {}
        if agent_id == 'system_root_watchdog':
            pos = None if (x is None and y is None) else {'x': float(x), 'y': float(y)}
            if pos and not all(math.isfinite(float(v)) and 0 <= float(v) <= 20000 for v in (x, y)):
                raise ValueError('Canvas coordinates must be finite and between 0 and 20000')
            self.watchdog_positions[project or self.active_project_name or 'global'] = pos
            await self.router.broadcast('agent_positioned', {'project_name': project, 'agent_id': agent_id, 'position': pos})
            return {'saved': True, 'position': pos}
        a = self.agent(agent_id, project=project)
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
        a.status = AgentStatus.WORKING
        await self.emit(a, 'auth_ready', {})
        return await self._google_turn_leased(a, msg, emit)

    async def _google_turn_leased(self, a, msg, emit):
        used = set()
        previous_quota = None
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
                    # The native conversation remains in the same HOME on failover.
                    # Account selection changes only the credential, never the session directory.
                    if not a.google_session_dir:
                        old = self.auth_pool.accounts.get(a.auth_slot_id)
                        a.google_session_dir = '@default' if a.session_id and not old else str(self.auth_pool.resolve_auth_dir(old or slot))
                    a.auth_slot_id = slot.account_id
                self.persist()
                auth_dir = Path(a.google_session_dir) if a.google_session_dir and a.google_session_dir != '@default' else None
                launched_event = asyncio.Event()

                async def run_task():
                    return await self.cli.execute_task(a, msg.formatted_text(), Path(a.working_dir), emit,
                        endpoint=self.endpoint, token=self.token_for(a.id),
                        allow_commands=self.projects[a.project_name].allow_commands, auth_dir=auth_dir,
                        google_account={'account_id': slot.account_id, 'email': slot.email} if slot else None,
                        previous_google_quota=previous_quota,
                        launched_event=launched_event)

                if slot:
                    async with self.auth_pool.launch_lease(slot.account_id):
                        task = asyncio.create_task(run_task())
                        launch_waiter = asyncio.create_task(launched_event.wait())
                        try:
                            await asyncio.wait([task, launch_waiter], timeout=12, return_when=asyncio.FIRST_COMPLETED)
                        finally:
                            if not launch_waiter.done():
                                launch_waiter.cancel()
                    result = await task
                else:
                    result = await run_task()
                if slot:
                    usage = getattr(a, 'last_turn_usage', None) or {}
                    self.auth_pool.mark_success(
                        slot.account_id,
                        input_tokens=usage.get('input_tokens', 0),
                        output_tokens=usage.get('output_tokens', 0),
                        cache_read_tokens=usage.get('cache_read_tokens', 0),
                        model=a.model,
                    )
                a.handoff_pending = None
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                exc_str = str(exc)
                if "subscriber fell behind updates" in exc_str.lower():
                    a.session_id = None
                    self.persist()
                retryable, seconds, reset = self.auth_pool.classify_error(exc)
                if not slot or not retryable:
                    if slot: self.auth_pool.mark_error(slot.account_id, exc_str)
                    raise
                if previous_quota and exc_str == previous_quota and len(used) > 1:
                    self.auth_pool.mark_transient_error(slot.account_id, exc_str, cooldown_seconds=60, model=a.model)
                else:
                    previous_quota = exc_str
                    self.auth_pool.mark_quota_blocked(slot.account_id, exc_str, seconds, reset, model=a.model)
                await self.emit(a, 'auth_failover', {'previous_account': slot.account_id,
                    'cooldown_seconds': seconds, 'reset_time': reset,
                    'session_id': a.session_id, 'session_directory_retained': True})
                # Re-enter the saved native conversation; never blindly replay tool operations.
                msg = msg.model_copy(update={'content': 'Continue after an account quota interruption. '
                    'Inspect your latest tool results and artifacts before retrying any operation.\n' + msg.content})
            finally:
                if slot:
                    try: self.auth_pool.retain_refreshed_credential(slot.account_id)
                    finally: self.auth_pool.release_slot(slot.account_id, model=a.model)

    async def reconfigure_agent(self, project, target_agent_id, model, harness,
                                mode='after_turn', actor_id='human_owner',
                                reasoning_effort=None, provider=None, name=None):
        a = self.agent(target_agent_id)
        if a.project_name != project: raise PermissionError('Cross-project change denied')
        if a.status == AgentStatus.TERMINATED: raise ValueError('Agent is terminated')
        if a.role == Role.MANAGER:
            if model.strip() != 'antigravity/gemini-3.8-flash-high' or HarnessType(harness) != HarnessType.ANTIGRAVITY:
                raise PermissionError('Manager role is permanently locked to antigravity/gemini-3.8-flash-high (antigravity harness) and cannot be changed to another model.')
        if actor_id != 'human_owner':
            actor = self.agent(actor_id)
            rank = {Role.CEO: 2, Role.MANAGER: 1, Role.WORKER: 0}
            if a.id != actor.id and rank[actor.role] <= rank[a.role] and 'Infra' not in actor.name:
                raise PermissionError('Only owner or a supervisor can change this agent')
            if a.id == actor.id and mode == 'interrupt':
                raise ValueError('Self changes must wait for the current turn')
        if mode not in ('after_turn', 'interrupt'): raise ValueError('Invalid change mode')
        h = HarnessType(harness)
        if not model.strip(): raise ValueError('Model is required')
        if h != HarnessType.DIRECT_API:
            if not self.projects[project].allow_commands: raise PermissionError('CLI commands are disabled')
            self.cli.validate(h, model, 0, reasoning_effort)
        spec = {'model': model.strip(), 'harness': h.value}
        if reasoning_effort is not None:
            spec['reasoning_effort'] = reasoning_effort
        if provider is not None:
            spec['provider'] = provider
        if name is not None:
            spec['name'] = name
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
        target_name = spec.get('name', a.name)
        target_provider = spec.get('provider', getattr(a, 'provider', None))
        target_effort = spec.get('reasoning_effort', getattr(a, 'reasoning_effort', None))
        if (a.model, a.harness.value, getattr(a, 'reasoning_effort', None), getattr(a, 'provider', None), a.name) == \
           (spec['model'], spec['harness'], target_effort, target_provider, target_name):
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

        full_history_file = folder / 'CONVERSATION_HISTORY_FULL.md'
        agent_msgs = [m for m in self.messages if a.id in (m.sender_id, m.recipient_id)]
        all_formatted = '\n\n---\n\n'.join(m.formatted_text() for m in agent_msgs)
        full_history_file.write_text(all_formatted, encoding='utf-8')

        recent = '\n'.join(m.formatted_text() for m in agent_msgs)[-60000:]
        a.handoff_pending = ('Runtime changed. You are the SAME logical agent with the SAME files, role and team. '
            'Do not restart completed work. Inspect unknown tool outcomes before repeating operations. '
            'Prior native session is archived; hidden reasoning is not transferable.\n'
            f'Full prior conversation archive (exact byte-to-byte JSON): {archive}\n'
            f'Full conversation markdown transcript (exact byte-to-byte): {full_history_file}\n'
            f'Current assignment: {a.current_task}\nLast result: {a.last_result}\nRecent conversation:\n{recent}')
        a.model, a.harness = spec['model'], HarnessType(spec['harness'])
        a.session_id = None
        a.google_session_dir = None
        a.auth_slot_id = None
        if 'reasoning_effort' in spec:
            a.reasoning_effort = spec['reasoning_effort']
        else:
            a.reasoning_effort = None
        if 'provider' in spec:
            a.provider = spec['provider']
        if 'name' in spec and spec['name']:
            a.name = spec['name']
        if a.status in (AgentStatus.FAILED, AgentStatus.BLOCKED_LOOP):
            a.status = AgentStatus.IDLE
            a.last_error = None
        a.thinking_budget = 0
        if spec['harness'] == 'direct_api':
            new_ctx = []
            sys_content = a.system_prompt + '\nProject: ' + self.projects[a.project_name].description
            new_ctx.append({'role': 'system', 'content': sys_content})
            char_budget = 120_000
            selected_msgs = []
            current_chars = 0
            for m in reversed(agent_msgs):
                txt = m.formatted_text()
                if current_chars + len(txt) > char_budget and len(selected_msgs) >= 10:
                    break
                selected_msgs.append((m, txt))
                current_chars += len(txt)
            selected_msgs.reverse()
            for m, txt in selected_msgs:
                r = 'assistant' if m.sender_id == a.id else 'user'
                new_ctx.append({'role': r, 'content': txt})
            self.contexts[a.id] = new_ctx
        else:
            self.contexts[a.id] = []
        a.pending_configuration = None
        self.persist()
        await self.emit(a, 'configuration_changed', {'previous': old, 'model': a.model, 'harness': a.harness.value})
        await self.router.broadcast('agent_updated', a.model_dump(exclude={'system_prompt'}))

    async def auto_diagnose_and_heal_agent(self, agent_id: str, retry_turn: bool = True) -> Dict[str, Any]:
        """Watchdog Fleet Optimizer:
        Inspects why an agent failed or stalled, auto-diagnoses the root cause,
        heals transient quota/timeout/session issues, preserves the exact model lock,
        and safely resumes execution without requiring human intervention.
        """
        if not agent_id or agent_id not in self.agents:
            return {'healed': False, 'reason': f"Agent '{agent_id}' not found"}
        a = self.agents[agent_id]
        if a.status not in (AgentStatus.FAILED, AgentStatus.PAUSED):
            return {'healed': False, 'reason': f"Agent status is {a.status.value}, does not require healing"}

        err = (a.last_error or '').lower()
        model = a.model
        action_taken = None

        # 1. Google Auth Pool / Quota saturation (RESOURCE_EXHAUSTED / 429)
        if any(k in err for k in ('resource_exhausted', '429', 'quota', 'rate limit', 'cooldown')):
            healed = self.auth_pool.heal_expired_cooldowns()
            a.auth_slot_id = None
            a.last_error = None
            a.status = AgentStatus.IDLE
            action_taken = f"Rotated Google auth slot; auto-healed cooldowns ({len(healed)} healed); locked to exact model '{model}'."

        # 2. CLI Subprocess Print Timeout / Broken Pipe / Timed out
        elif any(k in err for k in ('print timeout', 'timeout', 'broken pipe', 'timed out')):
            self.auth_pool.heal_expired_cooldowns()
            a.session_id = None
            a.last_error = None
            a.status = AgentStatus.IDLE
            action_taken = f"Cleared transient CLI timeout & reset session ID; auto-resumed on exact model '{model}'."

        # 3. Session state desync / Conversation not found / Subscriber fell behind
        elif any(k in err for k in ('conversation not found', 'subscriber fell behind', 'invalid session')):
            a.session_id = None
            a.google_session_dir = None
            a.last_error = None
            a.status = AgentStatus.IDLE
            action_taken = f"Purged desynchronized session state; fresh turn scheduled on exact model '{model}'."

        # 4. Worker completed turn without formal report_result call
        elif a.role == Role.WORKER and 'without report_result' in err:
            a.status = AgentStatus.IDLE
            a.last_error = None
            action_taken = f"Nudged worker to inspect deliverables and seal formal report on exact model '{model}'."

        # 5. Missing / malformed effort flag on Gemini Flash
        elif 'requires --effort' in err or 'invalid model selection' in err:
            if 'flash' in model.lower():
                a.reasoning_effort = 'high'
            a.last_error = None
            a.status = AgentStatus.IDLE
            action_taken = f"Normalized reasoning effort flag for exact model '{model}'; auto-resumed."

        if action_taken:
            self._write_status(a)
            self.persist()
            await self.emit(a, 'watchdog_optimizer_healed', {'action': action_taken, 'model': model})
            await self.router.broadcast('agent_updated', a.model_dump(exclude={'system_prompt'}))
            if retry_turn:
                if a.id in self.paused:
                    raw = self.paused.pop(a.id)
                    raw_content = raw.get('content') if isinstance(raw, dict) else getattr(raw, 'content', str(raw))
                    sender_id = raw.get('sender_id', 'system_root_watchdog') if isinstance(raw, dict) else getattr(raw, 'sender_id', 'system_root_watchdog')
                    kind = raw.get('kind', 'resume') if isinstance(raw, dict) else getattr(raw, 'kind', 'resume')
                    await self.send(a.project_name, a.id, raw_content, sender_id=sender_id, kind=kind)
                elif self.inboxes.get(a.id) or a.current_task:
                    await self.resume_agent(a.project_name, a.id, actor_id='system_root_watchdog')
            return {'healed': True, 'action': action_taken, 'model': model}

        return {'healed': False, 'reason': f"Non-recoverable failure requires human instruction: {a.last_error}"}
