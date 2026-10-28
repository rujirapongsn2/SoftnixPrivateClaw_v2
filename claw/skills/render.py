"""Bounded diagram rendering through a disposable worker process."""

import asyncio
import contextlib
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
# Resident memory budget for the worker *and every Chromium process it spawns*,
# enforced by _memory_guard. Not RLIMIT_AS: Chromium reserves ~1.4 TiB of
# virtual address space per renderer (V8 sandbox) while touching a few hundred
# MiB, so any practical address-space cap kills it at launch with SIGTRAP.
_WORKER_MEMORY_BYTES = 1024 * 1024 * 1024
_WORKER_ARGS: tuple[str, ...] = ("-m", "claw.skills.render_worker")
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
# Worker exit status for a diagram it refused (message on stdout as JSON).
REJECTED_EXIT_CODE = 2


class RenderRejected(ValueError):
    """The diagram or request can't be rendered as given. The message is ours,
    safe to show, and tells the caller what to change."""


def allow_export(user_id: str) -> bool:
    """Bound browser launches per user; cache hits do not consume this budget."""
    return _EXPORT_LIMITER.allow(user_id)


def _source(workspace: Path, path: str) -> tuple[Path, bytes]:
    from claw.api.file_preview import HTML_MAX_BYTES

    root = workspace.resolve()
    source = (root / path.removeprefix("/workspace/")).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise RenderRejected("Diagram file not found in workspace")
    if source.stat().st_size > HTML_MAX_BYTES:
        raise RenderRejected("Diagram exceeds 2 MB")
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


def _proc_stat(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the command name (index 0 = field 3)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat[stat.rfind(")") + 2 :].split()


def _linux_process_tree(root_pid: int) -> dict[int, tuple[int, int]]:
    """Map pid -> (start time, resident bytes) for root_pid and its descendants.

    Follows parent links, not the process group: Playwright launches Chromium
    detached, in a process group of its own, so a group-based view misses it."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return {}
    children: dict[int, list[int]] = {}
    info: dict[int, tuple[int, int]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        fields = _proc_stat(int(entry.name))
        try:
            if fields is None or fields[0] == "Z":
                continue
            # fields[1] ppid, fields[19] start time, fields[21] RSS pages.
            pid = int(entry.name)
            children.setdefault(int(fields[1]), []).append(pid)
            info[pid] = (int(fields[19]), int(fields[21]) * _PAGE_SIZE)
        except (ValueError, IndexError):
            continue
    tree: dict[int, tuple[int, int]] = {}
    frontier = [root_pid] if root_pid in info else []
    while frontier:
        pid = frontier.pop()
        tree[pid] = info[pid]
        frontier.extend(child for child in children.get(pid, ()) if child in info and child not in tree)
    return tree


def _kill_tracked(worker_pid: int, seen: dict[int, int]) -> None:
    """SIGKILL the worker's group and every descendant seen while it ran.

    A pid is signalled only while its start time still matches, so a recycled
    pid that now belongs to an unrelated process is never touched."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(worker_pid, signal.SIGKILL)
    for pid, started in seen.items():
        fields = _proc_stat(pid)
        try:
            if fields is not None and int(fields[19]) == started:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError, IndexError):
            continue


async def _memory_guard(worker_pid: int, exceeded: asyncio.Event, seen: dict[int, int]) -> None:
    if not sys.platform.startswith("linux"):
        return
    while True:
        tree = await asyncio.to_thread(_linux_process_tree, worker_pid)
        seen.update({pid: started for pid, (started, _) in tree.items()})
        if sum(rss for _, rss in tree.values()) > _WORKER_MEMORY_BYTES:
            exceeded.set()
            await asyncio.to_thread(_kill_tracked, worker_pid, seen)
            return
        await asyncio.sleep(0.1)


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
        raise RenderRejected("Canvas must be between 320×240 and 2400×2400")

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
        raise RenderRejected("PNG export rate limit exceeded; wait a minute and try again")

    temporary = root / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    async with asyncio.timeout(_WORKER_TIMEOUT_SECONDS):
        async with _RENDER_SLOTS:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                *_WORKER_ARGS,
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
            seen: dict[int, int] = {}
            memory_guard = asyncio.create_task(_memory_guard(proc.pid, memory_exceeded, seen))
            try:
                stdout, stderr = await proc.communicate()
            except BaseException:
                # Timeout or cancellation: nothing from this render may outlive it.
                _kill_tracked(proc.pid, seen)
                await proc.wait()
                temporary.unlink(missing_ok=True)
                raise
            finally:
                memory_guard.cancel()
                await asyncio.gather(memory_guard, return_exceptions=True)
                # Also covers a worker that died (e.g. CPU limit) with Chromium still up.
                _kill_tracked(proc.pid, seen)
    if memory_exceeded.is_set():
        temporary.unlink(missing_ok=True)
        raise RenderRejected("Diagram needs more than 1 GB to render; simplify it or use a smaller canvas")
    if proc.returncode == REJECTED_EXIT_CODE:
        temporary.unlink(missing_ok=True)
        try:
            reason = str(json.loads(stdout.decode("utf-8"))["error"])
        except (UnicodeDecodeError, ValueError, TypeError, KeyError):
            reason = "Diagram was rejected by the renderer"
        raise RenderRejected(reason)
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
        raise RenderRejected("Rendered PNG exceeds 25 MB; use a smaller canvas")
    temporary.replace(target)
    result["path"] = target.name
    metadata.write_text(json.dumps(result, separators=(",", ":")))
    await asyncio.to_thread(_cleanup_cache, root, target)
    return result
