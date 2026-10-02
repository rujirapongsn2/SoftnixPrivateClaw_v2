"""Tool registry with validation and audit hooks."""

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from claw.tools.base import Tool

_RETRY_HINT = "\n\n[Analyze the error above and try a different approach.]"

AuditHook = Callable[[str, dict[str, Any], str], None]


def _offered(tool: Tool) -> bool:
    """A tool can stay registered but unadvertised while it has nothing to act on
    (e.g. no connector serves files), saving its schema's tokens on every call."""
    offered = getattr(tool, "offered", None)
    return offered() if callable(offered) else True


class ToolRegistry:
    def __init__(
        self,
        on_execute: AuditHook | None = None,
        gate: Callable[[str, dict[str, Any]], Awaitable[str | None]] | None = None,
        settled: AuditHook | None = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._on_execute = on_execute
        self.gate = gate
        self.settled = settled

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    def get_definitions(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        """Return schemas for all tools, or a task-scoped subset.

        Long artifact jobs use the subset form so each resumed segment does not
        repay the token cost of unrelated browser, scheduling, memory, and
        connector definitions.
        """
        return [
            tool.to_schema()
            for name, tool in self._tools.items()
            if (names is None or name in names) and _offered(tool)
        ]

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        progress: Callable[[dict], None] | None = None,
        deadline: float | None = None,
        audit: bool = True,
    ) -> str:
        """Run a tool. `audit=False` is for a tool that makes many internal calls on
        its own behalf and is itself audited once (e.g. chunked downloads)."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: tool '{name}' not found. Available: {', '.join(self._tools)}"
        if self.gate is not None:
            # A pre-execution policy check (e.g. the workspace quota). A non-empty
            # answer is the result the model sees instead of running the tool.
            refusal = await self.gate(name, params)
            if refusal:
                if audit:
                    self._audit(name, params, refusal)
                return refusal
        errors = tool.validate_params(params)
        if errors:
            result = f"Error: invalid parameters for '{name}': " + "; ".join(errors) + _RETRY_HINT
            if audit:
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
        if not audit:
            return result
        self._audit(name, params, result)
        if self.settled is not None:
            try:
                self.settled(name, params, result)
            except Exception:
                logger.exception("Tool settle hook failed for {}", name)
        return result

    def _audit(self, name: str, params: dict[str, Any], result: str) -> None:
        if self._on_execute is None:
            return
        try:
            self._on_execute(name, params, result)
        except Exception:
            logger.exception("Tool audit hook failed for {}", name)
