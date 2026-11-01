"""Tool-ephemeral sandbox: each shell command runs in a short-lived container.

The agent itself stays in the host process (multi-tenant); only risky tool
execution pays the container cost. `docker run --rm` with CPU/memory/pids
limits and a workspace bind mount. Falls back to a plain subprocess when the
sandbox is disabled.
"""

import asyncio
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from claw.config import SandboxSettings

_OUTPUT_CAP = 20_000
# Where the user's workspace appears inside the sandbox. Paths under it are
# the model's own namespace, not the host's.
WORKSPACE_MOUNT = "/workspace"


def sandbox_user(setting: str) -> str:
    """The `--user` value for a setting of "auto", "" or an explicit uid[:gid]."""
    if setting != "auto":
        return setting
    uid, gid = os.getuid(), os.getgid()
    return "65534:65534" if uid == 0 else f"{uid}:{gid}"


def hardening_flags(settings: SandboxSettings) -> list[str]:
    """`docker run` flags that confine an ephemeral exec container.

    Read-only root filesystem (only /workspace and a fresh tmpfs /tmp are
    writable), every capability dropped, no setuid escalation, and a non-root
    user, so a command cannot change the image, read root-only files such as
    /etc/shadow, or keep state between runs. HOME and the caches point at /tmp
    because the read-only root has no writable home directory.
    The system-file guard (claw/tools/system_path_guard.py) is what refuses
    reading world-readable system files; this limits what slips past it.
    """
    if not getattr(settings, "hardened", True):
        return []
    flags = [
        "--read-only",
        "--tmpfs", f"/tmp:rw,exec,nosuid,nodev,size={getattr(settings, 'tmp_size', '512m')}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-e", "HOME=/tmp",
        "-e", "MPLCONFIGDIR=/tmp/.matplotlib",
        "-e", "XDG_CACHE_HOME=/tmp/.cache",
    ]
    user = sandbox_user(getattr(settings, "user", "auto"))
    if user:
        flags += ["--user", user]
    return flags


@dataclass(slots=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    infrastructure_error: bool = False

    def render(self) -> str:
        parts = []
        if self.infrastructure_error:
            parts.append("Error: [sandbox_unavailable] Execution environment is unavailable; do not retry different commands.")
        elif self.exit_code != 0 or self.timed_out:
            parts.append("Error: command failed")
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

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=None if self.settings.enabled else str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return SandboxResult(-1, "", str(exc), infrastructure_error=True)
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
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise

        return SandboxResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode("utf-8", "replace")[-_OUTPUT_CAP:],
            stderr=stderr_b.decode("utf-8", "replace")[-_OUTPUT_CAP:],
            timed_out=timed_out,
            infrastructure_error=self.settings.enabled and proc.returncode == 125,
        )

    def _docker_argv(self, command: str, workspace: Path) -> list[str]:
        s = self.settings
        return [
            "docker", "run", "--rm", "--pull=never",
            "--network", s.network,
            "--cpus", str(s.cpu_limit),
            "--memory", s.memory_limit,
            "--pids-limit", str(s.pids_limit),
            *hardening_flags(s),
            "--workdir", WORKSPACE_MOUNT,
            "--mount", f"type=bind,source={workspace.resolve()},target={WORKSPACE_MOUNT}",
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


# Baked into docker/sandbox.Dockerfile because a network=none sandbox cannot
# install them at run time. An image built before that change lacks them.
_IMAGE_PROBE = "python -c 'import matplotlib, openpyxl' && command -v tesseract >/dev/null"


async def image_readiness_problem(settings: SandboxSettings, timeout: float = 60.0) -> str | None:
    """Why the sandbox image can't serve a network=none sandbox, or None if it can.

    Only meaningful when the sandbox is enabled with network=none; returns None
    otherwise. Never raises: a probe that cannot run is itself the problem.
    """
    if not settings.enabled or settings.network != "none":
        return None
    rebuild = f"rebuild it: docker build -f docker/sandbox.Dockerfile -t {settings.image} ."
    argv = [
        "docker", "run", "--rm", "--pull=never", "--network", "none",
        settings.image, "/bin/sh", "-c", _IMAGE_PROBE,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        return f"docker is unavailable ({exc}); sandbox commands will fail"
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return f"probing {settings.image} timed out after {timeout:.0f}s"
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if proc.returncode == 0:
        return None
    if proc.returncode == 125:
        detail = (stderr.decode("utf-8", "replace").strip().splitlines() or [""])[0]
        return f"image {settings.image} is not usable ({detail}); {rebuild}"
    return (
        f"image {settings.image} lacks matplotlib/openpyxl/tesseract, which a network=none "
        f"sandbox cannot install at run time; {rebuild}"
    )


def shell_quote(command: str) -> str:
    return shlex.quote(command)
