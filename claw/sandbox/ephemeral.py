"""Tool-ephemeral sandbox: each shell command runs in a short-lived container.

The agent itself stays in the host process (multi-tenant); only risky tool
execution pays the container cost. `docker run --rm` with CPU/memory/pids
limits and a workspace bind mount. Falls back to a plain subprocess when the
sandbox is disabled.
"""

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from claw.config import SandboxSettings

_OUTPUT_CAP = 20_000


@dataclass(slots=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def render(self) -> str:
        parts = []
        if self.timed_out:
            parts.append("[command timed out]")
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        parts.append(f"[exit code: {self.exit_code}]")
        return "\n".join(parts)


class EphemeralSandbox:
    def __init__(self, settings: SandboxSettings):
        self.settings = settings

    async def run(self, command: str, workspace: Path) -> SandboxResult:
        if self.settings.enabled:
            argv = self._docker_argv(command, workspace)
        else:
            argv = ["/bin/sh", "-lc", command]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=None if self.settings.enabled else str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.settings.timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
            logger.warning("Sandbox command timed out after {}s", self.settings.timeout_seconds)

        return SandboxResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode("utf-8", "replace")[-_OUTPUT_CAP:],
            stderr=stderr_b.decode("utf-8", "replace")[-_OUTPUT_CAP:],
            timed_out=timed_out,
        )

    def _docker_argv(self, command: str, workspace: Path) -> list[str]:
        s = self.settings
        return [
            "docker", "run", "--rm",
            "--network", s.network,
            "--cpus", str(s.cpu_limit),
            "--memory", s.memory_limit,
            "--pids-limit", str(s.pids_limit),
            "--workdir", "/workspace",
            "--mount", f"type=bind,source={workspace.resolve()},target=/workspace",
            s.image,
            "/bin/sh", "-lc", command,
        ]

    def describe(self) -> str:
        if not self.settings.enabled:
            return "local subprocess (sandbox disabled)"
        return (
            f"docker {self.settings.image} (cpu={self.settings.cpu_limit}, "
            f"mem={self.settings.memory_limit}, network={self.settings.network})"
        )


def shell_quote(command: str) -> str:
    return shlex.quote(command)
