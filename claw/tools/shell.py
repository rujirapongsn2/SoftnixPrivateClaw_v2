"""Shell tool backed by the tool-ephemeral sandbox."""

from pathlib import Path
from typing import Any

from claw.sandbox.ephemeral import EphemeralSandbox
from claw.tools.base import Tool
from claw.sandbox.capabilities import is_offline, offline_guidance
from claw.tools.install_guard import blocked_message as install_blocked
from claw.tools.install_guard import offline_blocked_message as offline_blocked
from claw.tools.scan_guard import blocked_message as scan_blocked
from claw.tools.system_path_guard import blocked_message as system_path_blocked


class ExecTool(Tool):
    name = "exec"
    _base = (
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
    description = _base + (
        " /workspace is the user's "
        "persistent, size-limited storage: never install packages or `git clone` into it. "
        "Do that in the sandbox's /tmp within the same command (`cd /tmp && git clone ... "
        "&& ...`), since /tmp is discarded when the command ends, and copy only the result "
        "into /workspace. Keep scratch files in /workspace/.tmp/ (deleted automatically)."
    )
    # Without a network nothing can be installed, so the /tmp install advice
    # gives way to the list of what is preinstalled.
    offline_description = _base + (
        " Keep scratch files in /workspace/.tmp/ (deleted automatically) or /tmp (discarded when the "
        "command ends). "
    ) + offline_guidance()
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "Shell command to run"}},
        "required": ["command"],
    }

    def __init__(self, sandbox: EphemeralSandbox, workspace: Path):
        self.sandbox = sandbox
        self.workspace = workspace
        self.offline = is_offline(getattr(sandbox, "settings", None))
        if self.offline:
            self.description = self.offline_description

    async def execute(self, command: str, **_: Any) -> str:
        # Offline first: "install it in /tmp instead" is no advice without a network.
        refused = (
            system_path_blocked(command)
            or scan_blocked(command)
            or (offline_blocked(command) if self.offline else None)
            or install_blocked(command)
        )
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
