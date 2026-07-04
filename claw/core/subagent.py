"""Subagents: delegate a self-contained task to an isolated agent loop.

A subagent gets its own tool registry (read/write files, shell, web) scoped to
the same workspace, runs to completion with a capped iteration budget, and
returns just its final text. It shares the parent's provider and sandbox but
has no access to the parent's conversation — isolation keeps context small and
prevents a runaway helper from touching the main thread.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from claw.core.loop import AgentLoop
from claw.providers.base import LLMProvider, ProviderError
from claw.sandbox.ephemeral import EphemeralSandbox
from claw.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from claw.tools.registry import ToolRegistry
from claw.tools.shell import ExecTool
from claw.tools.web import WebFetchTool, WebSearchTool

_SUBAGENT_SYSTEM = (
    "You are a Claw subagent: a focused worker running one delegated task to completion. "
    "You cannot ask the user questions — work autonomously with the tools available and "
    "return a complete, self-contained result. Be concise and factual."
)


class SubagentManager:
    def __init__(
        self,
        provider: LLMProvider,
        sandbox: EphemeralSandbox,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        max_tokens: int = 4096,
        max_concurrent: int = 4,
    ):
        self.provider = provider
        self.sandbox = sandbox
        self.workspace = workspace
        self.model = model
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self._sem = asyncio.Semaphore(max_concurrent)

    def _build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        for tool_cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            tools.register(tool_cls(self.workspace))
        tools.register(ExecTool(self.sandbox, self.workspace))
        tools.register(WebFetchTool())
        tools.register(WebSearchTool())
        return tools

    async def run(self, task: str, context: str = "") -> str:
        """Run one subagent task; returns its final text (or an error note)."""
        async with self._sem:
            loop = AgentLoop(
                provider=self.provider,
                tools=self._build_tools(),
                model=self.model,
                max_iterations=self.max_iterations,
                max_tokens=self.max_tokens,
            )
            prompt = task if not context else f"{task}\n\nContext:\n{context}"
            messages = [
                {"role": "system", "content": _SUBAGENT_SYSTEM},
                {"role": "user", "content": prompt},
            ]
            turn_id = f"sub-{uuid.uuid4().hex[:8]}"
            try:
                outcome = await loop.run_turn(turn_id, messages, lambda _ev: None)
            except ProviderError as exc:
                logger.warning("Subagent failed: {}", exc)
                return f"Subagent error: {exc}"
            if outcome.reached_max_iterations and not outcome.final_content:
                return "Subagent reached its step limit without a final answer."
            return outcome.final_content or "Subagent produced no output."
