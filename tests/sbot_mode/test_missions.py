"""Mission execution end to end (PRD §4.3 / goal G3).

test_mission_engine.py covers the scheduler against a fake executor. This
covers the other half: the service that resolves a node to a real bot, prompts
it, and charges its tokens — plus the ownership checks on the way in, since a
mission plan is written by an LLM.
"""

import asyncio
import json
from typing import Any

import pytest

from sbot.config import LLMSettings, SandboxSettings, Settings
from sbot.core.mission_engine import InvalidGraphError
from sbot.core.missions import DEFAULT_BUDGET, MissionService
from sbot.providers.base import ChatResult, ToolCall
from sbot.tools.missions import (
    MissionPlanTool,
    MissionReplanTool,
    MissionStartTool,
    MissionStatusTool,
)
from tests.sbot_mode.conftest import FakeProvider, text_turn


def sent_text(messages: list[dict]) -> str:
    """Everything a call put in front of the model, prompts and tool results
    alike. Joined raw rather than JSON-dumped so a multi-line payload can be
    matched as itself."""
    return "\n".join(str(m.get("content") or "") for m in messages)


def make_service(
    stores, provider, tmp_path, max_parallel_nodes: int = 4, notifier=None
) -> MissionService:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=SandboxSettings(enabled=False),
        llm=LLMSettings(),
    )
    return MissionService(
        stores["missions"],
        stores["bots"],
        require_verified_results=False,  # Legacy text fixtures exercise scheduler behavior.
        provider=provider,
        sandbox=None,
        settings=settings,
        max_parallel_nodes=max_parallel_nodes,
        notifier=notifier,
    )


async def _two_specialists(stores, email: str):
    user = await stores["users"].get_or_create_by_email(email)
    researcher = await stores["bots"].create(
        owner_id=user.id, name="Researcher", role_title="Analyst", charter="หาข้อมูล"
    )
    writer = await stores["bots"].create(
        owner_id=user.id, name="Writer", role_title="Copywriter", charter="เขียนสรุป"
    )
    return user, researcher, writer


@pytest.mark.asyncio
async def test_mission_runs_its_graph_in_order_and_hands_results_downstream(stores, tmp_path):
    user, researcher, writer = await _two_specialists(stores, "graph@sbot.ai")
    provider = FakeProvider([
        text_turn("ตลาดโต 12% ต่อปี"),
        text_turn("บทสรุป: ตลาดกำลังโต"),
    ])
    service = make_service(stores, provider, tmp_path)

    mission = await service.plan(
        user.id,
        "ทำรายงานตลาด",
        [
            {"id": "research", "title": "หาข้อมูล", "bot_id": researcher.id, "instruction": "หาขนาดตลาด"},
            {
                "id": "write",
                "title": "เขียนสรุป",
                "bot_id": writer.id,
                "instruction": "เขียนสรุปจากข้อมูล",
                "depends_on": ["research"],
            },
        ],
    )
    assert await service.run_to_completion(mission.id, user.id) == "completed"

    status = await service.status(mission.id, user.id)
    assert status["status"] == "completed"
    assert {n["id"]: n["status"] for n in status["nodes"]} == {"research": "done", "write": "done"}

    # The writer must have been given the researcher's result — that handoff is
    # the whole point of an edge.
    assert "ตลาดโต 12%" in sent_text(provider.calls[1])
    # ...and the researcher could not have seen the writer's, since it ran first.
    assert "บทสรุป" not in sent_text(provider.calls[0])

    # Tokens are charged back so the budget means something.
    assert status["spent"]["tokens"] > 0


@pytest.mark.asyncio
async def test_plan_rejects_a_node_assigned_to_another_users_bot(stores, tmp_path):
    """The plan comes from an LLM, so a bot_id in it is untrusted input. Without
    the owner check, a mission could put another tenant's bot to work."""
    victim, victim_bot, _ = await _two_specialists(stores, "victim@sbot.ai")
    attacker = await stores["users"].get_or_create_by_email("attacker@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path)

    with pytest.raises(InvalidGraphError, match="unknown bot"):
        await service.plan(
            attacker.id,
            "borrow someone else's specialist",
            [{"id": "a", "title": "a", "bot_id": victim_bot.id, "instruction": "do it"}],
        )
    assert await stores["missions"].list_missions(attacker.id) == []
    assert await stores["missions"].list_missions(victim.id) == []


@pytest.mark.asyncio
async def test_plan_rejects_an_unexecutable_graph_before_it_can_run(stores, tmp_path):
    user, bot, _ = await _two_specialists(stores, "badplan@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path)

    with pytest.raises(InvalidGraphError):
        await service.plan(user.id, "no nodes", [])
    with pytest.raises(InvalidGraphError, match="no bot_id"):
        await service.plan(user.id, "unassigned", [{"id": "a", "title": "a", "instruction": "x"}])
    with pytest.raises(InvalidGraphError, match="unknown node"):
        await service.plan(
            user.id,
            "dangling edge",
            [{"id": "a", "title": "a", "bot_id": bot.id, "instruction": "x", "depends_on": ["ghost"]}],
        )
    # A rejected plan must not leave a mission behind for the scheduler to find.
    assert [m.status for m in await stores["missions"].list_missions(user.id)] == ["failed"]


@pytest.mark.asyncio
async def test_a_mission_planned_without_a_budget_still_has_a_ceiling(stores, tmp_path):
    """Missions are started by an LLM and run unattended, so "no budget given"
    cannot mean "spend without limit"."""
    user, bot, _ = await _two_specialists(stores, "budget@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path)

    mission = await service.plan(
        user.id, "unbounded?", [{"id": "a", "title": "a", "bot_id": bot.id, "instruction": "x"}]
    )
    assert mission.budget == DEFAULT_BUDGET
    assert mission.budget["max_tokens"] > 0


@pytest.mark.asyncio
async def test_a_partial_budget_keeps_the_ceilings_it_did_not_mention(stores, tmp_path):
    """The caller's budget used to replace the defaults outright, so asking for
    a *tighter* token cap removed the wall-clock and attempt limits with it — a
    graph that loops then runs until the process dies. Merging is what makes a
    budget a restriction rather than a trade."""
    user, bot, _ = await _two_specialists(stores, "partial@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path)

    mission = await service.plan(
        user.id,
        "tighter tokens only",
        [{"id": "a", "title": "a", "bot_id": bot.id, "instruction": "x"}],
        budget={"max_tokens": 1_000},
    )

    assert mission.budget["max_tokens"] == 1_000
    assert mission.budget["max_wall_seconds"] == DEFAULT_BUDGET["max_wall_seconds"]
    assert mission.budget["max_node_attempts"] == DEFAULT_BUDGET["max_node_attempts"]


@pytest.mark.asyncio
async def test_a_budget_limit_nothing_enforces_is_refused(stores, tmp_path):
    """`max_cost` was read by the scheduler but never fed: the specialist runner
    reports tokens only, so `spent["cost"]` stays 0 and a dollar ceiling never
    fires. Silently accepting it is the worst outcome — the caller believes the
    mission is capped."""
    user, bot, _ = await _two_specialists(stores, "deadkey@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path)
    nodes = [{"id": "a", "title": "a", "bot_id": bot.id, "instruction": "x"}]

    with pytest.raises(InvalidGraphError, match="max_cost"):
        await service.plan(user.id, "cap my spend", nodes, budget={"max_cost": 5})


@pytest.mark.asyncio
async def test_a_budget_value_the_scheduler_cannot_compare_is_refused(stores, tmp_path):
    """Budget arrives as a free-form dict off the HTTP body and every value is
    compared against accumulated spend inside the scheduler loop. A string there
    raises mid-run, which surfaces only as a crashed scheduler and a mission
    marked `failed`; a zero or negative ceiling is unenforceable in the other
    direction, since `if limit and ...` reads 0 as "no limit at all"."""
    user, bot, _ = await _two_specialists(stores, "badbudget@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path)
    nodes = [{"id": "a", "title": "a", "bot_id": bot.id, "instruction": "x"}]

    for bad in ({"max_tokens": "lots"}, {"max_tokens": 0}, {"max_wall_seconds": -1}):
        with pytest.raises(InvalidGraphError, match="positive number"):
            await service.plan(user.id, "bad budget", nodes, budget=bad)


@pytest.mark.asyncio
async def test_blackboard_carries_a_bulky_result_between_nodes(stores, tmp_path):
    """A node's own message is summarized on the way to its dependents, so
    anything large has to travel by key instead (PRD §3.5)."""
    user, researcher, writer = await _two_specialists(stores, "bb@sbot.ai")
    table = "| ปี | รายได้ |\n" + "\n".join(f"| 20{i:02d} | {i * 1000} |" for i in range(10, 30))

    provider = FakeProvider([
        [ChatResult(
            content=None,
            tool_calls=[ToolCall(
                id="w1",
                name="blackboard",
                arguments={"action": "write", "key": "revenue_table", "value": table},
            )],
        )],
        text_turn("เขียนตารางไว้ที่ key revenue_table แล้ว"),
        [ChatResult(
            content=None,
            tool_calls=[ToolCall(
                id="r1", name="blackboard", arguments={"action": "read", "key": "revenue_table"}
            )],
        )],
        text_turn("สรุปจากตาราง: รายได้โตต่อเนื่อง"),
    ])
    service = make_service(stores, provider, tmp_path)

    mission = await service.plan(
        user.id,
        "วิเคราะห์รายได้",
        [
            {"id": "research", "title": "ทำตาราง", "bot_id": researcher.id, "instruction": "ทำตารางรายได้"},
            {
                "id": "write",
                "title": "สรุป",
                "bot_id": writer.id,
                "instruction": "สรุปจากตาราง",
                "depends_on": ["research"],
            },
        ],
    )
    assert await service.run_to_completion(mission.id, user.id) == "completed"

    assert await stores["missions"].blackboard_read(mission.id, "revenue_table") == table
    # The writer's second call carries the tool result, so it really did receive
    # the full table rather than the summarized edge.
    assert table in sent_text(provider.calls[3])
    # And the key was advertised to it in the first place.
    assert "revenue_table" in sent_text(provider.calls[2])


@pytest.mark.asyncio
async def test_a_node_that_answers_with_nothing_is_not_reported_as_done(stores, tmp_path):
    """An empty result would satisfy its dependents, so the next step would run
    on nothing and the mission would 'succeed' having produced no work."""
    user, bot, _ = await _two_specialists(stores, "empty@sbot.ai")
    provider = FakeProvider([[ChatResult(content="")], [ChatResult(content="")], [ChatResult(content="")]])
    service = make_service(stores, provider, tmp_path)

    mission = await service.plan(
        user.id,
        "silent bot",
        [{"id": "a", "title": "a", "bot_id": bot.id, "instruction": "x", "max_attempts": 2}],
    )
    assert await service.run_to_completion(mission.id, user.id) == "failed"
    nodes = await stores["missions"].get_nodes(mission.id)
    assert nodes[0].status == "error"
    assert "no output" in nodes[0].output


@pytest.mark.asyncio
async def test_missions_are_owner_scoped_for_reading_starting_and_cancelling(stores, tmp_path):
    user, bot, _ = await _two_specialists(stores, "owner@sbot.ai")
    attacker = await stores["users"].get_or_create_by_email("thief@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path)

    mission = await service.plan(
        user.id, "private work", [{"id": "a", "title": "a", "bot_id": bot.id, "instruction": "x"}]
    )

    assert await service.status(mission.id, attacker.id) is None
    assert await service.start(mission.id, attacker.id) == "not_found"
    assert await service.cancel(mission.id, attacker.id) is False
    # Untouched: the failed cancel must not have moved the real owner's mission.
    assert (await service.status(mission.id, user.id))["status"] == "planned"


@pytest.mark.asyncio
async def test_cos_tools_plan_start_and_report_a_mission(stores, tmp_path):
    """The tool surface Chief of Staff actually sees: a plan it writes, a start
    that does not block the turn, and a status it can read back later."""
    user, researcher, writer = await _two_specialists(stores, "tools@sbot.ai")
    provider = FakeProvider([text_turn("ข้อมูลพร้อม"), text_turn("รายงานเสร็จ")])
    service = make_service(stores, provider, tmp_path)

    plan_tool = MissionPlanTool(service, user.id)
    result = await plan_tool.execute(
        goal="ทำรายงาน",
        nodes=[
            {"id": "r", "title": "หาข้อมูล", "bot_id": researcher.id, "instruction": "หา"},
            {"id": "w", "title": "เขียน", "bot_id": writer.id, "instruction": "เขียน", "depends_on": ["r"]},
        ],
    )
    assert "Mission created" in result
    mission_id = result.split("id: ")[1].split(")")[0]

    assert "running in the background" in await MissionStartTool(service, user.id).execute(mission_id)
    # start() must not block the turn; the mission finishes on its own task.
    assert await service._running[mission_id] == "completed"

    report = json.loads(await MissionStatusTool(service, user.id).execute(mission_id))
    assert report["status"] == "completed"
    assert [n["status"] for n in report["nodes"]] == ["done", "done"]

    listing = json.loads(await MissionStatusTool(service, user.id).execute())
    assert [m["id"] for m in listing] == [mission_id]


@pytest.mark.asyncio
async def test_a_bad_plan_is_reported_back_to_the_model_not_raised(stores, tmp_path):
    """A tool that raises kills the turn. Chief of Staff needs the validation
    error as text so it can correct the plan and try again."""
    user, bot, _ = await _two_specialists(stores, "retryplan@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path)

    result = await MissionPlanTool(service, user.id).execute(
        goal="cycle",
        nodes=[
            {"id": "a", "title": "a", "bot_id": bot.id, "instruction": "x", "depends_on": ["b"]},
            {"id": "b", "title": "b", "bot_id": bot.id, "instruction": "y", "depends_on": ["a"]},
        ],
    )
    assert result.startswith("Error:")
    assert "cycle" in result


@pytest.mark.asyncio
async def test_a_mission_stranded_by_a_restart_is_picked_back_up(stores, tmp_path):
    """The handle driving a mission lives in memory, so a restart leaves the row
    saying `running` with nobody scheduling it — and nothing else in the product
    ever notices, because every other path only reads status. A fresh service
    over the same database is exactly what a restarted process looks like.
    """
    user, researcher, _ = await _two_specialists(stores, "restart@sbot.ai")
    dead_process = make_service(stores, FakeProvider([]), tmp_path)
    mission = await dead_process.plan(
        user.id,
        "งานที่ค้างไว้",
        [{"id": "n", "title": "ทำงาน", "bot_id": researcher.id, "instruction": "ทำให้เสร็จ"}],
    )
    await stores["missions"].update_mission(mission.id, status="running")

    revived = make_service(stores, FakeProvider([text_turn("เสร็จแล้ว")]), tmp_path)
    assert await revived.resume_interrupted() == 1
    assert await revived._running[mission.id] == "completed"


@pytest.mark.asyncio
async def test_a_mission_orphaned_by_a_dead_worker_is_reclaimed_and_failed(stores, tmp_path):
    """A worker can die mid-node without the mission ever seeing `running`: the
    loop finds the node neither ready nor exhausted (it still says `running`
    from the dead process's claim) and settles the mission as `blocked` —
    which nothing else ever revisits. The startup sweep must pick this up the
    same way it picks up a stranded `running` mission, reclaim the expired
    lease, and let the now-exhausted node fail the mission for real.
    """
    user, researcher, _ = await _two_specialists(stores, "orphan@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path)
    mission = await service.plan(
        user.id,
        "งานที่ไม่มีใครดูแล้ว",
        [
            {
                "id": "n",
                "title": "ทำงาน",
                "bot_id": researcher.id,
                "instruction": "ทำ",
                "max_attempts": 1,
            }
        ],
    )
    # Simulate a crashed worker: claimed, one (and only) attempt spent, lease
    # already expired — exactly what a dead process leaves behind.
    claimed = await stores["missions"].claim_node(
        mission.id, "n", worker_id="dead-worker", lease_seconds=-1
    )
    assert claimed is not None and claimed.status == "running"
    await stores["missions"].update_mission(mission.id, status="blocked")

    revived = make_service(stores, FakeProvider([]), tmp_path)
    assert await revived.resume_interrupted() == 1
    assert await revived._running[mission.id] == "failed"


@pytest.mark.asyncio
async def test_a_mission_parked_on_a_real_gate_is_left_alone_by_the_sweep(stores, tmp_path):
    """A mission correctly parked on an open gate has nothing wrong with it —
    resuming it would just re-drive the engine to the same `blocked` outcome
    and re-report it to the user for no reason on every restart. The startup
    sweep must leave it untouched rather than "helpfully" re-running it.
    """
    reports: list[str] = []

    async def notifier(user_id: str, session_id: str, prompt: str) -> str:
        reports.append(prompt)
        return "reported"

    user, researcher, _ = await _two_specialists(stores, "gateresume@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path, notifier=notifier)
    mission = await service.plan(
        user.id,
        "งานที่ต้องรีวิว",
        [{"id": "g", "title": "รีวิว", "kind": "gate", "bot_id": researcher.id, "instruction": "-"}],
        session_id="sess-gate",
    )
    assert await service.run_to_completion(mission.id, user.id) == "blocked"
    assert len(reports) == 1

    revived = make_service(stores, FakeProvider([]), tmp_path, notifier=notifier)
    assert await revived.resume_interrupted() == 0
    assert mission.id not in revived._running
    # Not re-driven, so not re-reported either.
    assert len(reports) == 1
    mission_row = await revived.missions.get_mission(mission.id, user.id)
    assert mission_row is not None and mission_row.status == "blocked"
    node = (await revived.missions.get_nodes(mission.id))[0]
    assert node.status == "awaiting_human"


@pytest.mark.asyncio
async def test_a_settled_mission_is_not_resumed(stores, tmp_path):
    """The sweep is unscoped and runs on every boot, so anything it over-selects
    is charged to a user's budget for work that already finished."""
    user, researcher, _ = await _two_specialists(stores, "settled@sbot.ai")
    service = make_service(stores, FakeProvider([text_turn("ok")]), tmp_path)
    mission = await service.plan(
        user.id,
        "งานที่จบแล้ว",
        [{"id": "n", "title": "ทำงาน", "bot_id": researcher.id, "instruction": "ทำ"}],
    )
    assert await service.run_to_completion(mission.id, user.id) == "completed"

    assert await make_service(stores, FakeProvider([]), tmp_path).resume_interrupted() == 0


@pytest.mark.asyncio
async def test_a_step_that_fails_every_attempt_is_replanned_not_abandoned(stores, tmp_path):
    """PRD §4.3: an exhausted step used to fail the whole mission, because the
    engine's replan path had no hook wired to it. `retry` keeps the step's id, so
    its dependents still receive its result once it succeeds.
    """
    user, researcher, writer = await _two_specialists(stores, "replan@sbot.ai")
    provider = FakeProvider([
        [ChatResult(content=None)],  # the step's only attempt produces nothing
        [ChatResult(content=None, tool_calls=[ToolCall(
            id="p1",
            name="mission_replan",
            arguments={"retry": [{"id": "step", "instruction": "คำสั่งใหม่ที่ชัดเจนกว่าเดิม"}]},
        )])],
        text_turn("แก้แผนให้แล้ว"),
        text_turn("รายงานเสร็จ"),
    ])
    service = make_service(stores, provider, tmp_path)
    mission = await service.plan(
        user.id,
        "งานที่ต้องแก้แผน",
        [{
            "id": "step",
            "title": "ขั้นเดียว",
            "bot_id": researcher.id,
            "instruction": "คำสั่งเดิมที่ล้มเหลว",
            "max_attempts": 1,
        }],
    )

    assert await service.run_to_completion(mission.id, user.id) == "completed"

    node = (await stores["missions"].get_nodes(mission.id))[0]
    assert node.status == "done"
    assert node.instruction == "คำสั่งใหม่ที่ชัดเจนกว่าเดิม"

    # A replan runs outside a chat turn, so it is not granted `list_bots` — the
    # roster has to be in the prompt or reassigning a step means guessing ids.
    replan_prompt = sent_text(provider.calls[1])
    assert writer.id in replan_prompt  # not in the graph, so only the roster has it
    assert "คำสั่งเดิมที่ล้มเหลว" in replan_prompt


@pytest.mark.asyncio
async def test_the_replan_prompt_is_not_given_the_budget_notice(stores, tmp_path):
    """The replan prompt reserves answering in text as the "nothing can save
    this mission" signal, so it cannot also carry the budget notice's "always
    finish with your findings written out in your reply" — that instruction
    pushes the replanner straight into the give-up path and fails a mission
    that was still patchable. A node has no such reserved signal and keeps it.
    """
    user, researcher, _ = await _two_specialists(stores, "notice@sbot.ai")
    provider = FakeProvider([
        [ChatResult(content=None)],  # the step's only attempt produces nothing
        [ChatResult(content=None, tool_calls=[ToolCall(
            id="p1",
            name="mission_replan",
            arguments={"retry": [{"id": "step", "instruction": "คำสั่งใหม่"}]},
        )])],
        text_turn("แก้แผนให้แล้ว"),
        text_turn("รายงานเสร็จ"),
    ])
    service = make_service(stores, provider, tmp_path)
    mission = await service.plan(
        user.id,
        "งานที่ต้องแก้แผน",
        [{
            "id": "step",
            "title": "ขั้นเดียว",
            "bot_id": researcher.id,
            "instruction": "คำสั่งเดิม",
            "max_attempts": 1,
        }],
    )

    assert await service.run_to_completion(mission.id, user.id) == "completed"

    assert "# Budget" in sent_text(provider.calls[0])  # the node
    assert "# Budget" not in sent_text(provider.calls[1])  # the replan


@pytest.mark.asyncio
async def test_a_replan_that_changes_nothing_lets_the_mission_fail(stores, tmp_path):
    """The hook reports what the graph looks like afterwards, not what Chief of
    Staff said it did. A replanner that answers without patching anything would
    otherwise buy the loop another identical pass, once per allowed replan.
    """
    user, researcher, _ = await _two_specialists(stores, "noreplan@sbot.ai")
    provider = FakeProvider([
        [ChatResult(content=None)],
        text_turn("ทำอะไรไม่ได้แล้วครับ"),  # no tool call: the graph is untouched
    ])
    service = make_service(stores, provider, tmp_path)
    mission = await service.plan(
        user.id,
        "งานที่แก้ไม่ได้",
        [{
            "id": "step",
            "title": "ขั้นเดียว",
            "bot_id": researcher.id,
            "instruction": "ทำไม่ได้",
            "max_attempts": 1,
        }],
    )

    assert await service.run_to_completion(mission.id, user.id) == "failed"
    assert len(provider.calls) == 2  # the step, then one replan that gave up


@pytest.mark.asyncio
async def test_a_replan_patch_that_cannot_work_is_reported_back_to_the_model(stores, tmp_path):
    """The patch comes from a model, so every rejection has to arrive as text it
    can correct — a raise here would kill the turn instead."""
    user, researcher, _ = await _two_specialists(stores, "badpatch@sbot.ai")
    service = make_service(stores, FakeProvider([]), tmp_path)
    mission = await service.plan(
        user.id,
        "งาน",
        [{"id": "step", "title": "ขั้น", "bot_id": researcher.id, "instruction": "ทำ"}],
    )
    await stores["missions"].finish_node(mission.id, "step", status="error", output="ล้มเหลว")
    tool = MissionReplanTool(service, user.id)

    # Re-running the same step unchanged is exactly what already failed.
    assert "new instruction" in await tool.execute(
        mission_id=mission.id, retry=[{"id": "step"}]
    )
    # A step that is not part of this mission, and a mission that is not this user's.
    assert "not part of this mission" in await tool.execute(mission_id=mission.id, skip=["ghost"])
    other = await stores["users"].get_or_create_by_email("intruder@sbot.ai")
    assert "belongs to this user" in await MissionReplanTool(service, other.id).execute(
        mission_id=mission.id, skip=["step"]
    )
    assert "must skip, retry, or add" in await tool.execute(mission_id=mission.id)

    assert (await stores["missions"].get_nodes(mission.id))[0].status == "error"


# --------------------------------------------------------------------- gates
async def _gated_mission(service, stores, email: str):
    """research → review (gate) → write. The gate is the only thing standing
    between the two specialists, so whether `write` ran is a clean read on
    whether the gate held."""
    user, researcher, writer = await _two_specialists(stores, email)
    mission = await service.plan(
        user.id,
        "ทำรายงานที่ต้องให้คนตรวจก่อน",
        [
            {"id": "research", "title": "หาข้อมูล", "bot_id": researcher.id, "instruction": "หา"},
            {"id": "review", "title": "ให้คนตรวจ", "kind": "gate", "depends_on": ["research"]},
            {
                "id": "write",
                "title": "เขียน",
                "bot_id": writer.id,
                "instruction": "เขียน",
                "depends_on": ["review"],
            },
        ],
        session_id="sess-gate",
    )
    return user, mission


@pytest.mark.asyncio
async def test_a_gated_mission_blocks_and_then_finishes_once_the_user_approves(stores, tmp_path):
    """Before `resolve_gate` a gate was a dead end: the node parked as
    `awaiting_human`, the mission settled `blocked`, and the only way past was a
    replan skipping the very step someone had asked to review."""
    provider = FakeProvider([text_turn("ข้อมูลดิบ"), text_turn("รายงานฉบับสมบูรณ์")])
    service = make_service(stores, provider, tmp_path)
    user, mission = await _gated_mission(service, stores, "gate-approve@sbot.ai")

    assert await service.run_to_completion(mission.id, user.id) == "blocked"
    parked = {n["id"]: n["status"] for n in (await service.status(mission.id, user.id))["nodes"]}
    assert parked == {"research": "done", "review": "awaiting_human", "write": "pending"}
    assert len(provider.calls) == 1  # the writer must not have started

    assert await service.resolve_gate(mission.id, user.id, "review", True, note="ผ่านครับ") == "running"
    assert await service.run_to_completion(mission.id, user.id) == "completed"

    status = await service.status(mission.id, user.id)
    assert {n["id"]: n["status"] for n in status["nodes"]} == {
        "research": "done", "review": "done", "write": "done",
    }
    # The decision is recorded on the step, and the writer finally ran.
    assert [n["output"] for n in status["nodes"] if n["id"] == "review"] == ["ผ่านครับ"]
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_declining_a_gate_stops_the_mission_and_leaves_the_step_parked(stores, tmp_path):
    """A refusal must not be recorded as `skipped`: that status satisfies
    dependents, so restarting the mission would run exactly the work the user
    declined. Left parked instead, a restart blocks at the same gate."""
    provider = FakeProvider([text_turn("ข้อมูลดิบ"), text_turn("ไม่ควรได้เขียน")])
    service = make_service(stores, provider, tmp_path)
    user, mission = await _gated_mission(service, stores, "gate-decline@sbot.ai")
    assert await service.run_to_completion(mission.id, user.id) == "blocked"

    assert await service.resolve_gate(mission.id, user.id, "review", False, note="ยังไม่พร้อม") == "cancelled"

    status = await service.status(mission.id, user.id)
    assert status["status"] == "cancelled"
    by_id = {n["id"]: n for n in status["nodes"]}
    assert by_id["review"]["status"] == "awaiting_human"
    assert by_id["review"]["output"] == "ยังไม่พร้อม"
    assert by_id["write"]["status"] == "pending"
    assert len(provider.calls) == 1

    # And restarting it settles back on the same gate rather than past it.
    assert await service.run_to_completion(mission.id, user.id) == "blocked"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_a_gate_decision_is_rejected_unless_it_is_the_owners_and_the_step_is_parked(
    stores, tmp_path
):
    """The decision arrives from outside the scheduler — over HTTP, or relayed by
    a model — so every part of it is untrusted input."""
    provider = FakeProvider([text_turn("ข้อมูลดิบ")])
    service = make_service(stores, provider, tmp_path)
    user, mission = await _gated_mission(service, stores, "gate-guard@sbot.ai")
    assert await service.run_to_completion(mission.id, user.id) == "blocked"

    intruder = await stores["users"].get_or_create_by_email("gate-intruder@sbot.ai")
    with pytest.raises(InvalidGraphError, match="belongs to this user"):
        await service.resolve_gate(mission.id, intruder.id, "review", True)
    with pytest.raises(InvalidGraphError, match="not part of this mission"):
        await service.resolve_gate(mission.id, user.id, "ghost", True)
    # An ordinary finished step is not a review anyone was asked to make.
    with pytest.raises(InvalidGraphError, match="waiting for a review"):
        await service.resolve_gate(mission.id, user.id, "research", True)
    # Second approval of the same gate: the mission has already moved past it.
    assert await service.resolve_gate(mission.id, user.id, "review", True) == "running"
    await service.run_to_completion(mission.id, user.id)
    with pytest.raises(InvalidGraphError, match="waiting for a review"):
        await service.resolve_gate(mission.id, user.id, "review", True)


# -------------------------------------------------------------------- reports
@pytest.mark.asyncio
async def test_a_settled_mission_reports_itself_into_the_session_that_planned_it(stores, tmp_path):
    """A mission outlives the turn that planned it, so its result reached nobody:
    `session_id` was written to the row and never read again, and the user had to
    think to ask whether the team had finished."""
    reports: list[tuple[str, str, str]] = []

    async def notifier(user_id: str, session_id: str, prompt: str) -> str:
        reports.append((user_id, session_id, prompt))
        return "reported"

    provider = FakeProvider([text_turn("เสร็จแล้ว")])
    service = make_service(stores, provider, tmp_path, notifier=notifier)
    user, researcher, _ = await _two_specialists(stores, "report@sbot.ai")
    mission = await service.plan(
        user.id,
        "งานที่ต้องรายงาน",
        [{"id": "step", "title": "ขั้นเดียว", "bot_id": researcher.id, "instruction": "ทำ"}],
        session_id="sess-report",
    )
    assert await service.run_to_completion(mission.id, user.id) == "completed"

    assert len(reports) == 1
    reported_user, reported_session, prompt = reports[0]
    assert (reported_user, reported_session) == (user.id, "sess-report")
    # It must name the mission and send Chief of Staff to the status tool: the
    # prompt deliberately carries no results, so a report written without it
    # would be invented.
    assert mission.id in prompt
    assert "mission_status" in prompt
    assert "completed" in prompt


@pytest.mark.asyncio
async def test_a_blocked_mission_reports_the_review_it_is_waiting_on(stores, tmp_path):
    """The gate report is the one that matters: nothing else will ever move the
    mission, so a user who is not told stays blocked forever."""
    reports: list[str] = []

    async def notifier(user_id: str, session_id: str, prompt: str) -> str:
        reports.append(prompt)
        return "reported"

    provider = FakeProvider([text_turn("ข้อมูลดิบ")])
    service = make_service(stores, provider, tmp_path, notifier=notifier)
    user, mission = await _gated_mission(service, stores, "report-gate@sbot.ai")

    assert await service.run_to_completion(mission.id, user.id) == "blocked"
    assert len(reports) == 1
    assert "blocked" in reports[0]
    assert "mission_gate" in reports[0]

    # The user's own decision is not something to report back at them.
    await service.resolve_gate(mission.id, user.id, "review", False)
    assert len(reports) == 1


@pytest.mark.asyncio
async def test_a_mission_with_no_session_and_a_broken_notifier_still_settles(stores, tmp_path):
    """Reporting is best-effort: the outcome is already committed, so a deleted
    session or a provider that is down must not turn a completed mission into a
    crashed one."""
    calls: list[str] = []

    async def notifier(user_id: str, session_id: str, prompt: str) -> str:
        calls.append(session_id)
        raise RuntimeError("session is gone")

    provider = FakeProvider([text_turn("เสร็จ"), text_turn("เสร็จ")])
    service = make_service(stores, provider, tmp_path, notifier=notifier)
    user, researcher, _ = await _two_specialists(stores, "report-safe@sbot.ai")

    node = [{"id": "step", "title": "ขั้น", "bot_id": researcher.id, "instruction": "ทำ"}]
    detached = await service.plan(user.id, "ไม่มี session", node)
    assert await service.run_to_completion(detached.id, user.id) == "completed"
    assert calls == []  # nothing to report to

    attached = await service.plan(user.id, "มี session", node, session_id="sess-broken")
    assert await service.run_to_completion(attached.id, user.id) == "completed"
    assert calls == ["sess-broken"]


@pytest.mark.asyncio
async def test_a_mission_the_user_cancelled_is_not_reported_back_at_them(stores, tmp_path):
    """The report interrupts the session with an unprompted turn, so it has to be
    news. Cancelling is the user's own act — telling them about it would spend a
    turn saying what they just did."""
    reports: list[str] = []

    async def notifier(user_id: str, session_id: str, prompt: str) -> str:
        reports.append(prompt)
        return "reported"

    released = asyncio.Event()

    class BlockingProvider(FakeProvider):
        """Parks mid-call so the mission can be cancelled while a node is still
        in flight, which is the only way the engine settles one as `cancelled`."""

        def __init__(self) -> None:
            super().__init__([])
            self.entered = asyncio.Event()

        async def stream_chat(self, *args: Any, **kwargs: Any):
            self.entered.set()
            await released.wait()
            yield ChatResult(content="มาช้าไป")

    provider = BlockingProvider()
    service = make_service(stores, provider, tmp_path, notifier=notifier)
    user, researcher, _ = await _two_specialists(stores, "report-cancel@sbot.ai")
    mission = await service.plan(
        user.id,
        "งานที่ถูกยกเลิก",
        [{"id": "step", "title": "ขั้น", "bot_id": researcher.id, "instruction": "ทำ"}],
        session_id="sess-cancel",
    )
    assert await service.start(mission.id, user.id) == "running"
    await asyncio.wait_for(provider.entered.wait(), 5)
    assert await service.cancel(mission.id, user.id) is True
    released.set()

    assert await service.run_to_completion(mission.id, user.id) == "cancelled"
    assert reports == []


@pytest.mark.asyncio
async def test_mission_status_names_the_specialist_on_each_step(stores, tmp_path):
    """Status used to return raw bot ids, so answering "how is the researcher
    doing" meant a second `list_bots` round just to map them."""
    provider = FakeProvider([text_turn("ข้อมูลดิบ")])
    service = make_service(stores, provider, tmp_path)
    user, mission = await _gated_mission(service, stores, "names@sbot.ai")
    await service.run_to_completion(mission.id, user.id)

    by_id = {n["id"]: n for n in (await service.status(mission.id, user.id))["nodes"]}
    assert by_id["research"]["bot_name"] == "Researcher"
    assert by_id["write"]["bot_name"] == "Writer"
    # A gate is run by nobody, and the id it never had must not resolve to a name.
    assert by_id["review"]["bot_name"] == ""
    # The id stays alongside it: a step whose bot was archived is still traceable.
    assert by_id["research"]["bot_id"]


# ------------------------------------------------------------- active missions
async def _active(stores, user_id: str) -> list[dict]:
    """The /api/missions/active payload. Called directly rather than over HTTP:
    the endpoint only reads these two stores, and the repo has no API client
    fixture to justify standing one up for it."""
    from types import SimpleNamespace

    from sbot.api.manage import list_active_missions

    state = SimpleNamespace(missions=stores["missions"], bots=stores["bots"])
    return await list_active_missions(user=SimpleNamespace(id=user_id), state=state)


@pytest.mark.asyncio
async def test_active_missions_name_the_specialist_working_right_now(stores, tmp_path):
    """The bug this exists for: a mission runs detached from any chat turn, so a
    specialist busy inside one looked idle in the sidebar while Chief of Staff
    was telling the user it was working."""
    released = asyncio.Event()

    class BlockingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.entered = asyncio.Event()

        async def stream_chat(self, *args: Any, **kwargs: Any):
            self.entered.set()
            await released.wait()
            yield ChatResult(content="ผลวิจัย")

    provider = BlockingProvider()
    service = make_service(stores, provider, tmp_path)
    user, researcher, writer = await _two_specialists(stores, "active@sbot.ai")
    mission = await service.plan(
        user.id,
        "งานที่กำลังเดิน",
        [
            {"id": "research", "title": "วิจัย", "bot_id": researcher.id, "instruction": "หา"},
            {
                "id": "write",
                "title": "เขียน",
                "bot_id": writer.id,
                "instruction": "เขียน",
                "depends_on": ["research"],
            },
        ],
        session_id="sess-active",
    )
    assert await service.start(mission.id, user.id) == "running"
    await asyncio.wait_for(provider.entered.wait(), 5)

    rows = await _active(stores, user.id)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-active"
    assert (rows[0]["done"], rows[0]["total"]) == (0, 2)
    assert [(r["node_id"], r["bot_name"]) for r in rows[0]["running"]] == [("research", "Researcher")]

    released.set()
    assert await service.run_to_completion(mission.id, user.id) == "completed"
    # Gone once it settles: the sidebar reads this to decide who is busy, so a
    # finished mission left in the list would pin a spinner on forever.
    assert await _active(stores, user.id) == []


@pytest.mark.asyncio
async def test_a_mission_parked_on_a_gate_stays_active_and_says_what_it_waits_for(stores, tmp_path):
    """`blocked` reads like a finished state and is the opposite: nothing will
    move the mission until the user decides, so it is exactly what the UI has to
    keep showing."""
    provider = FakeProvider([text_turn("ข้อมูลดิบ"), text_turn("รายงาน")])
    service = make_service(stores, provider, tmp_path)
    user, mission = await _gated_mission(service, stores, "active-gate@sbot.ai")
    assert await service.run_to_completion(mission.id, user.id) == "blocked"

    rows = await _active(stores, user.id)
    assert len(rows) == 1
    assert rows[0]["status"] == "blocked"
    assert rows[0]["running"] == []  # nobody is working — it is waiting on a person
    assert [a["node_id"] for a in rows[0]["awaiting"]] == ["review"]
    assert (rows[0]["done"], rows[0]["total"]) == (1, 3)

    assert await service.resolve_gate(mission.id, user.id, "review", True) == "running"
    assert await service.run_to_completion(mission.id, user.id) == "completed"
    assert await _active(stores, user.id) == []


@pytest.mark.asyncio
async def test_active_missions_are_owner_scoped(stores, tmp_path):
    provider = FakeProvider([text_turn("ข้อมูลดิบ")])
    service = make_service(stores, provider, tmp_path)
    user, mission = await _gated_mission(service, stores, "active-owner@sbot.ai")
    attacker = await stores["users"].get_or_create_by_email("nosy@sbot.ai")
    assert await service.run_to_completion(mission.id, user.id) == "blocked"

    assert len(await _active(stores, user.id)) == 1
    assert await _active(stores, attacker.id) == []

@pytest.mark.asyncio
async def test_resume_exhausted_budget_never_claims_running_or_calls_model(stores, tmp_path):
    user, researcher, _ = await _two_specialists(stores, 'resume-budget@sbot.ai')
    provider = FakeProvider([])
    service = make_service(stores, provider, tmp_path)
    mission = await service.plan(user.id, 'Resume', [
        {'id': 'read', 'bot_id': researcher.id, 'instruction': 'Read'}], budget={'max_tokens': 100})
    await stores['missions'].update_mission(mission.id, status='paused', spent={'tokens': 120})
    tool = MissionStartTool(service, user.id)
    for _ in range(2):
        result = json.loads(await tool.execute(mission.id))
        assert result['status'] == 'paused' and result['started'] is False
        assert result['reason'] == 'max_tokens'
        assert result['spent']['tokens'] == 120
    assert not provider.calls and mission.id not in service._running
    saved = await stores['missions'].get_mission(mission.id, user.id)
    assert saved.status == 'paused' and saved.budget['max_tokens'] == 100


@pytest.mark.asyncio
async def test_explicit_resume_budget_preserves_spend_and_other_limits(stores, tmp_path):
    user, researcher, _ = await _two_specialists(stores, 'resume-explicit@sbot.ai')
    service = make_service(stores, FakeProvider([text_turn('done')]), tmp_path)
    mission = await service.plan(user.id, 'Resume', [
        {'id': 'read', 'bot_id': researcher.id, 'instruction': 'Read'}], budget={'max_tokens': 100, 'max_wall_seconds': 900})
    await stores['missions'].update_mission(mission.id, status='paused', spent={'tokens': 120})
    assert await service.start(mission.id, user.id, budget={'max_tokens': 500}) == 'running'
    assert await service._running[mission.id] == 'completed'
    saved = await stores['missions'].get_mission(mission.id, user.id)
    assert saved.spent['tokens'] >= 120
    assert saved.budget['max_tokens'] == 500
    assert saved.budget['max_wall_seconds'] == 900
