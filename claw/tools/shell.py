"""Shell tool backed by the tool-ephemeral sandbox."""

from pathlib import Path
from typing import Any

from claw.sandbox.ephemeral import EphemeralSandbox
from claw.tools.base import Tool


class ExecTool(Tool):
    name = "exec"
    description = (
        "Execute a shell command. Commands run inside an isolated ephemeral sandbox "
        "with the workspace mounted at /workspace."
    )
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "Shell command to run"}},
        "required": ["command"],
    }

    def __init__(self, sandbox: EphemeralSandbox, workspace: Path):
        self.sandbox = sandbox
        self.workspace = workspace

    async def execute(self, command: str, **_: Any) -> str:
        result = await self.sandbox.run(command, self.workspace)
        from claw.jobs.provider import current_execution
        ctx = current_execution.get()
        if ctx is not None and getattr(result, 'infrastructure_error', False):
            from claw.jobs.runtime_bridge import Handoff
            from claw.jobs.contracts import StepOutcome
            runtime = {**ctx.state.get('runtime', {}), 'pending_call': None}
            await ctx.checkpoint({**ctx.state, 'runtime': runtime, 'pending_tool': None})
            raise Handoff(StepOutcome('dependency', checkpoint=ctx.state,
                                     dependency='sandbox', reason='dependency_unavailable'))
        from claw.jobs.tool_results import ToolText
        from claw.jobs.contracts import ToolResult
        return ToolText(ToolResult(status='ok' if result.exit_code == 0 and not result.timed_out else 'error',
            text=result.render(), error_code='' if result.exit_code == 0 else 'execution_error',
            dependency='sandbox'))
