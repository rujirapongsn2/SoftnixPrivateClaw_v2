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
class MissionReported(AgentEvent):
    turn_id: str
    content: str
    artifacts: list[str] = field(default_factory=list)
    type: str = field(default="mission_reported", init=False)


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
class DelegationStarted(AgentEvent):
    """One bot handed a task to another and is waiting on it.

    Separate from `tool_started("delegate")` because that event only carries a
    truncated argument blob: the specialist is identified by whatever name the
    model happened to type, and the UI has no id to resolve an avatar with.
    Bot-to-bot work is the product, not a tool call, so it gets the identity of
    the *resolved* bot and the task verbatim.
    """

    turn_id: str
    delegation_id: str
    bot_id: str
    bot_name: str
    role_title: str
    task: str
    type: str = field(default="delegation_started", init=False)


@dataclass(slots=True)
class DelegationFinished(AgentEvent):
    """The specialist answered. `text` is the full reply, not a preview — it is
    shown as that bot's own message in the transcript, so truncating it here
    (as `tool_finished` must, being a debug preview) would cut the answer off
    mid-sentence.

    `delegation_id` pairs this with its `DelegationStarted`. Matching on
    `bot_id` alone cannot: the same bot may be delegated to twice in a turn,
    and a reconnect replays the turn, so neither the client nor the store can
    tell a repeat handoff from a replayed one without it.
    """

    turn_id: str
    delegation_id: str
    bot_id: str
    text: str
    # Files the specialist completed for this assignment. They are attached to
    # its own reply so the group transcript exposes a download immediately,
    # including after a reload.
    artifacts: list[str] = field(default_factory=list)
    is_error: bool = False
    type: str = field(default="delegation_finished", init=False)


@dataclass(slots=True)
class DelegationStep(AgentEvent):
    """One tool the specialist ran, reported under its own assignment card.

    Carries what `ToolProgress` cannot: which delegation it belongs to. The
    Execution panel keys a sub-step by its index into one shared list, so with
    `delegate_many` running several specialists at once there is no way back
    from a row to the bot that ran it. `detail` is the argument preview while
    running and the result preview once finished — the same two strings
    `tool_started`/`tool_finished` carry, kept on one row so the card shows a
    step rather than a pair of half-steps.
    """

    turn_id: str
    delegation_id: str
    bot_id: str
    index: int
    tool: str
    detail: str = ""
    status: str = "running"  # running | done | error
    type: str = field(default="delegation_step", init=False)


@dataclass(slots=True)
class DelegationDelta(AgentEvent):
    """A chunk of the specialist's reply as it is being written.

    Coalesced upstream rather than emitted per token: four specialists
    streaming at once through one bounded per-session queue would evict each
    other's tool events, and the replay buffer a reconnecting client is handed
    is bounded too. Losing a chunk is harmless — `delegation_finished` carries
    the whole reply and replaces whatever was previewed here.
    """

    turn_id: str
    delegation_id: str
    bot_id: str
    text: str
    type: str = field(default="delegation_delta", init=False)


@dataclass(slots=True)
class DelegatedTask(AgentEvent):
    """The instruction a leader handed this bot, sent to the bot's own thread.

    No other user message needs an event: the client that sent one renders its
    own text and the server never echoes it back. A delegated instruction has
    no such client — it is written by the leader, in a different session — so
    without this a user already watching the specialist's thread sees tools run
    and a reply arrive under no visible request.

    `turn_id` is what makes it safe to replay: the client already holds this
    turn's instruction and drops the repeat.
    """

    turn_id: str
    content: str
    delegated_by: str = ""
    type: str = field(default="delegated_task", init=False)


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
    # Set when an attached image was read by a separate vision model because the
    # chat model couldn't; "" on every ordinary turn.
    vision_model: str = ""
    coordinator_bot_id: str | None = None
    speaker_name: str = ""
    type: str = field(default="turn_completed", init=False)


@dataclass(slots=True)
class TurnError(AgentEvent):
    turn_id: str
    message: str
    type: str = field(default="turn_error", init=False)
