"""Retention cleanup for user workspaces.

Two places are swept, and nothing else:

* `.tmp/` — scratch space the agent is told to use. Any file older than the
  retention is deleted, then empty folders left behind.
* `uploads/` — user attachments, one flat folder. Generated images
  (`generated-*`) are not attachments: they are chat content and have their own
  cap, so they are left alone.

Age is the file's modification time. Symlinks are never followed or deleted, so
a link planted in a workspace cannot steer a delete outside it.
"""

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

TMP_DIR = ".tmp"
UPLOADS_DIR = "uploads"
GENERATED_PREFIX = "generated-"
_DAY = 86400
# An empty scratch folder is removed once it has sat untouched this long. Not
# at once: a running command may have just made it and be about to fill it.
_EMPTY_DIR_GRACE = 3600
_USER_DIR_RE = re.compile(r"^[0-9a-f]{32}$")
# One pass stops after this long and resumes where it left off next time, so
# millions of tenants cannot keep a worker thread busy indefinitely.
_PASS_BUDGET_SECONDS = 120.0
_STARTUP_DELAY_SECONDS = 60.0


# How many deleted paths one pass records in the audit log and in the log line, so
# "what was removed?" has an answer without logging every file of a large sweep.
_SAMPLE_SIZE = 20


@dataclass
class SweepResult:
    files: int = 0
    bytes: int = 0
    # Workspace-relative paths of (some of) the deleted files, for the audit trail.
    paths: list[str] = field(default_factory=list)

    def add(self, other: "SweepResult", prefix: str = "") -> None:
        self.files += other.files
        self.bytes += other.bytes
        room = _SAMPLE_SIZE - len(self.paths)
        if room > 0:
            self.paths.extend(f"{prefix}{p}" for p in other.paths[:room])


def _remove_file(path: str, size: int, result: SweepResult, dry_run: bool, base: str) -> None:
    if not dry_run:
        try:
            os.unlink(path)
        except OSError:
            return
    result.files += 1
    result.bytes += size
    if len(result.paths) < _SAMPLE_SIZE:
        result.paths.append(os.path.relpath(path, base))


def _sweep_tmp(tmp: Path, cutoff: float, dir_cutoff: float, dry_run: bool, base: str) -> SweepResult:
    result = SweepResult()
    if tmp.is_symlink() or not tmp.is_dir():
        return result
    for directory, _dirs, names in os.walk(tmp, topdown=False, followlinks=False):
        for name in names:
            path = os.path.join(directory, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            if info.st_mtime < cutoff:
                _remove_file(path, info.st_size, result, dry_run, base)
        if directory != str(tmp) and not dry_run:
            try:
                if not os.path.islink(directory) and os.lstat(directory).st_mtime < dir_cutoff:
                    os.rmdir(directory)  # only succeeds when it is empty
            except OSError:
                pass
    return result


def _sweep_uploads(uploads: Path, cutoff: float, dry_run: bool, base: str) -> SweepResult:
    result = SweepResult()
    if uploads.is_symlink() or not uploads.is_dir():
        return result
    try:
        entries = list(os.scandir(uploads))
    except OSError:
        return result
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            if entry.name.startswith(GENERATED_PREFIX):
                continue
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if info.st_mtime < cutoff:
            _remove_file(entry.path, info.st_size, result, dry_run, base)
    return result


def sweep_workspace(
    workspace: Path, *, now: float, tmp_days: int, uploads_days: int, dry_run: bool = False
) -> SweepResult:
    """Delete expired files in one workspace. Blocking: call via a thread."""
    result = SweepResult()
    if tmp_days > 0:
        result.add(_sweep_tmp(workspace / TMP_DIR, now - tmp_days * _DAY, now - _EMPTY_DIR_GRACE, dry_run, str(workspace)))
    if uploads_days > 0:
        result.add(_sweep_uploads(workspace / UPLOADS_DIR, now - uploads_days * _DAY, dry_run, str(workspace)))
    return result


class WorkspaceCleanupService:
    def __init__(self, workspaces_root: Path, policies, settings, audit=None):
        self.root = workspaces_root
        self.policies = policies
        self.settings = settings
        self.audit = audit
        self._task: asyncio.Task | None = None
        self._cursor = ""

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="workspace-cleanup")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(_STARTUP_DELAY_SECONDS)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Workspace cleanup pass failed")
            await asyncio.sleep(max(1, self.settings.workspace.cleanup_interval_minutes) * 60)

    async def run_once(self) -> SweepResult:
        policy = await self.policies.effective()
        total = SweepResult()
        if not policy.cleanup_enabled:
            return total
        # Observe-only until an administrator (or a fresh install) turns enforcement on.
        dry_run = not getattr(policy, "enforce", True)
        names = await asyncio.to_thread(self._user_dirs)
        # Resume after the last tenant a previous (time-limited) pass reached.
        pending = [name for name in names if name > self._cursor] or names
        started = time.monotonic()
        swept = 0
        for name in pending:
            if time.monotonic() - started > _PASS_BUDGET_SECONDS:
                break
            result = await asyncio.to_thread(
                sweep_workspace,
                self.root / name,
                now=time.time(),
                tmp_days=policy.tmp_retention_days,
                uploads_days=policy.uploads_retention_days,
                dry_run=dry_run,
            )
            total.add(result, prefix=f"{name}/")
            self._cursor = name
            swept += 1
            await asyncio.sleep(0)
        if swept == len(pending):
            self._cursor = ""
        if total.files:
            logger.info(
                "Workspace cleanup {}{} files ({} bytes) across {} workspaces: {}{}",
                "would delete " if dry_run else "deleted ",
                total.files,
                total.bytes,
                swept,
                ", ".join(total.paths),
                " …" if total.files > len(total.paths) else "",
            )
            if dry_run:
                logger.info(
                    "Workspace cleanup is observe-only: nothing was deleted. Turn on 'Enforce limits' in "
                    "Control Plane > Preferences > Workspace storage (or CLAW_WORKSPACE__ENFORCE=true) to delete."
                )
            if self.audit is not None and not dry_run:
                await self.audit.log(
                    "workspace_cleanup",
                    {"files": total.files, "bytes": total.bytes, "workspaces": swept, "paths": total.paths},
                )
        return total

    def _user_dirs(self) -> list[str]:
        try:
            return sorted(
                entry.name
                for entry in os.scandir(self.root)
                if _USER_DIR_RE.match(entry.name) and entry.is_dir(follow_symlinks=False)
            )
        except OSError:
            return []
