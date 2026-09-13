"""Admin control and readiness checks for persistent project containers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


_SIZE_UNITS = {
    "b": 1,
    "kb": 1_000,
    "mb": 1_000**2,
    "gb": 1_000**3,
    "tb": 1_000**4,
    "kib": 1_024,
    "mib": 1_024**2,
    "gib": 1_024**3,
    "tib": 1_024**4,
}
_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b)$", re.IGNORECASE)
_EMPTY_METRICS = {
    "metrics_available": False,
    "containers": {"running": 0, "stopped": 0, "total": 0},
    "cpu_percent": 0.0,
    "memory_usage_bytes": 0,
    "memory_limit_bytes": 0,
    "disk_usage_bytes": 0,
    "disk_usage_complete": True,
}


def _size_bytes(value: str) -> int:
    match = _SIZE_RE.fullmatch(value.strip())
    if match is None:
        return 0
    return round(float(match.group(1)) * _SIZE_UNITS[match.group(2).lower()])


def _directory_size(root: Path, max_entries: int = 250_000) -> tuple[int, bool]:
    """Return allocated bytes without following tenant-controlled symlinks."""
    total = 0
    entries = 0
    pending = [root]
    try:
        while pending:
            current = pending.pop()
            with os.scandir(current) as children:
                for child in children:
                    entries += 1
                    if entries > max_entries:
                        return total, False
                    try:
                        stat = child.stat(follow_symlinks=False)
                        total += getattr(stat, "st_blocks", 0) * 512 or stat.st_size
                        if child.is_dir(follow_symlinks=False):
                            pending.append(Path(child.path))
                    except OSError:
                        return total, False
    except OSError:
        return total, False
    return total, True


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
        self._metrics_lock = asyncio.Lock()
        self._metrics_cache: dict | None = None
        self._metrics_cached_at = 0.0
        self._disk_cache: tuple[int, bool] | None = None
        self._disk_cached_at = 0.0

    async def load(self) -> None:
        config = await self.config_store.get(default_enabled=self.settings.projects_enabled)
        self.settings.projects_enabled = config["enabled"]
        if self.settings.enabled and self.settings.projects_enabled:
            status = await self.status()
            if status["docker_available"] and not status["image_available"]:
                await self.start_build()

    async def _run(self, *argv: str, timeout: float = 20, output_cap: int = 20_000) -> tuple[int, str]:
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
                del tail[:-output_cap]

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
        if docker_available:
            image_available, metrics = await asyncio.gather(self._image_ready(), self._runtime_metrics())
        else:
            image_available, metrics = False, dict(_EMPTY_METRICS)
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
            **metrics,
        }

    async def _runtime_metrics(self) -> dict:
        now = time.monotonic()
        if self._metrics_cache is not None and now - self._metrics_cached_at < 5:
            return self._metrics_cache
        async with self._metrics_lock:
            now = time.monotonic()
            if self._metrics_cache is not None and now - self._metrics_cached_at < 5:
                return self._metrics_cache
            try:
                metrics = await self._collect_runtime_metrics()
            except (OSError, RuntimeError):
                metrics = {**_EMPTY_METRICS, "disk_usage_complete": False}
            self._metrics_cache = metrics
            self._metrics_cached_at = time.monotonic()
            return metrics

    async def _collect_runtime_metrics(self) -> dict:
        code, output = await self._run(
            "docker", "ps", "--all", "--filter", "label=sbot.project", "--format", "{{json .}}",
            timeout=10, output_cap=1_000_000,
        )
        if code != 0:
            return dict(_EMPTY_METRICS)
        rows = []
        for line in output.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("Names"):
                rows.append(row)
        names = [str(row["Names"]) for row in rows]
        running_names = [str(row["Names"]) for row in rows if str(row.get("State", "")).lower() == "running"]
        metrics = {
            "metrics_available": True,
            "containers": {
                "running": len(running_names),
                "stopped": len(names) - len(running_names),
                "total": len(names),
            },
            "cpu_percent": 0.0,
            "memory_usage_bytes": 0,
            "memory_limit_bytes": 0,
            "disk_usage_bytes": 0,
            "disk_usage_complete": True,
        }
        if running_names:
            stats_code, stats_output = await self._run(
                "docker", "stats", "--no-stream", "--format", "{{json .}}", *running_names, timeout=15
            )
            if stats_code == 0:
                for line in stats_output.splitlines():
                    try:
                        stat = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        metrics["cpu_percent"] += float(str(stat.get("CPUPerc", "0")).rstrip("%"))
                    except ValueError:
                        pass
                    usage, _, limit = str(stat.get("MemUsage", "")).partition("/")
                    metrics["memory_usage_bytes"] += _size_bytes(usage)
                    metrics["memory_limit_bytes"] += _size_bytes(limit)
            else:
                metrics["metrics_available"] = False
        if not names:
            self._disk_cache = (0, True)
            self._disk_cached_at = time.monotonic()
        elif self._disk_cache is None or time.monotonic() - self._disk_cached_at >= 60:
            inspect_code, inspect_output = await self._run(
                "docker", "container", "inspect", "--size", *names, timeout=20, output_cap=2_000_000
            )
            if inspect_code == 0:
                try:
                    inspected = json.loads(inspect_output)
                except json.JSONDecodeError:
                    inspected = []
                    metrics["disk_usage_complete"] = False
                sources: set[Path] = set()
                for container in inspected if isinstance(inspected, list) else []:
                    metrics["disk_usage_bytes"] += max(0, int(container.get("SizeRw") or 0))
                    for mount in container.get("Mounts") or []:
                        source = Path(str(mount.get("Source", "")))
                        if mount.get("Destination") not in {"/workspace", "/var/lib/docker"}:
                            continue
                        if source.is_dir() and not source.is_symlink():
                            sources.add(source)
                        else:
                            metrics["disk_usage_complete"] = False

                semaphore = asyncio.Semaphore(4)

                async def measure(source: Path) -> tuple[int, bool]:
                    async with semaphore:
                        return await asyncio.to_thread(_directory_size, source)

                for size, complete in await asyncio.gather(*(measure(source) for source in sources)):
                    metrics["disk_usage_bytes"] += size
                    metrics["disk_usage_complete"] = metrics["disk_usage_complete"] and complete
            else:
                metrics["disk_usage_complete"] = False
            self._disk_cache = (metrics["disk_usage_bytes"], metrics["disk_usage_complete"])
            self._disk_cached_at = time.monotonic()
        else:
            assert self._disk_cache is not None
            metrics["disk_usage_bytes"], metrics["disk_usage_complete"] = self._disk_cache
        metrics["cpu_percent"] = round(metrics["cpu_percent"], 1)
        return metrics

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
