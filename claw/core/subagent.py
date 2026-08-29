"""Subagents: delegate a self-contained task to an isolated agent loop.

A subagent gets its own tool registry (read/write files, shell, web) scoped to
the same workspace, runs to completion with a capped iteration budget, and
returns just its final text. It shares the parent's provider and sandbox but
has no access to the parent's conversation — isolation keeps context small and
prevents a runaway helper from touching the main thread.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from claw.core.loop import AgentLoop
from claw.core.turn_context import current_turn_deadline
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

# A budget computed from remaining time must never round down into 0, which
# AgentLoop reads as "no cap at all" — the opposite of what the caller meant.
_MIN_RUN_SECONDS = 1.0

# Returned instead of starting (or finishing) a subagent whose parent turn has
# already spent its budget. Phrased as an instruction because the model reads it
# as a tool result and would otherwise just delegate the same task again.
_OUT_OF_TIME = "Subagent not started: this turn is out of time. Answer with what you already have."


@dataclass(slots=True)
class SubagentRun:
    """A subagent's output plus whether it actually finished the task.

    Callers that chain subagents need the distinction: every failure here is
    returned as ordinary prose, so a limit message reads exactly like a result
    and would otherwise be synthesized into a confident answer.
    """

    text: str
    ok: bool


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
        max_turn_seconds: float = 600,
    ):
        self.provider = provider
        self.sandbox = sandbox
        self.workspace = workspace
        self.model = model
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.max_turn_seconds = max_turn_seconds
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
        return (await self.run_result(task, context)).text

    async def run_result(
        self, task: str, context: str = "", max_turn_seconds: float | None = None
    ) -> SubagentRun:
        """Run one subagent task, reporting whether it finished or hit a limit.

        `max_turn_seconds` overrides this manager's budget for a single run, so a
        caller that chains several subagents can share one wall-clock budget
        between them instead of granting each the full amount.

        Whatever budget is asked for, the run is also clamped to the time left in
        the calling turn: the parent loop only checks its deadline between
        iterations, so an unclamped subagent would let one `spawn` call keep a
        turn (and the user's request) alive for a second full budget.
        """
        budget = (
            self.max_turn_seconds if max_turn_seconds is None else max(max_turn_seconds, _MIN_RUN_SECONDS)
        )
        deadline = current_turn_deadline.get()
        # Queueing behind other subagents burns the parent's clock too, so the
        # wait for a slot is bounded by the same deadline rather than unbounded.
        try:
            await asyncio.wait_for(
                self._sem.acquire(), self._remaining(deadline) if deadline is not None else None
            )
        except TimeoutError:
            return SubagentRun(_OUT_OF_TIME, ok=False)
        try:
            if deadline is not None:
                left = self._remaining(deadline)
                if left <= 0:
                    return SubagentRun(_OUT_OF_TIME, ok=False)
                budget = max(min(budget, left), _MIN_RUN_SECONDS)
            loop = AgentLoop(
                provider=self.provider,
                tools=self._build_tools(),
                model=self.model,
                max_iterations=self.max_iterations,
                max_tokens=self.max_tokens,
                max_turn_seconds=budget,
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
                return SubagentRun(f"Subagent error: {exc}", ok=False)
            if not outcome.final_content:
                # Say which limit stopped it: the caller decides whether to
                # re-delegate, and "produced no output" would invite retrying
                # the identical task that just ran out of time.
                if outcome.timed_out:
                    return SubagentRun(
                        "Subagent ran out of time before finishing. Delegate a smaller piece.",
                        ok=False,
                    )
                if outcome.reached_max_iterations:
                    return SubagentRun("Subagent reached its step limit without a final answer.", ok=False)
                return SubagentRun("Subagent produced no output.", ok=False)
            return SubagentRun(outcome.final_content, ok=True)
        finally:
            self._sem.release()

    @staticmethod
    def _remaining(deadline: float) -> float:
        return deadline - time.monotonic()
