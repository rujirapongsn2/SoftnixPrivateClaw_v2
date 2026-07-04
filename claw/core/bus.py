"""In-memory, event-driven pub/sub bus. No polling anywhere.

Subscribers get their own bounded queue per session topic; slow subscribers
drop oldest events instead of blocking the agent (UI can always refetch state
from the message API).
"""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator

from loguru import logger

from claw.core.events import AgentEvent

_QUEUE_MAX = 1024


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = defaultdict(set)

    def publish(self, session_id: str, event: AgentEvent) -> None:
        for queue in self._subscribers.get(session_id, ()):  # copy-safe: sets not mutated during publish
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except asyncio.QueueEmpty:
                    pass
                logger.warning("Dropped oldest event for slow subscriber on {}", session_id)

    @asynccontextmanager
    async def subscribe(self, session_id: str) -> AsyncIterator["asyncio.Queue[AgentEvent]"]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subscribers[session_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[session_id].discard(queue)
            if not self._subscribers[session_id]:
                self._subscribers.pop(session_id, None)

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subscribers.get(session_id, ()))
