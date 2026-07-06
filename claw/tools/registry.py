"""Tool registry with validation and audit hooks."""

from collections.abc import Callable
from typing import Any

from loguru import logger

from claw.tools.base import Tool

_RETRY_HINT = "\n\n[Analyze the error above and try a different approach.]"

AuditHook = Callable[[str, dict[str, Any], str], None]


class ToolRegistry:
    def __init__(self, on_execute: AuditHook | None = None):
        self._tools: dict[str, Tool] = {}
        self._on_execute = on_execute

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

    def get_definitions(self) -> list[dict[str, Any]]:
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(
        self, name: str, params: dict[str, Any], progress: Callable[[dict], None] | None = None
    ) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: tool '{name}' not found. Available: {', '.join(self._tools)}"
        errors = tool.validate_params(params)
        if errors:
            result = f"Error: invalid parameters for '{name}': " + "; ".join(errors) + _RETRY_HINT
            self._audit(name, params, result)
            return result
        # Only tools that opt in receive the progress callback (kept out of the
        # normal param set so validation and every other tool are unaffected).
        call_kwargs = dict(params)
        if progress is not None and getattr(tool, "wants_progress", False):
            call_kwargs["progress"] = progress
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
