"""Streaming agent loop.

One turn = stream LLM → forward deltas → execute tool calls → iterate.
The loop owns no locks and no channel knowledge; the runtime schedules turns
per session and adapters consume events from the bus.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from claw.core.events import (
    AgentEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolFinished,
    ToolProgress,
    ToolStarted,
)
from claw.providers.base import ChatResult, LLMProvider, TextDelta, ThinkingDelta
from claw.tools.registry import ToolRegistry

Emit = Callable[[AgentEvent], None]

# Guards a tool call before execution. Returns (possibly-masked args, block_message).
# When block_message is not None the tool is not run and the message is fed back.
ArgGuard = Callable[[str, dict[str, Any]], tuple[dict[str, Any], str | None]]

# Ask-mode confirmation gate: (turn_id, tool_name, args_preview) -> approved.
ConfirmFn = Callable[[str, str, str], Awaitable[bool]]

# Tools gated behind a user confirmation when the session's permission mode is
# "ask": ones that touch the sandbox / run arbitrary code (`exec`), and ones
# that launch autonomous multi-step agents which can themselves run those tools
# with no further prompt (`workflow`, `spawn`) — so the gate can't be bypassed
# by delegating. `workflow` especially is long-running and expensive, so
# confirming before it starts is what the user expects.
UNSAFE_TOOLS = {"exec", "workflow", "spawn"}

_PREVIEW_CHARS = 200

# Directories/suffixes we never surface as artifacts when scanning for files an
# `exec` command created (build/cache noise, VCS internals, hidden dotfiles).
_ARTIFACT_IGNORE_DIRS = {"__pycache__", "node_modules"}
_ARTIFACT_IGNORE_SUFFIXES = {".pyc", ".pyo"}


def _snapshot_workspace(workspace: Path) -> dict[str, float]:
    """Map workspace-relative file path -> mtime, skipping cache/VCS/hidden files.

    Used to detect files an `exec` command creates or modifies (e.g. a chart a
    Python snippet writes with matplotlib), which the file-writing tools don't
    track. Kept cheap: a single tree walk of the user's workspace.
    """
    snap: dict[str, float] = {}
    if not workspace.exists():
        return snap
    for p in workspace.rglob("*"):
        rel = p.relative_to(workspace)
        if any(part.startswith(".") or part in _ARTIFACT_IGNORE_DIRS for part in rel.parts):
            continue
        if p.suffix in _ARTIFACT_IGNORE_SUFFIXES or not p.is_file():
            continue
        try:
            snap[str(rel)] = p.stat().st_mtime
        except OSError:
            continue
    return snap


@dataclass(slots=True)
class TurnOutcome:
    final_content: str | None
    new_messages: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)
    reached_max_iterations: bool = False
    # Workspace-relative paths of files the agent created/edited this turn, so
    # the UI can offer them as downloadable/openable artifacts.
    artifacts: list[str] = field(default_factory=list)


def _args_preview(arguments: dict[str, Any]) -> str:
    text = json.dumps(arguments, ensure_ascii=False)
    return text[:_PREVIEW_CHARS] + ("…" if len(text) > _PREVIEW_CHARS else "")


class AgentLoop:
    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        model: str | None = None,
        max_iterations: int = 30,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        arg_guard: ArgGuard | None = None,
        workspace: Path | None = None,
    ):
        self.provider = provider
        self.tools = tools
        self.model = model
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.arg_guard = arg_guard
        self.workspace = workspace

    async def run_turn(
        self,
        turn_id: str,
        messages: list[dict[str, Any]],
        emit: Emit,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        permission_mode: str = "auto",
        confirm: ConfirmFn | None = None,
    ) -> TurnOutcome:
        """Run one user turn. Mutates a copy of `messages`; returns appended messages.

        `model`/`api_key`/`api_base` override the loop defaults for this turn only
        (per-chat model selection), falling back to the agent's configured model.
        """
        working = list(messages)
        base_len = len(working)
        usage_total: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        effective_model = model or self.model
        # Files the agent wrote/edited this turn (deduped, in order), surfaced to
        # the user as openable artifacts. `baseline` lets us also detect files an
        # `exec` command created (e.g. a saved chart) by diffing the workspace.
        artifacts: list[str] = []
        baseline = _snapshot_workspace(self.workspace) if self.workspace is not None else {}

        for _iteration in range(self.max_iterations):
            result: ChatResult | None = None
            async for event in self.provider.stream_chat(
                working,
                tools=self.tools.get_definitions(),
                model=effective_model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                api_key=api_key,
                api_base=api_base,
            ):
                if isinstance(event, TextDelta):
                    emit(TextDeltaEvent(turn_id=turn_id, text=event.text))
                elif isinstance(event, ThinkingDelta):
                    emit(ThinkingDeltaEvent(turn_id=turn_id, text=event.text))
                elif isinstance(event, ChatResult):
                    result = event

            if result is None:
                raise RuntimeError("provider stream ended without a result")
            for key in usage_total:
                usage_total[key] += result.usage.get(key, 0)

            if not result.has_tool_calls:
                working.append({"role": "assistant", "content": result.content or ""})
                return TurnOutcome(
                    final_content=result.content,
                    new_messages=working[base_len:],
                    usage=usage_total,
                    artifacts=artifacts,
                )

            working.append(
                {
                    "role": "assistant",
                    "content": result.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in result.tool_calls
                    ],
                }
            )
            for tc in result.tool_calls:
                args_preview = _args_preview(tc.arguments)
                emit(ToolStarted(turn_id=turn_id, tool=tc.name, args_preview=args_preview))
                logger.info("Tool call: {}({})", tc.name, args_preview)
                args = tc.arguments
                block_message: str | None = None
                # Ask-mode gate: pause for user approval before an unsafe tool.
                if (
                    permission_mode == "ask"
                    and tc.name in UNSAFE_TOOLS
                    and confirm is not None
                ):
                    approved = await confirm(turn_id, tc.name, args_preview)
                    if not approved:
                        block_message = "The user declined to run this action."
                if block_message is None and self.arg_guard is not None:
                    args, block_message = self.arg_guard(tc.name, tc.arguments)
                if block_message is not None:
                    tool_result = f"Error: {block_message}"
                else:
                    # Sub-step progress for long tools (workflow) → Execution panel.
                    def _progress(payload: dict, _name: str = tc.name) -> None:
                        emit(
                            ToolProgress(
                                turn_id=turn_id,
                                tool=_name,
                                label=str(payload.get("label") or ""),
                                stage=str(payload.get("stage") or ""),
                                index=int(payload.get("index") or 0),
                                total=int(payload.get("total") or 0),
                                status=str(payload.get("status") or "running"),
                            )
                        )

                    tool_result = await self.tools.execute(tc.name, args, progress=_progress)
                # Track files the agent created/edited (successful write_file /
                # edit_file) so the UI can offer them as artifacts.
                if (
                    tc.name in ("write_file", "edit_file")
                    and not tool_result.startswith("Error")
                    and isinstance(args, dict)
                    and args.get("path")
                ):
                    p = str(args["path"])
                    if p not in artifacts:
                        artifacts.append(p)
                # Files created/modified by a shell command (e.g. matplotlib
                # savefig) aren't captured above, so diff the workspace vs the
                # turn's baseline and surface anything new or freshly changed.
                elif (
                    tc.name == "exec"
                    and self.workspace is not None
                    and not tool_result.startswith("Error")
                ):
                    for rel, mtime in _snapshot_workspace(self.workspace).items():
                        if (rel not in baseline or mtime > baseline[rel]) and rel not in artifacts:
                            artifacts.append(rel)
                emit(
                    ToolFinished(
                        turn_id=turn_id,
                        tool=tc.name,
                        result_preview=tool_result[:_PREVIEW_CHARS],
                        is_error=tool_result.startswith("Error"),
                    )
                )
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": tool_result,
                    }
                )

        logger.warning("Turn {} reached max iterations ({})", turn_id, self.max_iterations)
        return TurnOutcome(
            final_content=None,
            new_messages=working[base_len:],
            usage=usage_total,
            reached_max_iterations=True,
            artifacts=artifacts,
        )
