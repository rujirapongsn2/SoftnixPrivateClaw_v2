"""Admin control and readiness checks for persistent project containers."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class ProjectContainerManager:
    """Persist enablement and build the fixed developer image in the background."""

    def __init__(self, settings, config_store, source_root: Path | None = None):
        self.settings = settings
        self.config_store = config_store
        self.source_root = source_root or Path(__file__).resolve().parents[2]
        self._build_task: asyncio.Task | None = None
        self._build_error = ""
        self._build_started_at: str | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        config = await self.config_store.get(default_enabled=self.settings.projects_enabled)
        self.settings.projects_enabled = config["enabled"]
        if self.settings.enabled and self.settings.projects_enabled:
            status = await self.status()
            if status["docker_available"] and not status["image_available"]:
                await self.start_build()

    async def _run(self, *argv: str, timeout: float = 20) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        tail = bytearray()

        async def drain() -> None:
            assert proc.stdout is not None
            while chunk := await proc.stdout.read(8192):
                tail.extend(chunk)
                del tail[:-20_000]

        output_task = asyncio.create_task(drain())
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            await output_task
            return 124, "Command timed out"
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            await output_task
            raise
        await output_task
        return proc.returncode or 0, tail.decode("utf-8", "replace")

    async def _docker_ready(self) -> bool:
        try:
            code, _ = await self._run("docker", "info", "--format", "{{.ServerVersion}}", timeout=10)
            return code == 0
        except (OSError, RuntimeError):
            return False

    async def _image_ready(self) -> bool:
        try:
            code, _ = await self._run(
                "docker", "image", "inspect", self.settings.project_image, "--format", "{{.Id}}", timeout=10
            )
            return code == 0
        except (OSError, RuntimeError):
            return False

    async def status(self) -> dict:
        docker_available = await self._docker_ready()
        image_available = docker_available and await self._image_ready()
        building = self._build_task is not None and not self._build_task.done()
        return {
            "enabled": bool(self.settings.enabled and self.settings.projects_enabled),
            "docker_available": docker_available,
            "image_available": image_available,
            "image": self.settings.project_image,
            "building": building,
            "build_error": self._build_error,
            "build_started_at": self._build_started_at,
            "ready": bool(self.settings.enabled and self.settings.projects_enabled and docker_available and image_available),
        }

    async def set_enabled(self, enabled: bool) -> dict:
        await self.config_store.set_enabled(enabled)
        self.settings.projects_enabled = enabled
        status = await self.status()
        if enabled and status["docker_available"] and not status["image_available"]:
            await self.start_build()
            status = await self.status()
        return status

    async def start_build(self) -> dict:
        async with self._lock:
            if self._build_task is not None and not self._build_task.done():
                return await self.status()
            if not await self._docker_ready():
                self._build_error = "Docker is unavailable"
                return await self.status()
            if await self._image_ready():
                self._build_error = ""
                return await self.status()
            dockerfile = self.source_root / "docker" / "developer.Dockerfile"
            helper = self.source_root / "scripts" / "project-worktree.py"
            if not dockerfile.is_file() or not helper.is_file():
                self._build_error = "Developer image build files are unavailable"
                return await self.status()
            self._build_error = ""
            self._build_started_at = datetime.now(timezone.utc).isoformat()
            self._build_task = asyncio.create_task(self._build(dockerfile, helper))
        return await self.status()

    async def _build(self, dockerfile: Path, helper: Path) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="privateclaw-developer-image-") as temporary:
                context = Path(temporary)
                (context / "scripts").mkdir()
                shutil.copy2(dockerfile, context / "Dockerfile")
                shutil.copy2(helper, context / "scripts" / helper.name)
                code, output = await self._run(
                    "docker",
                    "build",
                    "--file",
                    str(context / "Dockerfile"),
                    "--tag",
                    self.settings.project_image,
                    str(context),
                    timeout=1800,
                )
                if code != 0:
                    lines = [line.strip() for line in output.splitlines() if line.strip()]
                    self._build_error = (lines[-1] if lines else "Developer image build failed")[:500]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._build_error = f"Developer image build failed: {exc}"[:500]
        finally:
            self._build_task = None

    async def close(self) -> None:
        task = self._build_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
