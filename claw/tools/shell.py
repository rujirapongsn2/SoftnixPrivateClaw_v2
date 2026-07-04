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
        return result.render()
