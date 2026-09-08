"""An intermediate delivery must not silently terminate a multi-step task."""
from unittest.mock import AsyncMock

import pytest

from sbot.core.loop import AgentLoop
from sbot.core.turn_context import current_session_id
from sbot.providers.base import ChatResult, ToolCall
from sbot.tools.plan import PlanTool
from sbot.tools.registry import ToolRegistry
from tests.sbot_mode.conftest import FakeProvider, text_turn


def plan(status):
    return [ChatResult(content=None, tool_calls=[ToolCall(
        id=f"plan-{status}", name="update_plan", arguments={
            "goal": "HTML then PowerPoint",
            "steps": [{"step": "HTML", "status": "done"},
                      {"step": "PowerPoint", "status": status}],
        },
    )])]


async def run(turns, **kwargs):
    store = AsyncMock()
    registry = ToolRegistry()
    registry.register(PlanTool(store))
    provider = FakeProvider(turns)
    token = current_session_id.set("session")
    try:
        outcome = await AgentLoop(provider, registry, **kwargs).run_turn(
            "turn", [{"role": "user", "content": "Make HTML first then PowerPoint"}], lambda e: None,
        )
    finally:
        current_session_id.reset(token)
    return outcome, provider, store


async def test_intermediate_final_continues_and_updates_persisted_plan():
    outcome, provider, store = await run([
        plan("pending"), text_turn("HTML ready. Shall I make PowerPoint?"),
        plan("done"), text_turn("Both delivered"),
    ])
    assert outcome.final_content == "Both delivered"
    assert len(provider.calls) == 4
    assert "unfinished" in provider.calls[2][-1]["content"]
    assert provider.calls[2][-1]["role"] == "system"
    assert not any(m["role"] in {"system", "user"} for m in outcome.new_messages)
    assert store.set_plan.call_args.args[2][-1]["status"] == "done"


@pytest.mark.parametrize("status", ["done", "waiting_for_user", "blocked"])
async def test_complete_or_explicitly_paused_plan_can_end(status):
    outcome, provider, store = await run([plan(status), text_turn("Status and reason")])
    assert outcome.final_content == "Status and reason"
    assert len(provider.calls) == 2
    assert store.set_plan.call_args.args[2][-1]["status"] == status


async def test_uncooperative_model_is_bounded_and_does_not_claim_success():
    outcome, provider, _ = await run([
        plan("in_progress"), text_turn("Done"), text_turn("Done"), text_turn("Done"),
    ])
    assert len(provider.calls) == 4
    assert outcome.finish_reason == "plan_incomplete"
    assert "⚠️" in outcome.final_content


async def test_continuation_respects_iteration_budget():
    outcome, provider, _ = await run([plan("pending"), text_turn("HTML ready")], max_iterations=2)
    assert outcome.reached_max_iterations
    assert len(provider.calls) == 2


async def test_no_current_plan_does_not_force_unrelated_work():
    outcome, provider, _ = await run([text_turn("Here is the status")])
    assert outcome.final_content == "Here is the status"
    assert len(provider.calls) == 1
