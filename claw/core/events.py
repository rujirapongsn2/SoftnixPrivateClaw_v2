"""Typed agent events, streamed end-to-end from the loop to the UI.

Every event serializes to a flat JSON object with a `type` discriminator so
web, mobile, and channel adapters share one protocol.
"""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentEvent:
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["type"] = self.type  # type: ignore[attr-defined]
        return payload


@dataclass(slots=True)
class TurnStarted(AgentEvent):
    turn_id: str
    type: str = field(default="turn_started", init=False)


@dataclass(slots=True)
class TextDeltaEvent(AgentEvent):
    turn_id: str
    text: str
    type: str = field(default="text_delta", init=False)


@dataclass(slots=True)
class ThinkingDeltaEvent(AgentEvent):
    turn_id: str
    text: str
    type: str = field(default="thinking_delta", init=False)


@dataclass(slots=True)
class ToolStarted(AgentEvent):
    turn_id: str
    tool: str
    args_preview: str
    type: str = field(default="tool_started", init=False)


@dataclass(slots=True)
class ToolFinished(AgentEvent):
    turn_id: str
    tool: str
    result_preview: str
    is_error: bool = False
    type: str = field(default="tool_finished", init=False)


@dataclass(slots=True)
class TurnCompleted(AgentEvent):
    turn_id: str
    content: str
    usage: dict[str, int] = field(default_factory=dict)
    type: str = field(default="turn_completed", init=False)


@dataclass(slots=True)
class TurnError(AgentEvent):
    turn_id: str
    message: str
    type: str = field(default="turn_error", init=False)
