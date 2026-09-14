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
