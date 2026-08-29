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

import contextvars

current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "claw_current_session_id", default=None
)

# `time.monotonic()` value past which the turn that is running has no time left.
# The agent loop only checks its own budget *between* iterations, so a tool that
# starts a nested agent (spawn, workflow) would otherwise be free to run its own
# full budget on top of the parent's. Tools that can outlive a turn read this and
# clamp themselves to what is left. None = no deadline in effect.
current_turn_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "claw_current_turn_deadline", default=None
)
