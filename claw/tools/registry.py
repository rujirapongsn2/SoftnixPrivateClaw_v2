"""Tool registry with validation and audit hooks."""

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from claw.core.tool_errors import (
    Category,
    SideEffect,
    check_params,
    is_control_flow,
    tool_failure,
)
from claw.tools.base import Tool

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

    def get_registered(self, name: str) -> Tool | None:
        """Return the occupied object regardless of call permissions."""
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
            try:
                refusal = await self.gate(name, params)
            except Exception as exc:
                if is_control_flow(exc):
                    raise
                # Fail closed: a check that cannot answer does not let the tool run.
                refusal = tool_failure(
                    name,
                    Category.PRECHECK_FAILED,
                    "An internal check that runs before this tool could not complete.",
                    side_effect=SideEffect.NONE,
                    exc=exc,
                )
            if refusal:
                if audit:
                    self._audit(name, params, refusal)
                return refusal
        failure = check_params(tool, name, params)
        if failure is not None:
            if failure.failure.category is Category.INVALID_ARGUMENTS:
                on_validation_error = getattr(tool, "on_validation_error", None)
                if callable(on_validation_error):
                    try:
                        on_validation_error()
                    except Exception:
                        logger.warning("Tool {} validation refresh hook failed", name)
            if audit:
                self._audit(name, params, failure)
            return failure
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
            if is_control_flow(exc):
                raise
            result = tool_failure(
                name,
                Category.EXECUTION_FAILED,
                f"{type(exc).__name__}: {exc}",
                side_effect=SideEffect.UNKNOWN,
                exc=exc,
            )
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
