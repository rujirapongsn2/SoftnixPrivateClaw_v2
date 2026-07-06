"""Workflow tool: run a multi-step dynamic workflow for a complex request."""

from collections.abc import Callable
from typing import Any

from claw.tools.base import Tool
from claw.workflows.service import WorkflowService


class WorkflowTool(Tool):
    name = "workflow"
    # Streams plan/step/synthesize progress to the Execution panel.
    wants_progress = True
    description = (
        "Run a multi-step dynamic workflow for a complex request that benefits from being "
        "broken into stages (research → analysis → synthesis, etc.). Plans the steps, runs "
        "each with an isolated worker, and returns a synthesized result. Use for genuinely "
        "multi-part tasks, not simple questions."
    )
    parameters = {
        "type": "object",
        "properties": {
            "request": {"type": "string", "description": "The complex, multi-step request"}
        },
        "required": ["request"],
    }

    def __init__(self, service: WorkflowService):
        self.service = service

    async def execute(
        self, request: str, progress: Callable[[dict], None] | None = None, **_: Any
    ) -> str:
        # Bridge the loop's sync progress emitter to the service's async callback.
        on_progress = None
        if progress is not None:
            async def on_progress(payload: dict) -> None:
                progress(payload)

        result = await self.service.run_request(request, on_progress=on_progress)
        lines = [f"Workflow {result.status}. Steps:"]
        for i, step in enumerate(result.plan.steps, start=1):
            lines.append(f"  {i}. [{step.status}] {step.title}")
        lines.append("\n" + result.final_output)
        return "\n".join(lines)
