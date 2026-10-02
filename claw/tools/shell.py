"""Shell tool backed by the tool-ephemeral sandbox."""

from pathlib import Path
from typing import Any

from claw.sandbox.ephemeral import EphemeralSandbox
from claw.tools.base import Tool
from claw.tools.install_guard import blocked_message as install_blocked
from claw.tools.scan_guard import blocked_message as scan_blocked


class ExecTool(Tool):
    name = "exec"
    description = (
        "Execute a shell command. Commands run inside an isolated ephemeral sandbox "
        "with the workspace mounted at /workspace. Search and list within /workspace; "
        "scanning the whole filesystem (`find /`, `grep -r ... /`) is refused because it "
        "holds only system files and times out. Connector/MCP data is not on this "
        "filesystem: fetch it with that connector's tools. /workspace is the user's "
        "persistent, size-limited storage: never install packages or `git clone` into it. "
        "Do that in the sandbox's /tmp within the same command (`cd /tmp && git clone ... "
        "&& ...`), since /tmp is discarded when the command ends, and copy only the result "
        "into /workspace. Keep scratch files in /workspace/.tmp/ (deleted automatically)."
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
        refused = scan_blocked(command) or install_blocked(command)
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
