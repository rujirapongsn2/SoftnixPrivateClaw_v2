"""The background loop that runs `FileLifecycle.sweep` over every user workspace."""

import asyncio
import os
import re
import time

from loguru import logger

from claw.workspace.lifecycle import FileLifecycle, SweepResult

_USER_DIR_RE = re.compile(r"^[0-9a-f]{32}$")
# One pass stops after this long and resumes where it left off next time, so
# millions of tenants cannot keep a worker thread busy indefinitely.
_PASS_BUDGET_SECONDS = 120.0
_STARTUP_DELAY_SECONDS = 60.0


class WorkspaceCleanupService:
    def __init__(self, lifecycle: FileLifecycle, policies, settings):
        self.lifecycle = lifecycle
        self.policies = policies
        self.settings = settings
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
        try:
            await self.lifecycle.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Trash reconcile failed")
        await asyncio.sleep(_STARTUP_DELAY_SECONDS)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Workspace cleanup pass failed")
            await asyncio.sleep(max(1, self.settings.workspace.cleanup_interval_minutes) * 60)

    async def run_once(self, *, now: float | None = None) -> SweepResult:
        policy = await self.policies.effective()
        total = SweepResult()
        if not policy.cleanup_enabled:
            return total
        names = await asyncio.to_thread(self._user_dirs)
        # Resume after the last tenant a previous (time-limited) pass reached.
        pending = [name for name in names if name > self._cursor] or names
        started = time.monotonic()
        swept = 0
        for name in pending:
            if time.monotonic() - started > _PASS_BUDGET_SECONDS:
                break
            try:
                result = await self.lifecycle.sweep(name, policy, now=time.time() if now is None else now)
            except Exception:
                logger.exception("Workspace cleanup failed for {}", name)
                result = SweepResult()
            total.add(result, prefix=f"{name}/")
            self._cursor = name
            swept += 1
            await asyncio.sleep(0)
        if swept == len(pending):
            self._cursor = ""
        if total.trashed or total.purged:
            verb = ("trashed", "purged") if policy.enforce else ("would trash", "would purge")
            logger.info(
                "Workspace cleanup {} {} files ({} bytes) and {} {} trash entries across {} workspaces: {}{}",
                verb[0], total.trashed, total.bytes, verb[1], total.purged, swept,
                ", ".join(total.paths), " …" if total.trashed > len(total.paths) else "",
            )
            if not policy.enforce:
                logger.info(
                    "Workspace cleanup is observe-only: nothing was moved. Turn on 'Enforce limits' in "
                    "Control Plane > Preferences > Workspace storage (or CLAW_WORKSPACE__ENFORCE=true)."
                )
        return total

    def _user_dirs(self) -> list[str]:
        try:
            return sorted(
                entry.name
                for entry in os.scandir(self.lifecycle.root)
                if _USER_DIR_RE.match(entry.name) and entry.is_dir(follow_symlinks=False)
            )
        except OSError:
            return []
