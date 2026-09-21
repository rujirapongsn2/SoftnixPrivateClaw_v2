"""Tool-ephemeral sandbox: each shell command runs in a short-lived container.

The agent itself stays in the host process (multi-tenant); only risky tool
execution pays the container cost. `docker run --rm` with CPU/memory/pids
limits and a workspace bind mount. Falls back to a plain subprocess when the
sandbox is disabled.
"""

from pathlib import Path

from claw.sandbox.ephemeral import (
    EphemeralSandbox as SharedSandbox,
    SandboxResult as SandboxResult,
    shell_quote as shell_quote,
)
from sbot.config import SandboxSettings


class EphemeralSandbox(SharedSandbox):
    def __init__(self, settings: SandboxSettings, readonly_inputs: bool = False):
        super().__init__(settings)
        self.readonly_inputs = readonly_inputs
        from sbot.sandbox.projects import ProjectEnvironments
        self.projects = ProjectEnvironments(settings)

    def _docker_argv(self, command: str, workspace: Path) -> list[str]:
        s = self.settings
        return [
            "docker", "run", "--rm", "--pull=never",
            "--network", s.network,
            "--cpus", str(s.cpu_limit),
            "--memory", s.memory_limit,
            "--pids-limit", str(s.pids_limit),
            "--workdir", "/workspace",
            "--mount", f"type=bind,source={workspace.resolve()},target=/workspace",
            *(['--mount', f'type=bind,source={workspace.resolve() / "inputs"},target=/workspace/inputs,readonly']
              if self.readonly_inputs else []),
            s.image,
            "/bin/sh", "-lc", command,
        ]
