"""Working-plan mechanism: a per-session goal + step checklist the agent
maintains via the update_plan tool, pinned into the (never-trimmed) system
prompt so the thread survives long conversations.
"""

import pytest

from claw.core.context import ContextAssembler, render_plan
from claw.core.turn_context import current_session_id
from claw.db.stores import SessionStore
from claw.providers.base import ChatResult, ToolCall
from claw.tools.plan import PlanTool
from tests.conftest import FakeProvider, text_turn
from tests.test_runtime import make_runtime

# ---- render_plan ------------------------------------------------------------


def test_render_plan_empty_cases():
    assert render_plan(None) == ""
    assert render_plan({}) == ""
    assert render_plan({"goal": "", "steps": []}) == ""


def test_render_plan_formats_goal_and_steps():
    out = render_plan(
        {
            "goal": "Ship the report",
            "steps": [
                {"step": "Gather data", "status": "done"},
                {"step": "Draft", "status": "in_progress"},
                {"step": "Review", "status": "pending"},
            ],
        }
    )
    assert "**Goal:** Ship the report" in out
    assert "[x] Gather data" in out
    assert "[→] Draft" in out
    assert "[ ] Review" in out
    # Unknown/missing status falls back to pending marker, never crashes.
    assert render_plan({"goal": "g", "steps": [{"step": "x", "status": "bogus"}]}).endswith("[ ] x")


# ---- SessionStore.set_plan --------------------------------------------------


@pytest.mark.asyncio
async def test_set_plan_roundtrip(db_factory):
    sessions = SessionStore(db_factory)
    session = await sessions.create("user-1")
    steps = [{"step": "a", "status": "done"}, {"step": "b", "status": "pending"}]
    await sessions.set_plan(session.id, "My goal", steps)

    reloaded = await sessions.get(session.id)
    assert reloaded.plan == {"goal": "My goal", "steps": steps}


@pytest.mark.asyncio
async def test_set_plan_replaces_previous(db_factory):
    sessions = SessionStore(db_factory)
    session = await sessions.create("user-1")
    await sessions.set_plan(session.id, "v1", [{"step": "old", "status": "pending"}])
    await sessions.set_plan(session.id, "v2", [{"step": "new", "status": "done"}])
    reloaded = await sessions.get(session.id)
    assert reloaded.plan["goal"] == "v2"
    assert reloaded.plan["steps"] == [{"step": "new", "status": "done"}]


# ---- PlanTool ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_tool_persists_to_active_session(db_factory):
    sessions = SessionStore(db_factory)
    session = await sessions.create("user-1")
    tool = PlanTool(sessions)

    token = current_session_id.set(session.id)
    try:
        result = await tool.execute(
            goal="Build feature",
            steps=[
                {"step": "design", "status": "done"},
                {"step": "code", "status": "in_progress"},
                {"step": "", "status": "pending"},  # blank dropped
                "not-a-dict",  # ignored
                {"step": "test", "status": "weird"},  # status normalized
            ],
        )
    finally:
        current_session_id.reset(token)

    assert "Plan saved" in result
    plan = (await sessions.get(session.id)).plan
    assert plan["goal"] == "Build feature"
    assert plan["steps"] == [
        {"step": "design", "status": "done"},
        {"step": "code", "status": "in_progress"},
        {"step": "test", "status": "pending"},  # blank+non-dict removed, bad status → pending
    ]


@pytest.mark.asyncio
async def test_plan_tool_without_active_session_errors(db_factory):
    tool = PlanTool(SessionStore(db_factory))
    # No current_session_id set → tool must refuse, not raise.
    result = await tool.execute(goal="x", steps=[])
    assert result.startswith("Error")


# ---- the whole point: plan survives history trimming ------------------------


def test_pinned_plan_survives_trimming():
    """A long history gets trimmed to fit the budget, but the plan lives in the
    system prompt, which assemble() never trims — so it stays in context."""
    plan_block = render_plan({"goal": "Finish migration", "steps": [{"step": "run", "status": "in_progress"}]})
    system = f"# Claw Agent\n\n{plan_block}"
    # Tiny budget so history is aggressively trimmed.
    assembler = ContextAssembler(max_context_tokens=50)
    history = [
        {"role": "user", "content": "old message " * 50},
        {"role": "assistant", "content": "old reply " * 50},
        {"role": "user", "content": "another old " * 50},
        {"role": "assistant", "content": "another reply " * 50},
    ]
    current = {"role": "user", "content": "what was the goal again?"}
    out = assembler.assemble(system, history, current)

    assert out[0]["role"] == "system"
    assert "Finish migration" in out[0]["content"]  # plan pinned, not trimmed
    assert len(out) < 2 + len(history)  # some history was actually dropped


# ---- end-to-end through the runtime ----------------------------------------


@pytest.mark.asyncio
async def test_runtime_persists_plan_and_pins_it_next_turn(stores, tmp_path):
    """A turn where the model calls update_plan persists the plan to the
    session; the following turn finds it pinned into the system prompt."""
    # Turn 1: model calls update_plan, then (next iteration) answers.
    tool_call = [
        ChatResult(
            content=None,
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="update_plan",
                    arguments={
                        "goal": "Migrate the database",
                        "steps": [
                            {"step": "back up", "status": "done"},
                            {"step": "run migration", "status": "in_progress"},
                        ],
                    },
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
    ]
    provider = FakeProvider([tool_call, text_turn("Working on it."), text_turn("Still going.")])
    runtime = make_runtime(stores, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email("u@x.y")
    session = await stores["sessions"].create(user.id)

    # Capture bus events to assert the realtime plan_updated fires (drives the UI).
    events: list[dict] = []

    async def listen():
        async with runtime.bus.subscribe(session.id) as queue:
            while True:
                ev = (await queue.get()).to_dict()
                events.append(ev)
                if ev["type"] in ("turn_completed", "turn_error"):
                    return

    import asyncio

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    await runtime.handle_message(user.id, session.id, "migrate the db")
    await asyncio.wait_for(listener, 2)

    # Realtime event for the UI carries the full plan.
    plan_events = [e for e in events if e["type"] == "plan_updated"]
    assert len(plan_events) == 1
    assert plan_events[0]["goal"] == "Migrate the database"
    assert plan_events[0]["steps"][1]["status"] == "in_progress"

    # Plan persisted to the session.
    saved = (await stores["sessions"].get(session.id)).plan
    assert saved["goal"] == "Migrate the database"
    assert saved["steps"][1] == {"step": "run migration", "status": "in_progress"}

    # Next turn: the plan is pinned into that turn's system prompt.
    await runtime.handle_message(user.id, session.id, "continue")
    system_prompt_last_turn = provider.calls[-1][0]["content"]
    assert "Migrate the database" in system_prompt_last_turn
    assert "[→] run migration" in system_prompt_last_turn
