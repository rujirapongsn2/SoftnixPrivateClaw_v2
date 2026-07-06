"""In-memory, event-driven pub/sub bus. No polling anywhere.

Subscribers get their own bounded queue per session topic; slow subscribers
drop oldest events instead of blocking the agent (UI can always refetch state
from the message API).

Each session also keeps a small replay buffer of the CURRENT turn's events. A
(re)connecting subscriber is replayed that buffer first, so a client that
navigated away mid-turn and came back rebuilds the live-only UI (tool cards,
sub-steps, confirmation cards, in-progress streaming) that the message API
can't return yet — it only has persisted user/assistant messages. The buffer
resets on `turn_started` and clears on `turn_completed`/`turn_error`, since a
finished turn's answer is durable and served by listMessages.
"""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator

from loguru import logger

from claw.core.events import AgentEvent

_QUEUE_MAX = 1024
# Cap the per-turn replay so a very long stream can't grow without bound; when
# exceeded the oldest events drop (a mid-turn reconnect may miss early stream
# text, but tool/confirm events are few and the final answer self-heals via the
# persisted-message refetch on completion).
_REPLAY_MAX = 2048


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = defaultdict(set)
        # session_id -> current turn's events, for replay to (re)connecting clients.
        self._replay: dict[str, list[AgentEvent]] = {}

    def publish(self, session_id: str, event: AgentEvent) -> None:
        self._record(session_id, event)
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

    def _record(self, session_id: str, event: AgentEvent) -> None:
        etype = getattr(event, "type", "")
        if etype == "turn_started":
            self._replay[session_id] = [event]
            return
        buf = self._replay.get(session_id)
        if buf is None:
            return  # no active turn tracked → nothing to replay
        buf.append(event)
        if len(buf) > _REPLAY_MAX:
            del buf[: len(buf) - _REPLAY_MAX]
        if etype in ("turn_completed", "turn_error"):
            # Turn done: the answer is persisted, so future connects rebuild from
            # listMessages — drop the live buffer.
            self._replay.pop(session_id, None)

    @asynccontextmanager
    async def subscribe(self, session_id: str) -> AsyncIterator["asyncio.Queue[AgentEvent]"]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=_QUEUE_MAX)
        # Replay the current turn's events first so a reconnecting client rebuilds
        # its live view. No `await` between here and registering the queue, so a
        # concurrent publish can't interleave (each future event lands live and is
        # NOT in this snapshot → no duplication).
        for event in self._replay.get(session_id, ()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                break
        self._subscribers[session_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[session_id].discard(queue)
            if not self._subscribers[session_id]:
                self._subscribers.pop(session_id, None)

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subscribers.get(session_id, ()))
