"""Ambient per-turn context for tools that need to know which session they're
running in, without threading `session_id` through every tool's signature.

Set once at the top of a turn (see AgentRuntime._process_turn) and read by
session-scoped tools like `update_plan`. A ContextVar — not an attribute on the
per-user agent — because one user can have several sessions running turns
concurrently (web + Telegram, two tabs, a scheduled run): they share the cached
per-user agent and its tool instances, so a plain attribute would race between
turns, whereas a ContextVar is isolated per async task and propagates across
`await` within that task.
"""

import asyncio
import contextvars
from collections.abc import Awaitable, Callable

current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "claw_current_session_id", default=None
)

current_turn_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sbot_current_turn_id", default=None
)

# `time.monotonic()` value past which the turn that is running has no time left.
# The agent loop only checks its own budget *between* iterations, so a tool that
# starts a nested agent (spawn, workflow) would otherwise be free to run its own
# full budget on top of the parent's. Tools that can outlive a turn read this and
# clamp themselves to what is left. None = no deadline in effect.
current_turn_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "claw_current_turn_deadline", default=None
)

# The UI language of the user whose turn is running. A tool whose result is
# shown to the user rather than only read by the model (delegate: a specialist's
# reply becomes a message row in the transcript) needs it to reach the message
# catalog. Not a constructor argument because tool instances live on the cached
# per-user agent and outlive any one turn, while the locale is per-request.
# None = fall back to i18n.DEFAULT_LOCALE, which is what a turn with no user
# behind it (a mission node) gets.
current_turn_locale: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "claw_current_turn_locale", default=None
)

# The root web turn installs this callback while it has a connected user who
# can decide on a confirmation card. Nested delegate/spawn loops inherit the
# context, so a specialist can ask that same user instead of silently creating
# a persistent project environment. Background missions have no callback and
# therefore remain fail-closed.
current_turn_confirmation: contextvars.ContextVar[
    Callable[[str, str, str], Awaitable[bool]] | None
] = contextvars.ContextVar("sbot_current_turn_confirmation", default=None)

# Project approvals for the active root turn. The scope is deliberately shared
# with nested delegate/spawn tasks through ContextVar propagation, so one goal
# does not prompt again merely because another team bot continues the same
# project. AgentRuntime installs a fresh scope per turn.
class ProjectApprovalScope:
    """Coordinate one project approval across every loop in a root turn."""

    def __init__(self) -> None:
        self.approved: set[str] = set()
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._lock = asyncio.Lock()

    def is_approved(self, slug: str) -> bool:
        return slug in self.approved

    async def request(
        self,
        slug: str,
        confirm: Callable[[], Awaitable[bool]],
    ) -> bool:
        """Run at most one confirmation callback for a slug; concurrent callers share it."""
        async with self._lock:
            if slug in self.approved:
                return True
            pending = self._pending.get(slug)
            owner = pending is None
            if owner:
                pending = asyncio.get_running_loop().create_future()
                self._pending[slug] = pending

        assert pending is not None
        if not owner:
            # One cancelled waiter must not cancel the decision awaited by the
            # owner and the other specialists.
            return await asyncio.shield(pending)

        try:
            approved = await confirm()
        except BaseException:
            async with self._lock:
                self._pending.pop(slug, None)
                if not pending.done():
                    pending.set_result(False)
            raise

        async with self._lock:
            if approved:
                self.approved.add(slug)
            self._pending.pop(slug, None)
            if not pending.done():
                pending.set_result(approved)
        return approved


current_project_approval_grants: contextvars.ContextVar[ProjectApprovalScope | None] = (
    contextvars.ContextVar("sbot_current_project_approval_grants", default=None)
)
