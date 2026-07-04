"""Heartbeat: the agent's proactive self-check.

Opt-in per user. On each tick the agent reviews its own memory/context and, via
a forced tool call (no fragile free-text parsing), decides whether anything is
worth reaching out about. Two phases:

  1. decide  — cheap LLM call → {action: skip|run, note}
  2. act     — only when action == "run": a normal agent turn is fired into a
               session, so the proactive message streams to the user like any reply.

This is distinct from the scheduler (which runs *user-defined* prompts at fixed
times); heartbeat is the agent deciding for itself that a check-in is warranted.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger

from claw.db.stores import MemoryStore, SessionStore, UserStore
from claw.providers.base import LLMProvider

# handle(user_id, session_id, prompt) -> final content
TurnHandler = Callable[[str, str, str], Awaitable[str | None]]

_MAX_SLEEP = 60.0

_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat_decision",
            "description": "Report whether the agent should proactively reach out now.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing worth saying; run = proactively message the user",
                    },
                    "note": {
                        "type": "string",
                        "description": "For run: what to do or say (an instruction to yourself).",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


@dataclass(slots=True)
class HeartbeatDecision:
    action: str
    note: str = ""


class HeartbeatService:
    def __init__(
        self,
        users: UserStore,
        memories: MemoryStore,
        sessions: SessionStore,
        provider: LLMProvider,
        handler: TurnHandler,
        model: str | None = None,
    ):
        self.users = users
        self.memories = memories
        self.sessions = sessions
        self.provider = provider
        self.handler = handler
        self.model = model
        self._running = False
        self._task: asyncio.Task | None = None
        self._firing: set[str] = set()

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def decide(self, user_id: str) -> HeartbeatDecision:
        """Phase 1: ask the model whether a proactive check-in is warranted."""
        core = await self.memories.get_core(user_id)
        history = await self.memories.recent_history(user_id, limit=5)
        context = (
            f"Core memory about the user:\n{core or '(none)'}\n\n"
            f"Recent activity notes:\n" + ("\n".join(history) if history else "(none)")
        )
        result = await self.provider.chat(
            messages=[
                {
                    "role": "system",
                    "content": "You are deciding whether to proactively message the user right now. "
                    "Only choose 'run' if there is something genuinely useful and timely to raise "
                    "(a reminder, a follow-up, a due item). When in doubt, skip. "
                    "Call the heartbeat_decision tool.",
                },
                {"role": "user", "content": context},
            ],
            tools=_HEARTBEAT_TOOL,
            model=self.model,
        )
        if not result.has_tool_calls:
            return HeartbeatDecision(action="skip")
        args = result.tool_calls[0].arguments
        action = str(args.get("action") or "skip")
        return HeartbeatDecision(action=action, note=str(args.get("note") or ""))

    async def run_once(self, user_id: str) -> HeartbeatDecision:
        """Run one check for a user; fires a proactive turn when the decision is 'run'."""
        decision = await self.decide(user_id)
        if decision.action == "run":
            session = await self.sessions.create(user_id, title="🔔 Proactive check-in")
            prompt = decision.note or "Proactively check in with the user about anything pending."
            await self.handler(user_id, session.id, prompt)
        return decision

    async def _loop(self) -> None:
        while self._running:
            try:
                due = await self.users.heartbeat_due(datetime.now(timezone.utc))
                for user in due:
                    if user.id in self._firing:
                        continue
                    self._firing.add(user.id)
                    asyncio.create_task(self._fire(user.id, user.heartbeat_interval_seconds))
            except Exception:
                logger.exception("Heartbeat tick failed")
            await asyncio.sleep(_MAX_SLEEP)

    async def _fire(self, user_id: str, interval: int) -> None:
        try:
            await self.run_once(user_id)
        except Exception:
            logger.exception("Heartbeat run failed for {}", user_id)
        finally:
            self._firing.discard(user_id)
            next_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, interval))
            await self.users.set_heartbeat(user_id, interval, next_at)
