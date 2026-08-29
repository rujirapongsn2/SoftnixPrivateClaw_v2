from pathlib import Path

from claw.config import SandboxSettings
from claw.core.subagent import SubagentManager, SubagentRun
from claw.sandbox.ephemeral import EphemeralSandbox
from claw.providers.base import ChatResult, ToolCall
from claw.tools.workflow import WorkflowTool
from claw.workflows import service as service_mod
from claw.workflows.service import WorkflowService
from tests.conftest import FakeProvider, text_turn


def _mgr(provider, tmp_path) -> SubagentManager:
    return SubagentManager(
        provider, EphemeralSandbox(SandboxSettings(enabled=False)), tmp_path, max_iterations=3
    )


def _plan_turn(steps: list[dict]):
    return [
        ChatResult(
            content=None, tool_calls=[ToolCall(id="p", name="propose_plan", arguments={"steps": steps})]
        )
    ]


async def test_multi_step_workflow_plans_runs_synthesizes(tmp_path):
    provider = FakeProvider(
        [
            _plan_turn(
                [
                    {"title": "Research", "instruction": "gather facts"},
                    {"title": "Draft", "instruction": "write it up"},
                ]
            ),
            text_turn("facts gathered"),  # subagent step 1
            text_turn("draft written"),  # subagent step 2
            text_turn("FINAL SYNTHESIS"),  # synthesis
        ]
    )
    service = WorkflowService(provider, _mgr(provider, tmp_path))
    result = await service.run_request("produce a report")

    assert result.status == "completed"
    assert [s.title for s in result.plan.steps] == ["Research", "Draft"]
    assert [s.output for s in result.plan.steps] == ["facts gathered", "draft written"]
    assert result.final_output == "FINAL SYNTHESIS"


async def test_single_step_skips_synthesis(tmp_path):
    provider = FakeProvider(
        [
            _plan_turn([{"title": "Do it", "instruction": "just do the thing"}]),
            text_turn("the only answer"),
        ]
    )
    service = WorkflowService(provider, _mgr(provider, tmp_path))
    result = await service.run_request("simple task")

    assert len(result.plan.steps) == 1
    assert result.final_output == "the only answer"  # no extra synthesis turn consumed


async def test_progress_callback_invoked(tmp_path):
    provider = FakeProvider(
        [
            _plan_turn([{"title": "Only", "instruction": "x"}]),
            text_turn("done"),
        ]
    )
    service = WorkflowService(provider, _mgr(provider, tmp_path))
    events: list[dict] = []

    # Progress is now a structured payload (stage/label/index/total/status) so
    # the UI can render a live checklist.
    async def on_progress(payload: dict) -> None:
        events.append(payload)

    await service.run_request("task", on_progress=on_progress)
    assert any(e["stage"] == "plan" and "Planned" in e["label"] for e in events)
    assert any(e["stage"] == "step" and e["index"] == 1 and e["total"] == 1 for e in events)


async def test_workflow_tool_output_format(tmp_path):
    provider = FakeProvider(
        [
            _plan_turn([{"title": "Step A", "instruction": "a"}]),
            text_turn("result A"),
        ]
    )
    tool = WorkflowTool(WorkflowService(provider, _mgr(provider, tmp_path)))
    out = await tool.execute(request="do a")
    assert "Workflow completed" in out
    assert "Step A" in out
    assert "result A" in out


class StubManager:
    """Stands in for SubagentManager so a step's outcome can be scripted."""

    def __init__(self, runs: list[SubagentRun], max_turn_seconds: float = 600.0):
        self.runs = list(runs)
        self.max_turn_seconds = max_turn_seconds
        self.calls: list[tuple[str, str, float | None]] = []

    async def run_result(self, task, context="", max_turn_seconds=None) -> SubagentRun:
        self.calls.append((task, context, max_turn_seconds))
        return self.runs.pop(0)


async def test_a_step_whose_subagent_gave_up_is_not_reported_as_done(tmp_path):
    # The subagent returns its limit message as ordinary prose, so a status
    # derived from "no exception raised" would call this workflow completed.
    provider = FakeProvider(
        [
            _plan_turn(
                [
                    {"title": "Research", "instruction": "gather facts"},
                    {"title": "Draft", "instruction": "write it up"},
                ]
            ),
            text_turn("FINAL"),
        ]
    )
    mgr = StubManager(
        [
            SubagentRun("Subagent ran out of time before finishing.", ok=False),
            SubagentRun("draft written", ok=True),
        ]
    )
    result = await WorkflowService(provider, mgr).run_request("produce a report")

    assert [s.status for s in result.plan.steps] == ["error", "done"]
    assert result.status == "failed"
    # The later step and the synthesizer must both be able to see it failed.
    assert "(did not finish)" in mgr.calls[1][1]


async def test_steps_share_one_budget_instead_of_each_getting_the_full_turn(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(service_mod, "monotonic", lambda: clock["t"])
    provider = FakeProvider(
        [
            _plan_turn(
                [
                    {"title": "One", "instruction": "a"},
                    {"title": "Two", "instruction": "b"},
                ]
            ),
            text_turn("FINAL"),
        ]
    )

    mgr = StubManager([SubagentRun("first result", ok=True)], max_turn_seconds=60.0)

    async def run_result(task, context="", max_turn_seconds=None):
        mgr.calls.append((task, context, max_turn_seconds))
        clock["t"] += 50.0
        return mgr.runs.pop(0)

    mgr.run_result = run_result
    result = await WorkflowService(provider, mgr).run_request("two-parter")

    # Step 1 is handed the whole remaining budget, not a fresh 600s.
    assert mgr.calls[0][2] == 60.0
    # Step 2 never runs: 10s left is under the minimum worth starting.
    assert len(mgr.calls) == 1
    assert result.plan.steps[1].status == "error"
    assert "ran out of time" in result.plan.steps[1].output
    assert result.status == "failed"


async def test_empty_plan_falls_back_to_single_step(tmp_path):
    provider = FakeProvider(
        [
            _plan_turn([]),  # planner returns no steps
            text_turn("did it anyway"),
        ]
    )
    service = WorkflowService(provider, _mgr(provider, tmp_path))
    result = await service.run_request("something")
    assert len(result.plan.steps) == 1
    assert result.final_output == "did it anyway"
