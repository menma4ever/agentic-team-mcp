from enum import Enum
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


class Role(str, Enum):
    CEO = 'CEO'
    MANAGER = 'MANAGER'
    WORKER = 'WORKER'


class AgentStatus(str, Enum):
    IDLE = 'idle'
    QUEUED = 'queued'
    WORKING = 'working'
    FAILED = 'failed'
    BLOCKED_LOOP = 'blocked_loop'
    PAUSED = 'paused'
    TERMINATED = 'terminated'


class HarnessType(str, Enum):
    DIRECT_API = 'direct_api'
    CLAUDE_CODE = 'claude_code'
    CODEX = 'codex'
    ANTIGRAVITY = 'antigravity'
    GEMINI_CLI = 'gemini_cli'
    HERMES = 'hermes'
    OPENCLAW = 'openclaw'


class AgentNode(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    project_name: str
    name: str
    role: Role
    model: str
    harness: HarnessType = HarnessType.DIRECT_API
    thinking_budget: int = 0
    reasoning_effort: Optional[str] = None
    status: AgentStatus = AgentStatus.IDLE
    current_task: str = 'Awaiting task'
    working_dir: str = ''
    created_at: str = Field(default_factory=now)
    last_heartbeat: str = Field(default_factory=now)
    avatar_logo: str = 'default'
    hat: str = 'hardhat'
    parent_id: Optional[str] = None
    system_prompt: str = ''
    session_id: Optional[str] = None
    last_error: Optional[str] = None
    run_id: Optional[str] = None
    pid: Optional[int] = None
    process_started_at: Optional[float] = None
    autonomous_turns: int = 0
    last_result: Optional[dict] = None
    connections: list[str] = Field(default_factory=list)
    position: Optional[dict] = None
    google_session_dir: Optional[str] = None
    pending_configuration: Optional[dict] = None
    session_history: list[dict] = Field(default_factory=list)
    handoff_pending: Optional[str] = None
    provider: str = 'google'
    auth_slot_id: Optional[str] = None
    forced_auth_slot_id: Optional[str] = None

    def update_heartbeat(self):
        self.last_heartbeat = now()


class Message(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    project_name: str
    sender_id: str
    sender_name: str
    sender_role: str
    recipient_id: str
    recipient_name: str
    content: str
    timestamp: str = Field(default_factory=now)
    is_interrupt: bool = False
    read: bool = False
    kind: str = 'message'

    def formatted_text(self):
        name = 'Human Owner' if self.sender_role == 'HUMAN' else f'{self.sender_role} {self.sender_name}'
        return f'[{name}]: {self.content}'


class ProjectState(BaseModel):
    name: str
    description: str = ''
    created_at: str = Field(default_factory=now)
    ceo_id: Optional[str] = None
    manager_id: Optional[str] = None
    worker_ids: list[str] = Field(default_factory=list)
    status: str = 'planning'
    completion: Optional[dict] = None
    allow_commands: bool = False
    read_roots: list[str] = Field(default_factory=list)

