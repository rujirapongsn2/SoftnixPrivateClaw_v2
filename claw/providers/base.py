"""Streaming-first LLM provider interface.

A provider yields incremental events while the model responds:
- TextDelta / ThinkingDelta as tokens arrive (forwarded straight to the UI)
- a final ChatResult carrying complete tool calls, usage, and finish reason

Tool calls are only actionable once complete, so they are delivered on the
final result rather than as partial deltas.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class TextDelta:
    text: str


@dataclass(slots=True)
class ThinkingDelta:
    text: str


@dataclass(slots=True)
class ChatResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


ProviderEvent = TextDelta | ThinkingDelta | ChatResult


class ProviderError(Exception):
    """Raised when the provider call fails after retries.

    Errors are raised, never smuggled back as assistant content — content-shaped
    errors poison session history (legacy lesson).
    """


class LLMProvider(ABC):
    @abstractmethod
    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream a chat completion. Yields deltas, then exactly one ChatResult.

        `api_key`/`api_base` override the provider defaults per call, so a single
        provider instance can serve models from different admin-configured upstreams.
        """

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> ChatResult:
        """Non-streaming convenience wrapper (memory consolidation, heartbeat)."""
        async for event in self.stream_chat(
            messages, tools, model, max_tokens, temperature, api_key=api_key, api_base=api_base
        ):
            if isinstance(event, ChatResult):
                return event
        raise ProviderError("stream ended without a final result")

    @abstractmethod
    def count_tokens(self, messages: list[dict[str, Any]], model: str | None = None) -> int:
        """Best-effort token count for context budgeting."""
