"""Workflow tool: run a multi-step dynamic workflow for a complex request."""

from typing import Any

from claw.tools.base import Tool
from claw.workflows.service import WorkflowService


class WorkflowTool(Tool):
    name = "workflow"
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

    async def execute(self, request: str, **_: Any) -> str:
        result = await self.service.run_request(request)
        lines = [f"Workflow {result.status}. Steps:"]
        for i, step in enumerate(result.plan.steps, start=1):
            lines.append(f"  {i}. [{step.status}] {step.title}")
        lines.append("\n" + result.final_output)
        return "\n".join(lines)
