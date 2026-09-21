import asyncio
from datetime import datetime, timezone
from pathlib import Path


class LoopMonitor:
    def __init__(self, engine):
        self.engine=engine
        self.alerted=set()
        self.task=None

    async def start(self):
        self.task=asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)

    async def run(self):
        while True:
            await asyncio.sleep(self.engine.config.heartbeat_interval_seconds)
            for aid in list(self.engine.active):
                a=self.engine.agent(aid)
                if a.status.value != "working": continue
                if a.run_id in self.alerted: continue
                last=datetime.fromisoformat(a.last_heartbeat).timestamp()
                status=Path(a.working_dir)/'status.md'
                if status.exists(): last=max(last,status.stat().st_mtime)
                elapsed=datetime.now(timezone.utc).timestamp()-last
                if elapsed>self.engine.config.inactivity_timeout_seconds:
                    self.alerted.add(a.run_id)
                    await self.engine.emit(a,'stall_warning',{'seconds_without_activity':int(elapsed)})
                    await self.engine._parent_notice(a,
                        f'{a.name} has no output or status.md change for {int(elapsed)} seconds. '
                        'Inspect its events and choose queue, interrupt, or termination.',kind='escalation')

