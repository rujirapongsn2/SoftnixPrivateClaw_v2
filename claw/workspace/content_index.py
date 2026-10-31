"""Background text extraction that lets the Files view search file contents.

Indexing rides on the Files listing instead of watching the disk: each new walk
of a user's workspace for the Files view queues the files that are new or whose
mtime changed, and drops rows for files that are gone. One sync per user runs at
a time, and at most one file is parsed at a time across the whole process, in a
limited child process (see extract_worker), so the cost stays bounded no matter
how many users browse or what the files contain.

The index is a cache of the disk. The listing itself still comes from the walk,
so a stale row can never show a deleted file; at worst a file not yet indexed is
found only by its name.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path, PurePosixPath

from loguru import logger

from claw.db.stores import WorkspaceFileTextStore
from claw.workspace.extract_worker import MAX_TEXT_CHARS
from claw.workspace.lifecycle import safe_path

INDEXED_SUFFIXES = frozenset({
    ".pdf", ".docx", ".xlsx", ".html", ".htm", ".md", ".txt", ".csv", ".tsv", ".log", ".json", ".xml", ".a2ui",
})
MAX_FILE_BYTES = 10 * 1024 * 1024
# Files parsed per sync; the next walk picks up where this one stopped.
SYNC_BATCH = 100
# Indexed paths checked against the disk per sync when the walk was cut short
# and so cannot tell which files are gone.
PRUNE_BATCH = 500
EXTRACT_TIMEOUT_S = 30
_SKIPPED_ZONES = frozenset({"scratch"})
_WORKER_ARGS = ("-m", "claw.workspace.extract_worker")


def indexable(entry: dict) -> bool:
    return (
        PurePosixPath(entry["path"]).suffix.lower() in INDEXED_SUFFIXES
        and entry["size"] <= MAX_FILE_BYTES
        and entry["zone"] not in _SKIPPED_ZONES
    )


async def extract_isolated(source: Path) -> str:
    """The file's text from a limited child process; "" when it fails, runs
    out of time, or is killed, so a hostile file is indexed as empty once."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, *_WORKER_ARGS, str(source),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(EXTRACT_TIMEOUT_S):
            stdout, _ = await proc.communicate()
    except TimeoutError:
        await _kill(proc)
        logger.warning("Files index gave up on a file after {}s", EXTRACT_TIMEOUT_S)
        return ""
    except BaseException:
        await _kill(proc)
        raise
    if proc.returncode != 0:
        return ""
    return stdout.decode("utf-8", "replace")[:MAX_TEXT_CHARS]


class ContentIndexer:
    def __init__(self, parallel: int = 1):
        self._parallel = parallel
        # Created on first use so it binds to the loop that runs the syncs.
        self._slots: asyncio.Semaphore | None = None
        self._running: dict[tuple[str, str], asyncio.Task] = {}
        self._pending: dict[tuple[str, str], int] = {}
        self._synced_walk: dict[tuple[str, str], float] = {}

    def indexing(self, mode: str, owner: str) -> bool:
        """True only while files for this user are actually waiting to be read."""
        return self._pending.get((mode, owner), 0) > 0

    def schedule(
        self, store: WorkspaceFileTextStore, mode: str, owner: str, workspace: Path, listing: dict
    ) -> None:
        """Sync this user's index against a walk, once per walk: pages and
        keystrokes served from the same cached walk start nothing."""
        key = (mode, owner)
        walked_at = listing.get("walked_at")
        task = self._running.get(key)
        if (task is not None and not task.done()) or self._synced_walk.get(key) == walked_at:
            return
        self._synced_walk[key] = walked_at
        task = asyncio.create_task(self._sync(store, mode, owner, workspace, listing))
        self._running[key] = task
        task.add_done_callback(lambda done: self._finished(key, done))

    def _finished(self, key: tuple[str, str], task: asyncio.Task) -> None:
        if self._running.get(key) is task:
            del self._running[key]
        self._pending.pop(key, None)

    async def shutdown(self) -> None:
        tasks = [t for t in self._running.values() if not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _sync(self, store: WorkspaceFileTextStore, mode: str, owner: str, workspace: Path, listing: dict) -> None:
        key = (mode, owner)
        files: list[dict] = listing["files"]
        try:
            known = await store.indexed(owner, mode)
            wanted = {f["path"]: f for f in files if indexable(f)}
            missing = [path for path in known if path not in wanted]
            if listing["truncated"]:
                # The walk stopped early, so absence from it proves nothing: ask the disk.
                missing = await asyncio.to_thread(_gone, workspace, missing[:PRUNE_BATCH])
            await store.remove(owner, mode, missing)
            stale = [f for path, f in wanted.items() if known.get(path) != f["modified_at"]]
            stale.sort(key=lambda f: f["modified_at"], reverse=True)
            batch = stale[:SYNC_BATCH]
            self._pending[key] = len(batch)
            if self._slots is None:
                self._slots = asyncio.Semaphore(self._parallel)
            for entry in batch:
                await self._index_one(store, mode, owner, workspace, entry)
                self._pending[key] -= 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - indexing is best effort; search falls back to names
            logger.exception("Files content index sync failed")

    async def _index_one(self, store, mode: str, owner: str, workspace: Path, entry: dict) -> None:
        # One file's failure must not stop the rest: it would stay first in the
        # newest-first queue and block this user's index on every later sync.
        try:
            source = safe_path(workspace, entry["path"])
        except (FileNotFoundError, ValueError):
            return
        try:
            async with self._slots:
                text = await extract_isolated(source)
            await store.put(owner, mode, entry["path"], entry["modified_at"], entry["size"], text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("Files index skipped one file")


async def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await proc.wait()


def _gone(workspace: Path, paths: list[str]) -> list[str]:
    gone = []
    for path in paths:
        try:
            safe_path(workspace, path)
        except (FileNotFoundError, ValueError):
            gone.append(path)
    return gone
