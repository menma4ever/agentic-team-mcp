# Transport only. Durable inboxes, attribution and scheduling are owned by Orchestrator.
import asyncio


class MessageRouter:
    def __init__(self):
        self.subscribers = []

    def subscribe(self, callback):
        self.subscribers.append(callback)

    def unsubscribe(self, callback):
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    async def broadcast(self, event_type, data):
        # Slow/disconnected browsers must not stall execution.
        async def deliver(callback):
            try:
                async with asyncio.timeout(2):
                    await callback({'type':event_type, 'data':data})
            except Exception:
                pass
        if self.subscribers:
            await asyncio.gather(*(deliver(c) for c in list(self.subscribers)))


message_router = MessageRouter()

