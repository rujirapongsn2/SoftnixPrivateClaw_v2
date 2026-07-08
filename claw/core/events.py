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
class ToolProgress(AgentEvent):
    """Sub-step progress emitted from inside a long-running tool (e.g. the
    workflow's plan → step 1..N → synthesize stages), so the Execution panel can
    show a live checklist instead of one opaque spinning node."""

    turn_id: str
    tool: str
    label: str
    stage: str = ""  # plan | step | synthesize
    index: int = 0  # 1-based step number (0 when not a numbered step)
    total: int = 0
    status: str = "running"  # running | done | error
    type: str = field(default="tool_progress", init=False)


@dataclass(slots=True)
class PlanUpdated(AgentEvent):
    """The agent revised its working plan (via the update_plan tool). Carries the
    full current goal + step checklist so the Execution panel can show live
    progress, and so a client joining mid-turn gets the latest plan at once."""

    turn_id: str
    goal: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    type: str = field(default="plan_updated", init=False)


@dataclass(slots=True)
class ToolConfirmRequest(AgentEvent):
    """Ask-mode: the agent wants to run a potentially unsafe tool and is waiting
    for the user to approve or decline. The client replies over the WS with
    {type: "tool_decision", request_id, approved}."""

    turn_id: str
    request_id: str
    tool: str
    args_preview: str
    type: str = field(default="tool_confirm_request", init=False)


@dataclass(slots=True)
class ToolConfirmResolved(AgentEvent):
    """A pending confirmation was answered (or timed out) — lets every connected
    client settle the card, not just the one that clicked."""

    turn_id: str
    request_id: str
    approved: bool
    type: str = field(default="tool_confirm_resolved", init=False)


@dataclass(slots=True)
class TurnCompleted(AgentEvent):
    turn_id: str
    content: str
    usage: dict[str, int] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    type: str = field(default="turn_completed", init=False)


@dataclass(slots=True)
class TurnError(AgentEvent):
    turn_id: str
    message: str
    type: str = field(default="turn_error", init=False)
