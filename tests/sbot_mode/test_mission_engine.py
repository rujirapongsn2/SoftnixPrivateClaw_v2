"""MissionEngine: DAG validation, scheduling invariants, and context economy.

These are regression tests for bugs that a naive scheduler reproduces very
easily, so each one asserts the invariant rather than the happy path:

* a node runs exactly once, even when ready nodes outnumber the parallel slots
* a mission whose node never succeeds ends `failed`, never `completed`
* a graph that cannot execute is rejected before it is persisted
"""

import asyncio
from collections import Counter

import pytest

from sbot.core.mission_engine import (
    CycleDetectedError,
    InvalidGraphError,
    MissionEngine,
    NodeContext,
    NodeResult,
    ReplanOutcome,
)
from sbot.db.models import MissionNode
from sbot.db.stores import MissionStore


async def _mission(stores, goal="goal", budget=None, email="mission@sbot.ai"):
    user = await stores["users"].get_or_create_by_email(email)
    return await stores["missions"].create_mission(owner_id=user.id, goal=goal, budget=budget)


# --------------------------------------------------------------- validate_dag
def test_validate_dag_accepts_a_chain():
    MissionEngine.validate_dag(
        [
            {"id": "A", "depends_on": []},
            {"id": "B", "depends_on": ["A"]},
            {"id": "C", "depends_on": ["B"]},
        ]
    )


def test_validate_dag_rejects_cycle():
    with pytest.raises(CycleDetectedError):
        MissionEngine.validate_dag(
            [
                {"id": "A", "depends_on": ["C"]},
                {"id": "B", "depends_on": ["A"]},
                {"id": "C", "depends_on": ["B"]},
            ]
        )


def test_validate_dag_rejects_self_loop():
    with pytest.raises(CycleDetectedError):
        MissionEngine.validate_dag([{"id": "A", "depends_on": ["A"]}])


def test_validate_dag_rejects_dangling_dependency():
    """The quiet one: a dangling edge is not a cycle, so it passes a naive
    check and then the dependent node can never become ready — the mission
    just sits there looking blocked with no reason given."""
    with pytest.raises(InvalidGraphError):
        MissionEngine.validate_dag(
            [{"id": "A", "depends_on": []}, {"id": "B", "depends_on": ["ghost"]}]
        )


def test_validate_dag_rejects_duplicate_ids():
    with pytest.raises(InvalidGraphError):
        MissionEngine.validate_dag(
            [{"id": "A", "depends_on": []}, {"id": "A", "depends_on": []}]
        )


@pytest.mark.asyncio
async def test_add_nodes_validates_against_existing_nodes(stores):
    """A replan appends nodes, so validation has to cover the union — otherwise
    the second call is where a cycle sneaks in."""
    mission = await _mission(stores, email="replan@sbot.ai")
    store: MissionStore = stores["missions"]
    await store.add_nodes(mission.id, [{"id": "A", "title": "a", "instruction": "i"}])

    with pytest.raises(InvalidGraphError):
        await store.add_nodes(mission.id, [{"id": "A", "title": "dup", "instruction": "i"}])
    with pytest.raises(InvalidGraphError):
        await store.add_nodes(
            mission.id, [{"id": "B", "title": "b", "instruction": "i", "depends_on": ["ghost"]}]
        )

    added = await store.add_nodes(
        mission.id, [{"id": "B", "title": "b", "instruction": "i", "depends_on": ["A"]}]
    )
    assert [n.id for n in added] == ["B"]


@pytest.mark.asyncio
async def test_add_nodes_rejects_a_duplicate_inside_one_batch(stores):
    """The batch is one LLM plan, so the second "research" has to be rejected as
    a bad graph — an IntegrityError escapes plan()'s cleanup as a 500."""
    mission = await _mission(stores, email="dupbatch@sbot.ai")
    with pytest.raises(InvalidGraphError):
        await stores["missions"].add_nodes(
            mission.id,
            [
                {"id": "research", "title": "a", "instruction": "i"},
                {"id": "research", "title": "b", "instruction": "i"},
            ],
        )


@pytest.mark.asyncio
async def test_node_ids_are_scoped_to_their_mission(stores):
    """Plans are written by an LLM and its node ids are semantic, so the second
    mission to call a step "research" is the normal case, not a collision. Every
    mutation is keyed by the pair, so one mission's copy cannot move another's.
    """
    store: MissionStore = stores["missions"]
    first = await _mission(stores, email="scope@sbot.ai")
    second = await _mission(stores, email="scope@sbot.ai")
    node = {"id": "research", "title": "หา", "instruction": "i", "max_attempts": 5}

    await store.add_nodes(first.id, [dict(node)])
    await store.add_nodes(second.id, [dict(node)])

    async def other() -> MissionNode:
        return (await store.get_nodes(second.id))[0]

    await store.finish_node(first.id, "research", status="error", output="ล้ม")
    assert (await other()).status == "pending" and (await other()).output is None

    # Both copies are now `error` and unleased, so the status predicates no
    # longer stand in for the key — only `mission_id` separates them.
    await store.finish_node(second.id, "research", status="error", output="ล้มของอีกภารกิจ")
    assert await store.revise_node(first.id, "research", instruction="ใหม่") is not None
    assert (await other()).instruction == "i" and (await other()).status == "error"

    assert await store.claim_node(first.id, "research", "w1", 60.0) is not None
    assert (await other()).attempts == 0 and (await other()).lease_owner is None
    # Claiming the same id under the other mission is a different node entirely.
    assert await store.claim_node(second.id, "research", "w1", 60.0) is not None


# ------------------------------------------------------------------ execution
@pytest.mark.asyncio
async def test_dag_order_and_blackboard_handoff(stores):
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, goal="เปิดตัวหน้าเว็บใหม่พร้อมเนื้อหาและ QA")

    await store.add_nodes(
        mission.id,
        [
            {"id": "node_seo", "title": "วิเคราะห์ Keyword", "instruction": "หา keyword", "depends_on": []},
            {"id": "node_ux", "title": "ออกแบบโครงสร้าง", "instruction": "Wireframe", "depends_on": []},
            {
                "id": "node_draft",
                "title": "เขียน Copywriting",
                "instruction": "เขียนบทความ",
                "depends_on": ["node_seo", "node_ux"],
            },
            {
                "id": "node_review",
                "title": "ตรวจสอบคุณภาพ",
                "instruction": "ตรวจ rubric",
                "depends_on": ["node_draft"],
            },
        ],
    )

    order: list[str] = []

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        order.append(node.id)
        if node.id == "node_seo":
            return NodeResult("done", "SEO keywords", {"keywords": "ai, agent, automation"})
        if node.id == "node_ux":
            return NodeResult("done", "UX structure", {"sections": "Hero, Features, Pricing"})
        if node.id == "node_draft":
            # Fan-in: both parents' writes must be visible.
            assert "keywords" in ctx.manifest
            assert "sections" in ctx.manifest
            assert set(ctx.parent_outputs) == {"node_seo", "node_ux"}
            return NodeResult("done", "Draft finished", {"draft_doc": "/workspaces/draft.md"})
        assert "draft_doc" in ctx.manifest
        return NodeResult("done", "Review passed", {"final_approval": True})

    engine = MissionEngine(store, executor, max_parallel_nodes=2)
    assert await engine.run_mission(mission.id) == "completed"

    assert order.index("node_seo") < order.index("node_draft")
    assert order.index("node_ux") < order.index("node_draft")
    assert order.index("node_draft") < order.index("node_review")

    assert await store.blackboard_read(mission.id, "keywords") == "ai, agent, automation"
    assert await store.blackboard_read(mission.id, "draft_doc") == "/workspaces/draft.md"
    assert await store.blackboard_read(mission.id, "final_approval") is True


@pytest.mark.asyncio
async def test_each_node_runs_exactly_once_when_ready_exceeds_slots(stores):
    """Regression: selecting a ready node is not the same as claiming it. A
    scheduler that creates tasks for every ready node but only admits N through
    a semaphore re-selects the queued ones on its next pass and runs them twice.
    """
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="parallel@sbot.ai")
    await store.add_nodes(
        mission.id,
        [{"id": f"n{i}", "title": f"n{i}", "instruction": "work"} for i in range(8)],
    )

    runs: Counter[str] = Counter()
    concurrent = 0
    peak = 0

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        nonlocal concurrent, peak
        runs[node.id] += 1
        concurrent += 1
        peak = max(peak, concurrent)
        try:
            await asyncio.sleep(0.02)  # long enough to be still running next pass
        finally:
            concurrent -= 1
        return NodeResult("done", f"{node.id} ok")

    engine = MissionEngine(store, executor, max_parallel_nodes=3)
    assert await engine.run_mission(mission.id) == "completed"

    assert runs == Counter({f"n{i}": 1 for i in range(8)})
    assert peak <= 3
    nodes = await store.get_nodes(mission.id)
    assert {n.attempts for n in nodes} == {1}
    assert all(n.lease_owner is None and n.lease_expires_at is None for n in nodes)


@pytest.mark.asyncio
async def test_failing_node_retries_then_mission_fails(stores):
    """Regression: the mission must not report `completed` while a node is in
    error, and the node must actually consume its retries."""
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="failing@sbot.ai")
    await store.add_nodes(
        mission.id,
        [
            {"id": "ok", "title": "ok", "instruction": "work"},
            {"id": "bad", "title": "bad", "instruction": "work", "max_attempts": 3},
            {"id": "after_bad", "title": "after", "instruction": "work", "depends_on": ["bad"]},
        ],
    )

    attempts_seen: list[int] = []
    errors_seen: list[str | None] = []

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        if node.id == "bad":
            attempts_seen.append(ctx.attempt)
            errors_seen.append(ctx.last_error)
            return NodeResult("error", f"boom #{ctx.attempt}")
        return NodeResult("done", "fine")

    engine = MissionEngine(store, executor, retry_base_delay=0.01)
    assert await engine.run_mission(mission.id) == "failed"

    assert attempts_seen == [1, 2, 3]
    # A retry that isn't told why it failed just reproduces the failure.
    assert errors_seen == [None, "boom #1", "boom #2"]

    by_id = {n.id: n for n in await store.get_nodes(mission.id)}
    assert by_id["bad"].status == "error"
    assert by_id["bad"].attempts == 3
    assert by_id["ok"].status == "done"
    # A node behind a permanently failed dependency must never run.
    assert by_id["after_bad"].status == "pending"
    assert by_id["after_bad"].attempts == 0


@pytest.mark.asyncio
async def test_executor_exception_is_a_node_error_not_an_engine_crash(stores):
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="raises@sbot.ai")
    await store.add_nodes(
        mission.id, [{"id": "boom", "title": "boom", "instruction": "w", "max_attempts": 1}]
    )

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        raise RuntimeError("executor blew up")

    engine = MissionEngine(store, executor, retry_base_delay=0.01)
    assert await engine.run_mission(mission.id) == "failed"
    node = (await store.get_nodes(mission.id))[0]
    assert node.status == "error"
    assert "executor blew up" in node.output


@pytest.mark.asyncio
async def test_unknown_executor_status_is_treated_as_error(stores):
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="badstatus@sbot.ai")
    await store.add_nodes(
        mission.id, [{"id": "n", "title": "n", "instruction": "w", "max_attempts": 1}]
    )

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        return NodeResult("finished-ish", "should not be persisted as done")

    engine = MissionEngine(store, executor, retry_base_delay=0.01)
    assert await engine.run_mission(mission.id) == "failed"
    assert (await store.get_nodes(mission.id))[0].status == "error"


@pytest.mark.asyncio
async def test_the_last_nodes_tokens_are_persisted_not_just_counted(stores):
    """A node commits itself `done` before its Task is marked complete, so the
    completion pass can see nothing unsettled while a finished node's cost is
    still unharvested. The spend was written at that point and only folded in
    afterwards by the `finally` drain — and a resumed mission re-reads spend from
    the row, so those tokens vanished from the ceiling they were meant to count
    against.
    """
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="lastspend@sbot.ai")
    await store.add_nodes(
        mission.id,
        [
            {"id": "a", "title": "a", "instruction": "w"},
            {"id": "b", "title": "b", "instruction": "w"},
        ],
    )

    class _LingeringFinish:
        """`b`'s row reaches `done` before its Task does — the real ordering,
        made deterministic."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def finish_node(self, mission_id, node_id, *a, **kw):
            await self._inner.finish_node(mission_id, node_id, *a, **kw)
            if node_id == "b":
                await asyncio.sleep(0.05)

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        return NodeResult("done", "ok", cost={"tokens": 10 if node.id == "a" else 100})

    engine = MissionEngine(_LingeringFinish(store), executor, retry_base_delay=0.01)
    assert await engine.run_mission(mission.id) == "completed"

    assert (await store.get_mission_unchecked(mission.id)).spent["tokens"] == 110


@pytest.mark.asyncio
async def test_an_oversized_node_title_is_clamped_to_its_column(stores):
    """The plan is written by an LLM into sized columns. On Postgres an
    over-long title raises DataError, which is not InvalidGraphError — so
    MissionService.plan()'s cleanup never runs, the half-built mission is left
    looking `planned`, and the caller sees a 500 instead of a 400. SQLite
    enforces no width, so the same plan passes in tests and fails in production.
    """
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="longtitle@sbot.ai")

    await store.add_nodes(
        mission.id, [{"id": "n", "title": "ก" * 5_000, "instruction": "w"}]
    )

    assert len((await store.get_nodes(mission.id))[0].title) <= 255


@pytest.mark.asyncio
async def test_an_oversized_node_id_is_refused_rather_than_truncated(stores):
    """`depends_on` references a node by id, so a truncated id would rewire the
    graph behind validate_dag's back — it validates the id it was given, not the
    one that reaches the column."""
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="longid@sbot.ai")

    with pytest.raises(InvalidGraphError, match="exceeds"):
        await store.add_nodes(
            mission.id,
            [
                {"id": "x" * 40, "title": "a", "instruction": "w"},
                {"id": "b", "title": "b", "instruction": "w", "depends_on": ["x" * 40]},
            ],
        )
    assert await store.get_nodes(mission.id) == []


@pytest.mark.asyncio
async def test_a_mission_field_that_is_not_a_column_is_refused(stores):
    """A silently-dropped write here is the worst kind: every caller of
    update_mission is moving a mission's lifecycle, so a misspelled `status`
    leaves a finished mission looking `running` and the resume sweep keeps
    picking it up. `metadata` is the trap `hasattr` fell into — a model answers
    to it, but it is not a column.
    """
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="badfield@sbot.ai")
    before = mission.status

    for bad in ({"statuss": "completed"}, {"metadata": "x"}):
        with pytest.raises(ValueError, match="no column"):
            await store.update_mission(mission.id, **bad)

    assert (await store.get_mission_unchecked(mission.id)).status == before


@pytest.mark.asyncio
async def test_a_store_failure_after_the_executor_is_a_retryable_node_error(stores):
    """The try/except used to wrap only the executor, but everything around it
    talks to the database. A raise there escaped to _harvest, which only logs —
    so the node kept the `running` status claim_node gave it, the scheduler
    skipped it as in-flight, and the mission settled `blocked` with the work
    already done and discarded. Leases are reclaimed once per run_mission call,
    so nothing in the same run recovered it.
    """
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="storefail@sbot.ai")
    await store.add_nodes(
        mission.id, [{"id": "n", "title": "n", "instruction": "w", "max_attempts": 2}]
    )

    calls = Counter()

    class _FlakyWrite:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def blackboard_write(self, *a, **kw):
            calls["write"] += 1
            if calls["write"] == 1:
                raise RuntimeError("connection reset")
            return await self._inner.blackboard_write(*a, **kw)

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        calls["run"] += 1
        return NodeResult("done", "shipped", blackboard_updates={"k": "v"})

    engine = MissionEngine(_FlakyWrite(store), executor, retry_base_delay=0.01)
    assert await engine.run_mission(mission.id) == "completed"

    # Retried within the same run rather than parked as `running` forever.
    assert calls["run"] == 2
    node = (await store.get_nodes(mission.id))[0]
    assert node.status == "done"


@pytest.mark.asyncio
async def test_a_store_failure_before_the_executor_is_a_retryable_node_error(stores):
    """Same hazard on the other side of the executor: the manifest read happened
    outside the guard, so a blip there stranded the node as `running` too."""
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="manifestfail@sbot.ai")
    await store.add_nodes(
        mission.id, [{"id": "n", "title": "n", "instruction": "w", "max_attempts": 2}]
    )

    calls = Counter()

    class _FlakyManifest:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def blackboard_manifest(self, *a, **kw):
            calls["manifest"] += 1
            if calls["manifest"] == 1:
                raise RuntimeError("connection reset")
            return await self._inner.blackboard_manifest(*a, **kw)

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        calls["run"] += 1
        return NodeResult("done", "shipped")

    engine = MissionEngine(_FlakyManifest(store), executor, retry_base_delay=0.01)
    assert await engine.run_mission(mission.id) == "completed"

    # The executor never saw the first attempt, so it runs exactly once.
    assert calls["run"] == 1
    assert (await store.get_nodes(mission.id))[0].status == "done"


@pytest.mark.asyncio
async def test_skipped_node_lets_dependents_proceed(stores):
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="skip@sbot.ai")
    await store.add_nodes(
        mission.id,
        [
            {"id": "maybe", "title": "maybe", "instruction": "w"},
            {"id": "child", "title": "child", "instruction": "w", "depends_on": ["maybe"]},
        ],
    )

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        return NodeResult("skipped" if node.id == "maybe" else "done", "")

    engine = MissionEngine(store, executor)
    assert await engine.run_mission(mission.id) == "completed"
    by_id = {n.id: n.status for n in await store.get_nodes(mission.id)}
    assert by_id == {"maybe": "skipped", "child": "done"}


@pytest.mark.asyncio
async def test_gate_node_parks_for_a_human_and_blocks(stores):
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="gate@sbot.ai")
    await store.add_nodes(
        mission.id,
        [
            {"id": "approve", "kind": "gate", "title": "approve", "instruction": "sign off"},
            {"id": "ship", "title": "ship", "instruction": "w", "depends_on": ["approve"]},
        ],
    )

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        raise AssertionError(f"gate must not reach the executor, got {node.id}")

    engine = MissionEngine(store, executor)
    assert await engine.run_mission(mission.id) == "blocked"
    by_id = {n.id: n.status for n in await store.get_nodes(mission.id)}
    assert by_id == {"approve": "awaiting_human", "ship": "pending"}


@pytest.mark.asyncio
async def test_budget_exhaustion_pauses_instead_of_running_forever(stores):
    store: MissionStore = stores["missions"]
    mission = await _mission(
        stores, budget={"max_node_attempts": 2}, email="budget@sbot.ai"
    )
    await store.add_nodes(
        mission.id, [{"id": f"n{i}", "title": "n", "instruction": "w"} for i in range(5)]
    )

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        return NodeResult("done", "ok", cost={"tokens": 100, "cost": 0.01})

    engine = MissionEngine(store, executor, max_parallel_nodes=1)
    assert await engine.run_mission(mission.id) == "paused"

    nodes = await store.get_nodes(mission.id)
    assert sum(1 for n in nodes if n.status == "done") == 2
    refreshed = await store.get_mission_unchecked(mission.id)
    assert refreshed.spent["tokens"] == 200
    assert refreshed.spent["cost"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_replan_hook_can_rescue_an_exhausted_graph(stores):
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="hook@sbot.ai")
    await store.add_nodes(
        mission.id, [{"id": "bad", "title": "bad", "instruction": "w", "max_attempts": 1}]
    )

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        return NodeResult("error" if node.id == "bad" else "done", "")

    calls = 0

    async def replan(mission_id: str, exhausted: list[MissionNode]) -> ReplanOutcome:
        nonlocal calls
        calls += 1
        assert [n.id for n in exhausted] == ["bad"]
        await store.finish_node(mission_id, "bad", status="skipped", output="replanned around")
        await store.add_nodes(mission_id, [{"id": "plan_b", "title": "b", "instruction": "w"}])
        return ReplanOutcome(changed=True, cost={"tokens": 70})

    engine = MissionEngine(store, executor, replan_hook=replan, retry_base_delay=0.01)
    assert await engine.run_mission(mission.id) == "completed"
    assert calls == 1
    assert {n.id: n.status for n in await store.get_nodes(mission.id)} == {
        "bad": "skipped",
        "plan_b": "done",
    }
    # Thinking about the rescue is an LLM call inside a budgeted loop, so it is
    # charged like a node — otherwise a mission can replan its way past its cap.
    assert (await store.get_mission_unchecked(mission.id)).spent["tokens"] == 70


@pytest.mark.asyncio
async def test_a_replan_hook_that_raises_fails_the_mission_not_the_scheduler(stores):
    """The hook is where an LLM is allowed to think, so it is the least reliable
    thing the loop calls. A raise escaping run_mission would surface as a crashed
    scheduler and hide the node failure that is the real reason for the failure.
    """
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="hookraise@sbot.ai")
    await store.add_nodes(
        mission.id, [{"id": "bad", "title": "bad", "instruction": "w", "max_attempts": 1}]
    )

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        return NodeResult("error", "nope")

    async def replan(mission_id: str, exhausted: list[MissionNode]) -> ReplanOutcome:
        raise RuntimeError("the replanner itself broke")

    engine = MissionEngine(store, executor, replan_hook=replan, retry_base_delay=0.01)
    assert await engine.run_mission(mission.id) == "failed"


@pytest.mark.asyncio
async def test_mission_resumes_after_a_worker_dies(stores):
    """Long-horizon requirement: state lives in the database, so a mission whose
    worker vanished mid-node must be resumable by the next run_mission call."""
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="resume@sbot.ai")
    await store.add_nodes(mission.id, [{"id": "n", "title": "n", "instruction": "w"}])

    # Simulate a crashed worker: node claimed, lease already expired.
    claimed = await store.claim_node(mission.id, "n", worker_id="dead-worker", lease_seconds=-1)
    assert claimed is not None and claimed.status == "running"

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        assert ctx.attempt == 2
        assert "Lease expired" in (ctx.last_error or "")
        return NodeResult("done", "recovered")

    engine = MissionEngine(store, executor, worker_id="fresh-worker")
    assert await engine.run_mission(mission.id) == "completed"
    node = (await store.get_nodes(mission.id))[0]
    assert node.status == "done" and node.output == "recovered"


@pytest.mark.asyncio
async def test_claim_node_yields_exactly_one_winner(stores):
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="claim@sbot.ai")
    await store.add_nodes(
        mission.id, [{"id": "n", "title": "n", "instruction": "w", "max_attempts": 5}]
    )

    results = await asyncio.gather(
        *(
            store.claim_node(mission.id, "n", worker_id=f"w{i}", lease_seconds=60)
            for i in range(5)
        )
    )
    assert sum(1 for r in results if r is not None) == 1
    assert (await store.get_nodes(mission.id))[0].attempts == 1


@pytest.mark.asyncio
async def test_mission_paused_externally_stops_scheduling(stores):
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="external@sbot.ai")
    await store.add_nodes(
        mission.id, [{"id": f"n{i}", "title": "n", "instruction": "w"} for i in range(4)]
    )

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        await store.update_mission(mission.id, status="cancelled")
        return NodeResult("done", "ok")

    engine = MissionEngine(store, executor, max_parallel_nodes=1)
    assert await engine.run_mission(mission.id) == "cancelled"
    assert sum(1 for n in await store.get_nodes(mission.id) if n.status == "done") == 1


# ------------------------------------------------------------ context economy
@pytest.mark.asyncio
async def test_manifest_describes_values_without_carrying_them(stores):
    """Every downstream node would otherwise carry the whole blackboard in its
    prompt, making token cost grow with the graph rather than the task."""
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="manifest@sbot.ai")
    huge = "x" * 5000
    await store.blackboard_write(mission.id, "report", huge, node_id="n1")
    await store.blackboard_write(mission.id, "count", 42, node_id="n1")
    await store.blackboard_write(mission.id, "items", ["a", "b", "c"], node_id="n2")

    manifest = await store.blackboard_manifest(mission.id)

    assert manifest["report"]["chars"] == 5000
    assert len(manifest["report"]["preview"]) < 200
    assert huge not in str(manifest)
    assert manifest["report"]["written_by_node"] == "n1"
    assert manifest["count"]["preview"] == 42
    assert manifest["items"]["items"] == 3
    # The full value is still one read away for the node that needs it.
    assert await store.blackboard_read(mission.id, "report") == huge


@pytest.mark.asyncio
async def test_parent_output_is_summarized_into_child_context(stores):
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="summary@sbot.ai")
    await store.add_nodes(
        mission.id,
        [
            {"id": "parent", "title": "p", "instruction": "w"},
            {"id": "child", "title": "c", "instruction": "w", "depends_on": ["parent"]},
        ],
    )

    long_output = "y" * 4000
    seen: dict[str, str] = {}

    async def executor(node: MissionNode, ctx: NodeContext) -> NodeResult:
        if node.id == "parent":
            return NodeResult("done", long_output)
        seen.update(ctx.parent_outputs)
        return NodeResult("done", "ok")

    engine = MissionEngine(store, executor)
    assert await engine.run_mission(mission.id) == "completed"
    assert len(seen["parent"]) < len(long_output)
    assert seen["parent"].startswith("y")


@pytest.mark.asyncio
async def test_a_claimed_node_cannot_be_re_armed_by_a_replan(stores):
    """Re-arming a node another worker is running makes it legitimately
    claimable while its first copy is still executing, and claim_node cannot
    stop that — the mission would pay for the same step twice.
    """
    store: MissionStore = stores["missions"]
    mission = await _mission(stores, email="rearm@sbot.ai")
    await store.add_nodes(mission.id, [{"id": "n", "title": "n", "instruction": "เดิม"}])
    await store.finish_node(mission.id, "n", status="error", output="ล้ม")

    assert await store.revise_node(mission.id, "n", instruction="ใหม่") is not None
    await store.finish_node(mission.id, "n", status="error", output="ล้มอีก")
    assert await store.claim_node(mission.id, "n", "worker-a", 300.0) is not None

    assert await store.revise_node(mission.id, "n", instruction="ใหม่กว่า") is None
    node = (await store.get_nodes(mission.id))[0]
    assert node.status == "running"
    assert node.instruction == "ใหม่"


@pytest.mark.asyncio
async def test_lease_heartbeat_keeps_long_running_work_owned(stores):
    store = stores['missions']
    mission = await _mission(stores, email='heartbeat-lease@sbot.ai')
    await store.add_nodes(mission.id, [{'id': 'n', 'title': 'long', 'instruction': 'build'}])
    await store.update_mission(mission.id, status='running')

    async def execute(node, context):
        await asyncio.sleep(0.25)
        assert await store.reclaim_expired_leases(mission.id) == 0
        return NodeResult(status='done', output='built')

    engine = MissionEngine(store, execute, lease_seconds=0.12)
    assert await engine.run_mission(mission.id) == 'completed'
    assert (await store.get_nodes(mission.id))[0].attempts == 1


@pytest.mark.asyncio
async def test_old_worker_cannot_finish_reclaimed_attempt(stores):
    store = stores['missions']
    mission = await _mission(stores, email='fencing@sbot.ai')
    await store.add_nodes(mission.id, [{'id': 'n', 'title': 'n', 'instruction': 'build'}])
    first = await store.claim_node(mission.id, 'n', 'old', -1)
    await store.reclaim_expired_leases(mission.id)
    second = await store.claim_node(mission.id, 'n', 'new', 60)
    await store.finish_node(mission.id, 'n', status='done', output='stale',
                            lease_owner='old', attempt=first.attempts)
    node = (await store.get_nodes(mission.id))[0]
    assert node.status == 'running'
    assert node.lease_owner == 'new'
    assert node.attempts == second.attempts


def test_wall_budget_includes_time_before_restart():
    from types import SimpleNamespace
    mission = SimpleNamespace(budget={'max_wall_seconds': 100})
    assert MissionEngine._budget_exceeded(mission, {'seconds': 90}, 11, []) == 'max_wall_seconds'


@pytest.mark.asyncio
async def test_runtime_contract_is_fenced_with_node_completion(stores):
    store = stores['missions']
    mission = await _mission(stores, email='contract-fencing@sbot.ai')
    await store.add_nodes(mission.id, [{'id': 'n', 'title': 'n', 'instruction': 'build'}])
    old = await store.claim_node(mission.id, 'n', 'old', -1)
    await store.reclaim_expired_leases(mission.id)
    new = await store.claim_node(mission.id, 'n', 'new', 60)
    await store.finish_node(mission.id, 'n', status='done', output='current',
                            lease_owner='new', attempt=new.attempts,
                            task_result={'status': 'completed', 'verification_status': 'not_verified'})
    await store.finish_node(mission.id, 'n', status='done', output='stale',
                            lease_owner='old', attempt=old.attempts,
                            task_result={'status': 'completed', 'verification_status': 'passed'})
    assert await store.blackboard_read(mission.id, 'result:n') == 'current'
    assert (await store.blackboard_read(mission.id, 'contract:n'))['verification_status'] == 'not_verified'


@pytest.mark.asyncio
async def test_agent_cannot_forge_runtime_contract(stores):
    from sbot.tools.blackboard import BlackboardTool
    mission = await _mission(stores, email='reserved-contract@sbot.ai')
    tool = BlackboardTool(stores['missions'], mission.id, 'n')
    for key in ('result:n', 'contract:n', 'scope:members', 'delivery:status'):
        answer = await tool.execute('write', key=key, value='completed')
        assert answer.startswith('Error:')
        assert await stores['missions'].blackboard_read(mission.id, key) is None
