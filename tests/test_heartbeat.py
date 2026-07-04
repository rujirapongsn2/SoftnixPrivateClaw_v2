from typing import Any

from claw.core.heartbeat import HeartbeatService
from claw.providers.base import ChatResult, ToolCall
from tests.conftest import FakeProvider


class DecisionProvider(FakeProvider):
    """Returns a scripted heartbeat_decision tool call."""

    def __init__(self, action: str, note: str = ""):
        super().__init__([[ChatResult(
            content=None,
            tool_calls=[ToolCall(id="h", name="heartbeat_decision",
                                 arguments={"action": action, "note": note})],
        )]])


def _svc(stores, provider, fired):
    async def handler(user_id: str, session_id: str, prompt: str) -> str:
        fired.append((user_id, prompt))
        return "sent"

    return HeartbeatService(
        stores["users"], stores["memories"], stores["sessions"], provider, handler
    )


async def test_run_decision_fires_proactive_turn(stores):
    fired: list = []
    user = await stores["users"].get_or_create_by_email("h@x.y")
    await stores["memories"].set_core(user.id, "User has a dentist appointment tomorrow.")
    svc = _svc(stores, DecisionProvider("run", "Remind about the dentist appointment."), fired)

    decision = await svc.run_once(user.id)

    assert decision.action == "run"
    assert fired == [(user.id, "Remind about the dentist appointment.")]
    # A proactive session was created.
    sessions = await stores["sessions"].list_for_user(user.id)
    assert any(s.title.startswith("🔔") for s in sessions)


async def test_skip_decision_does_nothing(stores):
    fired: list = []
    user = await stores["users"].get_or_create_by_email("h2@x.y")
    svc = _svc(stores, DecisionProvider("skip"), fired)

    decision = await svc.run_once(user.id)

    assert decision.action == "skip"
    assert fired == []
    assert await stores["sessions"].list_for_user(user.id) == []


async def test_decide_defaults_to_skip_without_tool_call(stores):
    fired: list = []
    user = await stores["users"].get_or_create_by_email("h3@x.y")
    # Provider returns plain text, no tool call.
    svc = _svc(stores, FakeProvider([[ChatResult(content="hmm")]]), fired)

    decision = await svc.decide(user.id)
    assert decision.action == "skip"
