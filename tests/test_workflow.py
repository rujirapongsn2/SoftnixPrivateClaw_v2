from pathlib import Path

from claw.config import SandboxSettings
from claw.core.subagent import SubagentManager
from claw.sandbox.ephemeral import EphemeralSandbox
from claw.providers.base import ChatResult, ToolCall
from claw.tools.workflow import WorkflowTool
from claw.workflows.service import WorkflowService
from tests.conftest import FakeProvider, text_turn


def _mgr(provider, tmp_path) -> SubagentManager:
    return SubagentManager(provider, EphemeralSandbox(SandboxSettings(enabled=False)), tmp_path,
                           max_iterations=3)


def _plan_turn(steps: list[dict]):
    return [ChatResult(content=None, tool_calls=[
        ToolCall(id="p", name="propose_plan", arguments={"steps": steps})
    ])]


async def test_multi_step_workflow_plans_runs_synthesizes(tmp_path):
    provider = FakeProvider([
        _plan_turn([
            {"title": "Research", "instruction": "gather facts"},
            {"title": "Draft", "instruction": "write it up"},
        ]),
        text_turn("facts gathered"),   # subagent step 1
        text_turn("draft written"),    # subagent step 2
        text_turn("FINAL SYNTHESIS"),  # synthesis
    ])
    service = WorkflowService(provider, _mgr(provider, tmp_path))
    result = await service.run_request("produce a report")

    assert result.status == "completed"
    assert [s.title for s in result.plan.steps] == ["Research", "Draft"]
    assert [s.output for s in result.plan.steps] == ["facts gathered", "draft written"]
    assert result.final_output == "FINAL SYNTHESIS"


async def test_single_step_skips_synthesis(tmp_path):
    provider = FakeProvider([
        _plan_turn([{"title": "Do it", "instruction": "just do the thing"}]),
        text_turn("the only answer"),
    ])
    service = WorkflowService(provider, _mgr(provider, tmp_path))
    result = await service.run_request("simple task")

    assert len(result.plan.steps) == 1
    assert result.final_output == "the only answer"  # no extra synthesis turn consumed


async def test_progress_callback_invoked(tmp_path):
    provider = FakeProvider([
        _plan_turn([{"title": "Only", "instruction": "x"}]),
        text_turn("done"),
    ])
    service = WorkflowService(provider, _mgr(provider, tmp_path))
    events: list[str] = []

    async def on_progress(msg: str) -> None:
        events.append(msg)

    await service.run_request("task", on_progress=on_progress)
    assert any("Planned" in e for e in events)
    assert any("Step 1/1" in e for e in events)


async def test_workflow_tool_output_format(tmp_path):
    provider = FakeProvider([
        _plan_turn([{"title": "Step A", "instruction": "a"}]),
        text_turn("result A"),
    ])
    tool = WorkflowTool(WorkflowService(provider, _mgr(provider, tmp_path)))
    out = await tool.execute(request="do a")
    assert "Workflow completed" in out
    assert "Step A" in out
    assert "result A" in out


async def test_empty_plan_falls_back_to_single_step(tmp_path):
    provider = FakeProvider([
        _plan_turn([]),          # planner returns no steps
        text_turn("did it anyway"),
    ])
    service = WorkflowService(provider, _mgr(provider, tmp_path))
    result = await service.run_request("something")
    assert len(result.plan.steps) == 1
    assert result.final_output == "did it anyway"
