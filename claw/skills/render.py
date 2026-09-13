"""Bounded diagram rendering through a disposable worker process."""

import asyncio
import hashlib
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

from claw.core.limits import RateLimiter

_RENDER_SLOTS = asyncio.Semaphore(2)
_EXPORT_LIMITER = RateLimiter(6)
_CACHE_PREFIX = "diagram-render-"
_CACHE_MAX_FILES = 20
_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
_WORKER_TIMEOUT_SECONDS = 30
_WORKER_MEMORY_BYTES = 1024 * 1024 * 1024


def allow_export(user_id: str) -> bool:
    """Bound browser launches per user; cache hits do not consume this budget."""
    return _EXPORT_LIMITER.allow(user_id)


def _source(workspace: Path, path: str) -> tuple[Path, bytes]:
    from claw.api.file_preview import HTML_MAX_BYTES

    root = workspace.resolve()
    source = (root / path.removeprefix("/workspace/")).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError("Diagram file not found in workspace")
    if source.stat().st_size > HTML_MAX_BYTES:
        raise ValueError("Diagram exceeds 2 MB")
    return source, source.read_bytes()


def _cleanup_cache(root: Path, keep: Path) -> None:
    now = time.time()
    cached = sorted(
        (p for p in root.glob(f"{_CACHE_PREFIX}*.png") if p.is_file() and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    kept = 1 if keep in cached else 0
    for item in cached:
        if item == keep:
            continue
        expired = now - item.stat().st_mtime > _CACHE_MAX_AGE_SECONDS
        if kept >= _CACHE_MAX_FILES or expired:
            item.unlink(missing_ok=True)
            item.with_suffix(".json").unlink(missing_ok=True)
        else:
            kept += 1


def _linux_process_group_rss(group_id: int) -> int:
    """Return resident bytes for the disposable worker's whole process group."""
    total_kib = 0
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return 0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            fields = stat[stat.rfind(")") + 2 :].split()
            if int(fields[2]) != group_id:  # field 5: process group id
                continue
            status = (entry / "status").read_text()
            match = next((line for line in status.splitlines() if line.startswith("VmRSS:")), "")
            total_kib += int(match.split()[1]) if match else 0
        except (OSError, ValueError, IndexError):
            continue
    return total_kib * 1024


async def _memory_guard(process_group: int, exceeded: asyncio.Event) -> None:
    if not sys.platform.startswith("linux"):
        return
    while True:
        await asyncio.sleep(0.1)
        rss = await asyncio.to_thread(_linux_process_group_rss, process_group)
        if rss > _WORKER_MEMORY_BYTES:
            exceeded.set()
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return


async def render_diagram(
    workspace: Path,
    path: str,
    width: int = 1440,
    height: int = 1000,
    *,
    user_id: str | None = None,
):
    root = workspace.resolve()
    source, raw = await asyncio.to_thread(_source, root, path)
    if not 320 <= width <= 2400 or not 240 <= height <= 2400:
        raise ValueError("Canvas must be between 320×240 and 2400×2400")

    digest = hashlib.sha256(raw + f"\0{width}x{height}\0v2".encode()).hexdigest()[:32]
    target = root / f"{_CACHE_PREFIX}{digest}.png"
    metadata = target.with_suffix(".json")
    if target.is_file() and not target.is_symlink() and metadata.is_file():
        try:
            result = json.loads(metadata.read_text())
            result["path"] = target.name
            return result
        except (OSError, ValueError, TypeError):
            target.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)

    if user_id is not None and not allow_export(user_id):
        raise PermissionError("PNG export rate limit exceeded; wait a minute and try again")

    temporary = root / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    async with asyncio.timeout(_WORKER_TIMEOUT_SECONDS):
        async with _RENDER_SLOTS:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "claw.skills.render_worker",
                str(root),
                str(source),
                str(temporary),
                str(width),
                str(height),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            memory_exceeded = asyncio.Event()
            memory_guard = asyncio.create_task(_memory_guard(proc.pid, memory_exceeded))
            try:
                stdout, stderr = await proc.communicate()
            except BaseException:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
                temporary.unlink(missing_ok=True)
                raise
            finally:
                memory_guard.cancel()
                await asyncio.gather(memory_guard, return_exceptions=True)
    if memory_exceeded.is_set():
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Diagram renderer exceeded its 1 GB memory limit")
    if proc.returncode != 0:
        temporary.unlink(missing_ok=True)
        detail = stderr.decode("utf-8", "replace")[-2000:].strip()
        raise RuntimeError(detail or "Diagram renderer failed")
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Diagram renderer returned an invalid response") from exc
    if not temporary.is_file() or temporary.is_symlink() or temporary.stat().st_size > 25 * 1024 * 1024:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Diagram renderer did not produce a bounded PNG")
    temporary.replace(target)
    result["path"] = target.name
    metadata.write_text(json.dumps(result, separators=(",", ":")))
    await asyncio.to_thread(_cleanup_cache, root, target)
    return result
