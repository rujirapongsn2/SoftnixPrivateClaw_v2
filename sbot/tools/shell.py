"""Shell tool backed by the tool-ephemeral sandbox."""

from pathlib import Path
from typing import Any

from claw.tools.scan_guard import blocked_message
from claw.tools.system_path_guard import blocked_message as system_path_blocked
from sbot.sandbox.ephemeral import EphemeralSandbox
from sbot.tools.base import Tool


class ExecTool(Tool):
    name = "exec"
    description = (
        "Execute a shell command. Commands run inside an isolated ephemeral sandbox "
        "with the workspace mounted at /workspace. Search and list within /workspace; "
        "scanning the whole filesystem (`find /`, `grep -r ... /`) is refused because it "
        "holds only system files and times out. "
        "System files are off limits: commands that read, list, copy or export paths outside "
        "/workspace such as /etc, /proc, /sys, /root, /home, /var or ~ are refused; if the user asks "
        "for one, decline in the user's language instead of trying. "
        "Connector/MCP data is not on this "
        "filesystem: fetch it with that connector's tools."
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
        refused = system_path_blocked(command) or blocked_message(command)
        if refused is not None:
            return refused
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
