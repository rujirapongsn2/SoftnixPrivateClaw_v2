"""Streaming agent loop.

One turn = stream LLM → forward deltas → execute tool calls → iterate.
The loop owns no locks and no channel knowledge; the runtime schedules turns
per session and adapters consume events from the bus.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from claw.core.events import (
    AgentEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolFinished,
    ToolStarted,
)
from claw.providers.base import ChatResult, LLMProvider, TextDelta, ThinkingDelta
from claw.tools.registry import ToolRegistry

Emit = Callable[[AgentEvent], None]

# Guards a tool call before execution. Returns (possibly-masked args, block_message).
# When block_message is not None the tool is not run and the message is fed back.
ArgGuard = Callable[[str, dict[str, Any]], tuple[dict[str, Any], str | None]]

_PREVIEW_CHARS = 200


@dataclass(slots=True)
class TurnOutcome:
    final_content: str | None
    new_messages: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)
    reached_max_iterations: bool = False


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
    ):
        self.provider = provider
        self.tools = tools
        self.model = model
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.arg_guard = arg_guard

    async def run_turn(self, turn_id: str, messages: list[dict[str, Any]], emit: Emit) -> TurnOutcome:
        """Run one user turn. Mutates a copy of `messages`; returns appended messages."""
        working = list(messages)
        base_len = len(working)
        usage_total: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}

        for _iteration in range(self.max_iterations):
            result: ChatResult | None = None
            async for event in self.provider.stream_chat(
                working,
                tools=self.tools.get_definitions(),
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
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
                emit(ToolStarted(turn_id=turn_id, tool=tc.name, args_preview=_args_preview(tc.arguments)))
                logger.info("Tool call: {}({})", tc.name, _args_preview(tc.arguments))
                args = tc.arguments
                block_message: str | None = None
                if self.arg_guard is not None:
                    args, block_message = self.arg_guard(tc.name, tc.arguments)
                if block_message is not None:
                    tool_result = f"Error: {block_message}"
                else:
                    tool_result = await self.tools.execute(tc.name, args)
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
        )
