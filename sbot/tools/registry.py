"""Tool registry with validation, per-bot restriction, and audit hooks."""

from collections.abc import Callable, Iterable
from typing import Any

from loguru import logger

from sbot.tools.base import Tool

_RETRY_HINT = "\n\n[Analyze the error above and try a different approach.]"

# Tools a bot's own `tool_allowlist` cannot take away. Two groups, one reason
# each: the agent's own machinery (its memory, its plan, reading the skills it
# was given) is not a capability grant — a bot restricted to `web_search` still
# has to be able to remember things, or per-bot memory stops working. And the
# Chief of Staff orchestration tools are granted by the bot's *kind*, which is
# already checked before they are registered at all; a CoS that also carried an
# allowlist would otherwise lose the ability to delegate.
ALWAYS_AVAILABLE_TOOLS = frozenset({
    "remember",
    "recall_memory",
    "update_plan",
    "read_skill",
    "save_blueprint",
    "publish_artifact",
    "list_bots",
    "create_bot",
    "delegate",
    "delegate_many",
    "mission_plan",
    "mission_start",
    "mission_status",
})

AuditHook = Callable[[str, dict[str, Any], str], None]


class ToolRegistry:
    def __init__(self, on_execute: AuditHook | None = None):
        self._tools: dict[str, Tool] = {}
        self._on_execute = on_execute
        self._allowed: frozenset[str] | None = None

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def restrict_to(self, names: Iterable[str] | None) -> None:
        """Limit this registry to a bot's allowlisted tools (PRD §3.1).

        Applied when the tool set is *read*, not when it is built, for two
        reasons. Tools registered later — MCP connector tools are synced into
        the registry mid-turn — are covered too, so an allowlist can't be
        widened by something arriving after construction. And because agents
        are cached per (user, bot), a turn can re-apply the bot's current
        allowlist cheaply instead of the cache serving a boundary that was
        edited hours ago.

        `None` means unrestricted, which is what a bot with no allowlist gets.
        An empty list is not the same thing: it means "this bot only talks".
        """
        self._allowed = None if names is None else frozenset(names) | ALWAYS_AVAILABLE_TOOLS

    def _is_allowed(self, name: str) -> bool:
        return self._allowed is None or name in self._allowed

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name) if self._is_allowed(name) else None

    def has(self, name: str) -> bool:
        """Can the current bot call `name` this turn? Allowlist-aware."""
        return name in self._tools and self._is_allowed(name)

    def is_registered(self, name: str) -> bool:
        """Is `name` occupied, regardless of who may call it?

        The question a registrant asks, and deliberately not the one `has`
        answers: a name filtered out by the current bot's allowlist is still
        taken, and treating it as free lets a later registration overwrite the
        tool holding it (see connectors._register_scoped, where the loser would
        be an admin-global connector and the winner a user's own).
        """
        return name in self._tools

    @property
    def tool_names(self) -> list[str]:
        return [name for name in self._tools if self._is_allowed(name)]

    def get_definitions(self) -> list[dict[str, Any]]:
        return [
            tool.to_schema() for name, tool in self._tools.items() if self._is_allowed(name)
        ]

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        progress: Callable[[dict], None] | None = None,
        deadline: float | None = None,
    ) -> str:
        if not self._is_allowed(name):
            # Never offered in this turn's definitions, so reaching here means the
            # model named it from memory of an earlier turn or invented it. Answered
            # as a tool result rather than an exception so the turn continues.
            result = (
                f"Error: tool '{name}' is not available to this bot. "
                f"Available: {', '.join(self.tool_names)}"
            )
            self._audit(name, params, result)
            return result
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: tool '{name}' not found. Available: {', '.join(self.tool_names)}"
        errors = tool.validate_params(params)
        if errors:
            result = f"Error: invalid parameters for '{name}': " + "; ".join(errors) + _RETRY_HINT
            self._audit(name, params, result)
            return result
        # Only tools that opt in receive the progress callback / turn deadline
        # (kept out of the normal param set so validation and every other tool
        # are unaffected).
        call_kwargs = dict(params)
        if progress is not None and getattr(tool, "wants_progress", False):
            call_kwargs["progress"] = progress
        if deadline is not None and getattr(tool, "wants_deadline", False):
            call_kwargs["deadline"] = deadline
        try:
            result = await tool.execute(**call_kwargs)
        except Exception as exc:
            logger.warning("Tool {} failed: {}", name, exc)
            result = f"Error executing {name}: {exc}" + _RETRY_HINT
        self._audit(name, params, result)
        return result

    def _audit(self, name: str, params: dict[str, Any], result: str) -> None:
        if self._on_execute is None:
            return
        try:
            self._on_execute(name, params, result)
        except Exception:
            logger.exception("Tool audit hook failed for {}", name)
