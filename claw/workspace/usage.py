"""Measuring a user's workspace and enforcing the storage quota.

Measuring walks the tree, which costs ~10 ms per 1,000 files, so it always runs
off the event loop and its result is cached briefly. The quota is therefore a
guard on the *write paths* (file tools, uploads, connector downloads, shell
commands), not an exact byte counter: a single command can overshoot, after
which further writes are refused until space is freed.
"""

import asyncio
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

# A tree beyond this many files is measured as "at least this many" — which is
# already far past any quota, so the walk can stop instead of running for minutes.
MEASURE_FILE_CAP = 1_000_000
_TOP_FILES = 5
# How long a measurement is trusted before a write re-measures. Writes made in
# the meantime are added to it (see `note_written`), so a burst cannot slip under.
_CACHE_SECONDS = 30.0
# After a command that may have written (`forget`) the next check measures again,
# but never more often than this: an agent running commands back to back must not
# make every one of them wait for a walk of the whole tree.
_MIN_REMEASURE_SECONDS = 5.0
# The walk stops this many times past the file quota (and no earlier than the
# floor): a tree that big is over the quota whatever its exact size, and counting
# a million files to say so is the cost this avoids.
_CAP_FACTOR = 2
_CAP_FLOOR = 10_000
_MAX_TRACKED_USERS = 4096
_WARN_EVERY_SECONDS = 600.0

_MB = 1024 * 1024


@dataclass(frozen=True)
class Usage:
    bytes: int = 0
    files: int = 0
    # False when the walk stopped at MEASURE_FILE_CAP (so the numbers are lower bounds).
    complete: bool = True
    largest: tuple[tuple[str, int], ...] = field(default_factory=tuple)


def measure(root: Path, *, top: int = _TOP_FILES, file_cap: int = MEASURE_FILE_CAP) -> Usage:
    """Total size and file count under `root`. Blocking: call via a thread.

    Symlinks are never followed (a link out of the workspace must neither count
    nor be walked), and unreadable entries are skipped rather than failing.
    """
    total = files = 0
    biggest: list[tuple[int, str]] = []
    complete = True
    stack = [str(root)]
    base_len = len(str(root)) + 1
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            size = entry.stat(follow_symlinks=False).st_size
                            total += size
                            files += 1
                            if len(biggest) < top or size > biggest[-1][0]:
                                biggest.append((size, entry.path[base_len:]))
                                biggest.sort(reverse=True)
                                del biggest[top:]
                            if files >= file_cap:
                                complete = False
                                stack.clear()
                                break
                    except OSError:
                        continue
        except OSError:
            continue
    return Usage(total, files, complete, tuple((path, size) for size, path in biggest))


def _human(size: int) -> str:
    if size >= 1024 * _MB:
        return f"{size / (1024 * _MB):.1f} GB"
    if size >= _MB:
        return f"{size / _MB:.1f} MB"
    return f"{size / 1024:.0f} KB"


# Commands that only look at or remove files. With the workspace over quota the
# agent still has to be able to clean up, or it could never get back under it.
_CLEANUP_COMMANDS = {
    "rm", "rmdir", "unlink", "ls", "du", "df", "stat", "wc", "cat", "head", "tail", "echo",
    "true", "pwd", "find",
}
_FIND_UNSAFE = ("-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls")
_SEPARATORS = ("&&", "||", ";", "|", "\n", "&")


def is_cleanup_command(command: str) -> bool:
    """True when every part of `command` only inspects or deletes files."""
    cleaned = command.replace("2>/dev/null", " ").replace(">/dev/null", " ").replace("2>&1", " ")
    if ">" in cleaned or "<" in cleaned or "$(" in cleaned or "`" in cleaned:
        return False
    parts = [cleaned]
    for separator in _SEPARATORS:
        parts = [piece for part in parts for piece in part.split(separator)]
    seen = False
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            tokens = shlex.split(part)
        except ValueError:
            return False
        if not tokens:
            continue
        name = os.path.basename(tokens[0])
        if name not in _CLEANUP_COMMANDS:
            return False
        if name == "find" and any(arg in _FIND_UNSAFE for arg in tokens[1:]):
            return False
        seen = True
    return seen


class WorkspaceAccounts:
    """Cached per-user usage plus the quota decision built on it."""

    def __init__(self, workspaces_root: Path, policies):
        self.root = workspaces_root
        self.policies = policies
        self._usage: dict[str, tuple[float, Usage]] = {}
        # Users whose cached figure is known to be out of date (a command ran since).
        self._dirty: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._warned: dict[str, float] = {}

    def _workspace(self, user_id: str) -> Path:
        return self.root / user_id

    def _remember(self, user_id: str, usage: Usage) -> None:
        if len(self._usage) >= _MAX_TRACKED_USERS:
            oldest = min(self._usage, key=lambda key: self._usage[key][0])
            self._usage.pop(oldest, None)
            self._locks.pop(oldest, None)
            self._dirty.discard(oldest)
            self._warned.pop(oldest, None)
        self._usage[user_id] = (time.monotonic(), usage)
        self._dirty.discard(user_id)

    def _fresh(self, user_id: str, max_age: float) -> Usage | None:
        cached = self._usage.get(user_id)
        if cached is None:
            return None
        age = time.monotonic() - cached[0]
        limit = min(max_age, _MIN_REMEASURE_SECONDS) if user_id in self._dirty else max_age
        return cached[1] if age < limit else None

    async def usage(self, user_id: str, *, max_age: float = _CACHE_SECONDS, file_cap: int = MEASURE_FILE_CAP) -> Usage:
        fresh = self._fresh(user_id, max_age)
        if fresh is not None:
            return fresh
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            # Another task may have measured while this one waited for the lock.
            fresh = self._fresh(user_id, max_age)
            if fresh is not None:
                return fresh
            usage = await asyncio.to_thread(measure, self._workspace(user_id), file_cap=file_cap)
            self._remember(user_id, usage)
            return usage

    def note_written(self, user_id: str, nbytes: int, nfiles: int = 1) -> None:
        """Count a write made after the last measurement, so a burst of writes
        inside the cache window is still seen by the next quota check."""
        cached = self._usage.get(user_id)
        if cached is None:
            return
        stamp, usage = cached
        self._usage[user_id] = (
            stamp,
            Usage(usage.bytes + max(0, nbytes), usage.files + max(0, nfiles), usage.complete, usage.largest),
        )

    def forget(self, user_id: str, *, now: bool = False) -> None:
        """Mark the cached measurement out of date (a command that may have written ran).

        It is still reused for a few seconds, so back-to-back commands do not each
        trigger a walk; after that the next check measures again. `now=True` drops
        it at once, for deletions: space that was just freed must be seen by the
        very next write, not refused on a stale figure."""
        if now:
            self._usage.pop(user_id, None)
            self._dirty.discard(user_id)
        elif user_id in self._usage:
            self._dirty.add(user_id)

    async def check(
        self, user_id: str, incoming_bytes: int = 0, incoming_files: int = 1, *, max_age: float = _CACHE_SECONDS
    ) -> str | None:
        """None when the write may proceed, else the message to show instead."""
        policy = await self.policies.effective()
        limit_bytes = policy.quota_bytes
        limit_files = policy.quota_files
        if not limit_bytes and not limit_files:
            return None
        cap = max(_CAP_FLOOR, limit_files * _CAP_FACTOR) if limit_files else MEASURE_FILE_CAP
        usage = await self.usage(user_id, max_age=max_age, file_cap=min(cap, MEASURE_FILE_CAP))
        over_bytes = bool(limit_bytes) and usage.bytes + incoming_bytes > limit_bytes
        over_files = bool(limit_files) and usage.files + incoming_files > limit_files
        if not (over_bytes or over_files):
            return None
        if not getattr(policy, "enforce", True):
            self._warn_observed(user_id, usage, policy)
            return None
        return self.full_message(usage, policy, incoming_bytes)

    def _warn_observed(self, user_id: str, usage: Usage, policy) -> None:
        """Observe-only mode: say in the log that the quota WOULD have refused this write."""
        now = time.monotonic()
        if now - self._warned.get(user_id, -_WARN_EVERY_SECONDS) < _WARN_EVERY_SECONDS:
            return
        self._warned[user_id] = now
        logger.warning(
            "Workspace {} is over its quota ({} bytes, {} files) but limits are not enforced yet "
            "(observe-only); turn on 'Enforce limits' in the Control Plane to block writes",
            user_id,
            usage.bytes,
            usage.files,
        )

    @staticmethod
    def full_message(usage: Usage, policy, incoming_bytes: int = 0) -> str:
        parts = []
        if policy.quota_mb:
            parts.append(f"{_human(usage.bytes)} of {_human(policy.quota_bytes)}")
        if policy.quota_files:
            parts.append(f"{usage.files}{'+' if not usage.complete else ''} of {policy.quota_files} files")
        message = (
            "Error: the workspace storage quota is full (" + ", ".join(parts) + "). Nothing was written. "
            "Delete files that are no longer needed (for example with `rm`), then try again."
        )
        if usage.largest:
            message += " Largest files: " + ", ".join(f"{path} ({_human(size)})" for path, size in usage.largest) + "."
        return message

    async def gate(self, user_id: str, name: str, params: dict) -> str | None:
        """Pre-execution check for the tools that write into the workspace."""
        if name == "exec":
            command = str(params.get("command") or "")
            message = await self.check(user_id, 0, 0, max_age=10.0)
            if message is None:
                return None
            return None if is_cleanup_command(command) else message
        if name == "write_file":
            return await self.check(user_id, len(str(params.get("content") or "").encode("utf-8")), 1)
        if name == "edit_file":
            grown = len(str(params.get("new_text") or "")) - len(str(params.get("old_text") or ""))
            return await self.check(user_id, max(0, grown), 0)
        if name in {"generate_workbook", "render_diagram"}:
            return await self.check(user_id, 0, 1)
        return None
