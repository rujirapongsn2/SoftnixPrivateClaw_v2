"""Owner-scoped, recoverable file lifecycle. Files never follow a deleted chat.

Every workspace file belongs to a zone with its own retention. An expired file
moves to a per-owner trash outside the workspace, stays recoverable for
`trash_retention_days`, and is then purged. Trash and its journal live under
`<workspaces_root>/_file_lifecycle/<sha256(owner)>/`, one directory per entry
holding `record.json` and `data`. Entry ids start with the trash time in hex
milliseconds, so sorting ids sorts entries by age. `index/` holds one empty
marker per entry, `<path key>-<id>`, so a missing path maps to its trash entry
with one directory listing.

Workspace-side moves go through directory descriptors opened with O_NOFOLLOW,
so a sandbox that swaps a folder for a symlink mid-operation cannot steer a
move outside the workspace. No schema migration is needed.
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath

from loguru import logger
from sqlalchemy import Text, cast, inspect, select

DAY = 86400
MOVES_PER_PASS = 500
RECONCILE_MAX_ENTRIES = 100_000
SAMPLE_SIZE = 20
TERMINAL_JOBS = frozenset({"completed", "failed", "cancelled", "succeeded"})
ACTIVE_ARTIFACT_JOBS = frozenset({"queued", "running", "waiting_dependency", "blocked", "recovering"})
FINISHED_MISSIONS = frozenset({"completed", "failed", "cancelled"})
# A job that has not changed for this long is stuck, not working. It must not pause an
# owner's cleanup forever (a blocked or paused job never reaches a finished state).
STALE_WORK_DAYS = 7
# Folders the backend itself keeps in a PrivateClaw workspace.
SYSTEM_DIRS = frozenset({"blueprints", "skills"})
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ID = re.compile(r"^[0-9a-f]{32}$")
_LOCKS_KEPT = 1024


class Zone(StrEnum):
    SYSTEM = "system"
    USER = "user"
    AI = "ai"
    SCRATCH = "scratch"


RETENTION_FIELD: dict[Zone, str] = {
    Zone.SCRATCH: "tmp_retention_days",
    Zone.USER: "uploads_retention_days",
    Zone.AI: "ai_retention_days",
}


def zone_of(mode: str, relative: str) -> Zone:
    parts = PurePosixPath(relative).parts
    top = parts[0]
    if top.startswith("."):
        return Zone.SCRATCH if top == ".tmp" else Zone.SYSTEM
    if top == "uploads" and len(parts) > 1:
        if not parts[-1].startswith("generated-"):
            return Zone.USER
        return Zone.SYSTEM if mode == "sbot" else Zone.AI
    # Sbot workspaces hold project folders, which no age rule may touch.
    if mode == "sbot" or top in SYSTEM_DIRS:
        return Zone.SYSTEM
    return Zone.AI


def retention_days(policy, zone: Zone) -> int:
    name = RETENTION_FIELD.get(zone)
    return getattr(policy, name) if name else 0


class FileConflict(Exception):
    pass


@dataclass
class SweepResult:
    trashed: int = 0
    purged: int = 0
    bytes: int = 0
    paths: list[str] = field(default_factory=list)

    def add(self, other: "SweepResult", prefix: str = "") -> None:
        self.trashed += other.trashed
        self.purged += other.purged
        self.bytes += other.bytes
        room = SAMPLE_SIZE - len(self.paths)
        if room > 0:
            self.paths.extend(f"{prefix}{p}" for p in other.paths[:room])


def _parts(relative: str) -> tuple[str, ...]:
    parts = PurePosixPath(relative).parts if relative else ()
    if (
        not parts
        or parts[0] == "/"
        or "\x00" in relative
        or any(p in {"..", ".git", ".env"} or p.startswith(".env.") for p in parts)
    ):
        raise FileNotFoundError(relative)
    return parts


def safe_path(root: Path, relative: str, *, exists: bool = True) -> Path:
    """Path-based check for reads. Moves use `_dir_fd` instead."""
    root = root.absolute()
    try:
        if root.is_symlink():
            raise FileNotFoundError(relative)
        path = root
        for part in _parts(relative):
            path = path / part
            if path.is_symlink():
                raise FileNotFoundError(relative)
        if exists and not path.is_file():
            raise FileNotFoundError(relative)
    except OSError as exc:
        # A name the filesystem cannot hold (ENAMETOOLONG and the like) names no file.
        raise FileNotFoundError(relative) from exc
    return path


@contextmanager
def _dir_fd(base: Path, names, *, create: bool = False):
    """Descriptor of `base/names...`, opened one component at a time without following links."""
    try:
        fd = os.open(base, _DIR_FLAGS)
    except OSError as exc:
        raise FileNotFoundError(str(base)) from exc
    try:
        for name in names:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(name, 0o755, dir_fd=fd)
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except NotADirectoryError as exc:
                # macOS reports a symlink as ENOTDIR here too; that stays not found.
                if create and not stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    raise FileConflict("A file is in the way of the original folder.") from exc
                raise FileNotFoundError(name) from exc
            except OSError as exc:
                raise FileNotFoundError(name) from exc
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _control_path(root: Path, owner: str) -> Path:
    return root.absolute() / "_file_lifecycle" / hashlib.sha256(owner.encode("utf-8")).hexdigest()


def control_root(root: Path, owner: str) -> Path:
    path = _control_path(root, owner)
    for parent in (path.parent, path):
        if parent.is_symlink():
            raise FileNotFoundError(owner)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _new_id(now: float) -> str:
    return f"{int(now * 1000):012x}{secrets.token_hex(10)}"


def _id_time(ident: str) -> float:
    return int(ident[:12], 16) / 1000


def _path_key(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, _DIR_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path: Path, value: dict, *, durable: bool = True) -> None:
    # A stale temp file after a crash must never block a later recovery attempt.
    temp = path.parent / f".record-{uuid.uuid4().hex}.new"
    try:
        with open(temp, "x", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False)
            if durable:
                out.flush()
                os.fsync(out.fileno())
        os.replace(temp, path)
        if durable:
            _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict | None:
    try:
        if path.is_symlink():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _data_mode(entry_dir: Path) -> int | None:
    try:
        return os.lstat(entry_dir / "data").st_mode
    except OSError:
        return None


def _marker(entry: dict) -> str:
    return f"{_path_key(entry['path'])}-{entry['id']}"


def _drop_entry(control: Path, ident: str, path: str | None) -> None:
    if path is not None:
        (control / "index" / f"{_path_key(path)}-{ident}").unlink(missing_ok=True)
    shutil.rmtree(control / ident, ignore_errors=True)


_locks: OrderedDict[tuple[str, str], asyncio.Lock] = OrderedDict()


def _owner_lock(root: Path, owner: str) -> asyncio.Lock:
    """Keeps the API and the sweeper of one process from interleaving on one owner."""
    key = (str(root), owner)
    lock = _locks.setdefault(key, asyncio.Lock())
    _locks.move_to_end(key)
    if len(_locks) > _LOCKS_KEPT:
        for old in [k for k, held in _locks.items() if not held.locked()][: len(_locks) - _LOCKS_KEPT]:
            del _locks[old]
    return lock


async def purge_user_files(roots, owner: str) -> None:
    """Remove a deleted account's workspace and trash under every root. Best effort."""

    def remove(root: Path) -> None:
        for path in (root.absolute() / owner, _control_path(root, owner)):
            try:
                if path.is_symlink():
                    path.unlink()
                elif path.exists():
                    shutil.rmtree(path)
            except OSError:
                logger.exception("Could not remove files of deleted user {} at {}", owner, path)

    if not re.fullmatch(r"[0-9A-Za-z_-]+", owner or ""):
        return
    for root in roots:
        # Under the owner lock, so a running sweep cannot recreate the trash folder afterwards.
        async with _owner_lock(Path(root).absolute(), owner):
            await asyncio.to_thread(remove, Path(root))


def workspace_roots(app) -> list[Path]:
    """Every workspaces root this deployment uses: PrivateClaw, and Sbot when combined."""
    return [state.settings.workspaces_root for name in ("claw", "sbot") if (state := getattr(app.state, name, None))]


class FileLifecycle:
    def __init__(self, state, mode: str):
        self.state = state
        self.mode = mode
        self.root = state.settings.workspaces_root.absolute()
        if mode == "sbot":
            from sbot.db import models
        else:
            from claw.db import models
        self.models = models
        self._tables: frozenset[str] | None = None

    def workspace(self, owner: str) -> Path:
        path = self.root / owner
        if path.is_symlink():
            raise FileNotFoundError(owner)
        return path


    async def active_work(self, owner: str) -> bool:
        """Whether unfinished jobs, journals or missions of this owner may still use any file."""
        from claw.jobs.models import ForegroundJournal, Job

        m = self.models
        now = time.time()
        recent = now - STALE_WORK_DAYS * DAY
        recent_dt = datetime.fromtimestamp(recent, timezone.utc)
        async with self.state.sessions.factory() as db:
            if self._tables is None:
                conn = await db.connection()
                self._tables = await conn.run_sync(lambda c: frozenset(inspect(c).get_table_names()))
            tables = self._tables
            if self.mode == "privateclaw":
                pattern = "%" + owner.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                jobs = (
                    await db.scalars(
                        select(m.AppSetting.value).where(
                            m.AppSetting.key.like("artifact_job:%"),
                            m.AppSetting.updated_at >= recent_dt,
                            cast(m.AppSetting.value, Text).like(pattern, escape="\\"),
                        )
                    )
                ).all()
                if any((j or {}).get("user_id") == owner and j.get("status") in ACTIVE_ARTIFACT_JOBS for j in jobs):
                    return True
            if "agent_jobs" in tables and await db.scalar(
                select(Job.id)
                .where(
                    Job.owner_id == owner,
                    Job.mode == self.mode,
                    Job.status.not_in(TERMINAL_JOBS),
                    Job.updated_at >= recent,
                )
                .limit(1)
            ):
                return True
            if "agent_foreground_journals" in tables and await db.scalar(
                select(ForegroundJournal.id)
                .where(
                    ForegroundJournal.owner_id == owner,
                    ForegroundJournal.mode == self.mode,
                    ForegroundJournal.status == "open",
                    ForegroundJournal.updated_at >= recent,
                )
                .limit(1)
            ):
                return True
            if self.mode == "sbot" and await db.scalar(
                select(m.Mission.id)
                .where(
                    m.Mission.owner_id == owner,
                    m.Mission.status.not_in(FINISHED_MISSIONS),
                    m.Mission.updated_at >= recent_dt,
                )
                .limit(1)
            ):
                return True
        return False

    async def pending_deliveries(self, owner: str) -> set[str]:
        """Resolved paths a pending local delivery still has to read."""
        broker = getattr(getattr(self.state, "runtime", None), "local_workspaces", None)
        if broker is None:
            return set()
        return await asyncio.to_thread(broker.pending_delivery_sources, owner)


    def list_files(self, owner: str, policy, *, limit: int = 1000) -> dict:
        ws = self.workspace(owner)
        files = []
        for directory, dirs, names in os.walk(ws, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()]
            for name in names:
                if name.startswith("."):
                    continue
                path = Path(directory) / name
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                rel = path.relative_to(ws).as_posix()
                zone = zone_of(self.mode, rel)
                days = retention_days(policy, zone)
                files.append({
                    "path": rel,
                    "size": info.st_size,
                    "modified_at": info.st_mtime,
                    "zone": str(zone),
                    "expires_at": info.st_mtime + days * DAY if days else None,
                })
                if len(files) >= limit:
                    return {"files": files, "truncated": True}
        return {"files": files, "truncated": False}

    @staticmethod
    def _entries(control: Path) -> list[str]:
        """Entry ids, oldest first."""
        try:
            return sorted(e.name for e in os.scandir(control) if _ID.match(e.name) and e.is_dir(follow_symlinks=False))
        except OSError:
            return []

    def _trashed(self, control: Path, ident: str, owner: str) -> dict | None:
        entry = _read_json(control / ident / "record.json")
        if (
            entry is None
            or entry.get("id") != ident
            or entry.get("mode") != self.mode
            or entry.get("owner") != owner
            or entry.get("status") not in {"prepared", "trashed"}
            or not stat.S_ISREG(_data_mode(control / ident) or 0)
        ):
            return None
        return entry

    def list_trash(self, owner: str, *, retention_days: int, limit: int = 200) -> dict:
        control = control_root(self.root, owner)
        ids = self._entries(control)
        files = []
        for ident in reversed(ids):
            entry = self._trashed(control, ident, owner)
            if entry is None:
                continue
            files.append({
                "id": ident,
                "path": entry["path"],
                "at": entry["at"],
                "actor": entry.get("actor"),
                "rule": entry.get("rule"),
                "size": entry.get("size", 0),
                "expires_at": entry["at"] + retention_days * DAY,
            })
            if len(files) >= limit:
                break
        return {"files": files, "total": len(ids)}

    def archived(self, owner: str, paths) -> dict[str, str]:
        """Trash ids of the given paths that are gone from the workspace but sit in trash."""
        try:
            markers = [e.name for e in os.scandir(_control_path(self.root, owner) / "index")]
        except OSError:
            return {}
        newest: dict[str, str] = {}
        for name in markers:
            key, _, ident = name.partition("-")
            if _ID.match(ident) and ident > newest.get(key, ""):
                newest[key] = ident
        ws = self.workspace(owner)
        found = {}
        for path in dict.fromkeys(paths):
            ident = newest.get(_path_key(path))
            if ident is None:
                continue
            try:
                _parts(path)
            except FileNotFoundError:
                continue
            try:
                os.lstat(ws / path)
            except FileNotFoundError:
                found[path] = ident
            except (OSError, ValueError):
                continue
        return found


    def _move_to_trash(self, owner: str, relative: str, *, actor: str, rule: str, now: float, cutoff=None) -> dict:
        parts = _parts(relative)
        control = control_root(self.root, owner)
        with _dir_fd(self.workspace(owner), parts[:-1]) as parent:
            name = parts[-1]
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                raise FileNotFoundError(relative) from exc
            if not stat.S_ISREG(info.st_mode):
                raise FileNotFoundError(relative)
            # The sweeper only takes files untouched since the cutoff, so it never
            # shares a file with a running turn. Recheck right before the move.
            if cutoff is not None and info.st_mtime >= cutoff:
                raise FileConflict("File was modified after the retention cutoff.")
            ident = _new_id(now)
            entry = {
                "id": ident, "path": "/".join(parts), "owner": owner, "actor": actor, "rule": rule,
                "at": now, "size": info.st_size, "status": "prepared", "mode": self.mode,
            }
            entry_dir = control / ident
            entry_dir.mkdir(mode=0o700)
            with _dir_fd(entry_dir, ()) as trash:
                try:
                    _write_json(entry_dir / "record.json", entry)
                    os.rename(name, "data", src_dir_fd=parent, dst_dir_fd=trash)
                except OSError:
                    shutil.rmtree(entry_dir, ignore_errors=True)
                    raise
                # From here the entry holds the user's file, so a failure leaves it for reconcile.
                os.fsync(parent)
                os.fsync(trash)
                moved = os.stat("data", dir_fd=trash, follow_symlinks=False).st_mode
                if not stat.S_ISREG(moved):
                    if not stat.S_ISDIR(moved):
                        os.unlink("data", dir_fd=trash)
                    else:
                        # A rename back never replaces a non-empty directory; on failure
                        # the entry stays for reconcile, which leaves non-files alone.
                        os.rename("data", name, src_dir_fd=trash, dst_dir_fd=parent)
                    shutil.rmtree(entry_dir, ignore_errors=True)
                    raise FileConflict("The path changed while it was being moved.")
        entry["status"] = "trashed"
        _write_json(entry_dir / "record.json", entry, durable=False)
        (control / "index").mkdir(mode=0o700, exist_ok=True)
        (control / "index" / _marker(entry)).touch()
        return entry

    def _record(self, owner: str, ident: str) -> tuple[dict, Path]:
        if not _ID.match(ident or ""):
            raise FileNotFoundError(ident)
        control = control_root(self.root, owner)
        entry = self._trashed(control, ident, owner)
        if entry is None:
            raise FileNotFoundError(ident)
        return entry, control

    def _restore(self, owner: str, ident: str, now: float) -> dict:
        entry, control = self._record(owner, ident)
        parts = _parts(entry["path"])
        self.workspace(owner).mkdir(parents=True, exist_ok=True)
        with _dir_fd(control / ident, ()) as trash:
            # A restored file starts a fresh retention period.
            os.utime("data", (now, now), dir_fd=trash, follow_symlinks=False)
            with _dir_fd(self.workspace(owner), parts[:-1], create=True) as parent:
                try:
                    os.link("data", parts[-1], src_dir_fd=trash, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError:
                    raise FileConflict("A file already exists at the original path.") from None
                os.fsync(parent)
        entry.update(status="restored", restored_at=now, restored_by=owner)
        _write_json(control / ident / "record.json", entry, durable=False)
        _drop_entry(control, ident, entry["path"])
        return entry

    @staticmethod
    def _request_purge(control: Path, entry: dict, *, by: str, now: float) -> None:
        entry.update(status="purge_requested", purged_by=by, purged_at=now)
        _write_json(control / entry["id"] / "record.json", entry)
        (control / entry["id"] / "data").unlink(missing_ok=True)
        entry["status"] = "purged"


    async def trash(self, owner: str, relative: str, *, actor: str) -> dict:
        parts = _parts(relative)
        async with _owner_lock(self.root, owner):
            if await self.active_work(owner):
                raise FileConflict("A chat, job or mission is still running. Try again when it finishes.")
            if str(self.workspace(owner).resolve().joinpath(*parts)) in await self.pending_deliveries(owner):
                raise FileConflict("A pending delivery to your computer still needs this file.")
            await self.state.audit.log(
                "file_trash_requested", {"path": relative, "actor": actor, "mode": self.mode}, user_id=owner
            )
            entry = await asyncio.to_thread(
                self._move_to_trash, owner, relative, actor=actor, rule="user", now=time.time()
            )
            await self.state.audit.log("file_trashed", entry, user_id=owner)
            return entry

    async def restore(self, owner: str, ident: str) -> dict:
        async with _owner_lock(self.root, owner):
            entry, _ = await asyncio.to_thread(self._record, owner, ident)
            await self.state.audit.log("file_restore_requested", entry, user_id=owner)
            entry = await asyncio.to_thread(self._restore, owner, ident, time.time())
            await self.state.audit.log("file_restored", entry, user_id=owner)
            return entry

    async def purge(self, owner: str, ident: str, *, confirmed: bool = False, expected_path: str | None = None) -> dict:
        async with _owner_lock(self.root, owner):
            entry, control = await asyncio.to_thread(self._record, owner, ident)
            if confirmed is not True or expected_path != entry["path"]:
                raise FileConflict("Permanent deletion requires explicit confirmation of this file path.")
            await self.state.audit.log("file_purge_requested", entry, user_id=owner)
            await asyncio.to_thread(self._request_purge, control, entry, by=owner, now=time.time())
            await self.state.audit.log("file_purged", entry, user_id=owner)
            await asyncio.to_thread(_drop_entry, control, ident, entry["path"])
            return entry


    def _expired(self, ws: Path, policy, now: float, skip_ai: bool) -> list[tuple[str, int, float, float, Zone]]:
        """(path, size, mtime, cutoff, zone) of every expired file, oldest first, in one pruned walk."""
        cutoffs = {}
        for zone in RETENTION_FIELD:
            days = retention_days(policy, zone)
            if days > 0 and not (skip_ai and zone is Zone.AI):
                cutoffs[zone] = now - days * DAY
        found = []
        stack = [("", ws)]
        while cutoffs and stack:
            prefix, directory = stack.pop()
            try:
                items = list(os.scandir(directory))
            except OSError:
                continue
            for item in items:
                rel = prefix + item.name
                try:
                    if item.is_dir(follow_symlinks=False):
                        # Below the top level a folder's zone is its top folder's, except in uploads/.
                        if prefix or item.name == "uploads" or zone_of(self.mode, rel + "/x") in cutoffs:
                            stack.append((rel + "/", item.path))
                        continue
                    if not item.is_file(follow_symlinks=False):
                        continue
                    zone = zone_of(self.mode, rel)
                    if zone not in cutoffs:
                        continue
                    info = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                if info.st_mtime < cutoffs[zone]:
                    found.append((rel, info.st_size, info.st_mtime, cutoffs[zone], zone))
        found.sort(key=lambda f: f[2])
        return found

    def _sweep_files(self, owner: str, policy, now: float, skip_ai: bool, pending: set[str]) -> SweepResult:
        result = SweepResult()
        ws = self.workspace(owner)
        if not ws.is_dir():
            return result
        resolved = ws.resolve()
        for rel, size, _mtime, cutoff, zone in self._expired(ws, policy, now, skip_ai):
            if result.trashed >= MOVES_PER_PASS:
                break
            if str(resolved / rel) in pending:
                continue
            if policy.enforce:
                rule = f"{zone}:{retention_days(policy, zone)}d"
                try:
                    size = self._move_to_trash(owner, rel, actor="lifecycle", rule=rule, now=now, cutoff=cutoff)["size"]
                except (FileConflict, OSError) as exc:
                    logger.debug("Lifecycle skipped {}/{}: {}", owner, rel, exc)
                    continue
            result.trashed += 1
            result.bytes += size
            if len(result.paths) < SAMPLE_SIZE:
                result.paths.append(rel)
        return result

    def _purge_trash(self, owner: str, policy, now: float) -> list[dict]:
        """Entries past the trash TTL, then the oldest beyond the cap. Purges them when enforcing."""
        control = control_root(self.root, owner)
        cutoff = now - policy.trash_retention_days * DAY
        cap = policy.trash_cap_mb * 1024 * 1024
        doomed, live = [], []
        for ident in self._entries(control):
            if _id_time(ident) < cutoff:
                doomed.append(ident)
            elif cap:
                with suppress(OSError):
                    live.append((ident, os.lstat(control / ident / "data").st_size))
        total = sum(size for _, size in live)
        for ident, size in live:
            if total <= cap:
                break
            doomed.append(ident)
            total -= size
        purged = []
        for ident in doomed:
            entry = _read_json(control / ident / "record.json")
            if entry is None or entry.get("id") != ident or entry.get("mode") != self.mode:
                continue
            if policy.enforce:
                self._request_purge(control, entry, by="lifecycle", now=now)
            purged.append(entry)
        return purged

    async def sweep(self, owner: str, policy, *, now: float) -> SweepResult:
        """One pass over one owner. With `policy.enforce` off it only counts."""
        skip_ai = await self.active_work(owner)
        if skip_ai:
            logger.debug("Lifecycle skips the AI zone of {} this pass: active work", owner)
        pending = await self.pending_deliveries(owner)
        async with _owner_lock(self.root, owner):

            def run() -> tuple[SweepResult, list[dict]]:
                return self._sweep_files(owner, policy, now, skip_ai, pending), self._purge_trash(owner, policy, now)

            result, purged = await asyncio.to_thread(run)
            result.purged = len(purged)
            if policy.enforce and (result.trashed or purged):
                await self.state.audit.log(
                    "workspace_lifecycle",
                    {"trashed": result.trashed, "purged": len(purged), "bytes": result.bytes,
                     "paths": result.paths, "mode": self.mode},
                    user_id=owner,
                )
                control = control_root(self.root, owner)
                await asyncio.to_thread(lambda: [_drop_entry(control, e["id"], e.get("path")) for e in purged])
        return result


    def _converge(self, control: Path, ident: str, now: float) -> dict | None:
        """Bring one entry to a resting state. Returns it when it stays in trash."""
        entry_dir = control / ident
        entry = _read_json(entry_dir / "record.json")
        data = _data_mode(entry_dir)
        if data is not None and not stat.S_ISREG(data):
            logger.warning("Trash entry {} holds a non-file; left for an operator", entry_dir)
            return None
        if entry is None:
            if data is None:
                shutil.rmtree(entry_dir, ignore_errors=True)
            return None
        if entry.get("mode") != self.mode or entry.get("id") != ident:
            return None
        if entry.get("status") not in {"prepared", "trashed"} or data is None:
            (entry_dir / "data").unlink(missing_ok=True)
            _drop_entry(control, ident, entry.get("path"))
            return None
        if entry["status"] == "prepared":
            entry["status"] = "trashed"
            _write_json(entry_dir / "record.json", entry)
        if not now - 3650 * DAY < _id_time(ident) <= now + DAY:
            # Ids from before time-ordered ids existed sort at random. Re-key them by trash time.
            fresh = _new_id(float(entry.get("at") or now))
            os.rename(entry_dir, control / fresh)
            (control / "index" / _marker(entry)).unlink(missing_ok=True)
            entry["id"] = fresh
            _write_json(control / fresh / "record.json", entry)
        return entry

    def _reconcile(self, now: float) -> int:
        base = self.root / "_file_lifecycle"
        if base.is_symlink() or not base.is_dir():
            return 0
        seen = 0
        for owner_dir in os.scandir(base):
            if not owner_dir.is_dir(follow_symlinks=False):
                continue
            control = Path(owner_dir.path)
            markers = set()
            for ident in self._entries(control):
                if seen >= RECONCILE_MAX_ENTRIES:
                    return seen
                seen += 1
                try:
                    entry = self._converge(control, ident, now)
                except OSError:
                    logger.exception("Could not reconcile trash entry {}", control / ident)
                    continue
                if entry is not None:
                    markers.add(_marker(entry))
            index = control / "index"
            if index.is_symlink():
                continue
            existing = {e.name for e in os.scandir(index)} if index.is_dir() else set()
            for name in existing - markers:
                if not (control / name.partition("-")[2]).is_dir():
                    (index / name).unlink(missing_ok=True)
            if markers - existing:
                index.mkdir(mode=0o700, exist_ok=True)
                for name in markers - existing:
                    (index / name).touch()
        return seen

    async def reconcile(self, *, now: float | None = None) -> int:
        """Converge every journal entry after a crash. Running it again changes nothing."""
        try:
            return await asyncio.to_thread(self._reconcile, time.time() if now is None else now)
        except Exception:
            logger.exception("File lifecycle reconcile failed under {}", self.root)
            return 0
