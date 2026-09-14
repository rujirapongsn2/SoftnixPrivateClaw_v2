"""Safe lifecycle helpers for legacy workspace skill directories.

Bundle resources live in the database. Ownership records for directories that
PrivateClaw created live outside each user's writable workspace, so workspace
tools cannot forge the evidence used by destructive maintenance operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

OWNERSHIP_DIR = ".privateclaw-managed-skills"
ARCHIVE_DIR = ".skill-archive"


def archive_failure_detail(exc: OSError, operation: str = "archived") -> str:
    """Return an actionable workspace error without promising rollback state."""
    action = "deleted" if operation == "deleted" else "archived"
    if isinstance(exc, PermissionError):
        return (
            f"Workspace folder could not be {action} because the service account lacks permission. "
            "Ask an administrator to repair the workspace owner or use the maintenance tool."
        )
    return (
        f"Workspace folder could not be {action}. Refresh the orphan list before retrying; "
        "the folder may have been moved to the archive."
    )


def _skill_root(workspace: Path) -> Path:
    return workspace.resolve() / "skills"


def _ownership_root(workspace: Path, user_id: str) -> Path:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return workspace.resolve().parent / OWNERSHIP_DIR / digest


def _ownership_path(workspace: Path, user_id: str, skill_id: str) -> Path:
    digest = hashlib.sha256(skill_id.encode("utf-8")).hexdigest()
    return _ownership_root(workspace, user_id) / f"{digest}.json"


def record_managed_directory(workspace: Path, user_id: str, skill_id: str, name: str) -> None:
    """Record a directory created by PrivateClaw in service-owned metadata."""
    root = _skill_root(workspace)
    candidate = root / name
    if candidate.is_symlink() or not candidate.is_dir() or candidate.resolve().parent != root:
        raise ValueError("Managed skill directory must be a direct child of workspace/skills")
    path = _ownership_path(workspace, user_id, skill_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_stat = candidate.stat()
    payload = {
        "version": 1,
        "user_id": user_id,
        "skill_id": skill_id,
        "name": name,
        "relative_path": f"skills/{name}",
        "device": directory_stat.st_dev,
        "inode": directory_stat.st_ino,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _read_ownership(workspace: Path, user_id: str, skill_id: str) -> dict | None:
    path = _ownership_path(workspace, user_id, skill_id)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _all_ownership(workspace: Path, user_id: str) -> list[dict]:
    root = _ownership_root(workspace, user_id)
    if root.is_symlink() or not root.is_dir():
        return []
    records: list[dict] = []
    for path in root.glob("*.json"):
        try:
            if not path.is_symlink():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    records.append(payload)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return records


def _valid_record(
    record: dict, user_id: str, skill_id: str, name: str, directory: Path
) -> bool:
    try:
        directory_stat = directory.stat()
    except OSError:
        return False
    return record == {
        "version": 1,
        "user_id": user_id,
        "skill_id": skill_id,
        "name": name,
        "relative_path": f"skills/{name}",
        "device": directory_stat.st_dev,
        "inode": directory_stat.st_ino,
    }


def owned_directory(workspace: Path, user_id: str, skill_id: str, name: str) -> Path | None:
    record = _read_ownership(workspace, user_id, skill_id)
    root = _skill_root(workspace)
    candidate = root / name
    if candidate.is_symlink() or not candidate.is_dir():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if resolved.parent != root or record is None:
        return None
    return resolved if _valid_record(record, user_id, skill_id, name, resolved) else None


def _archive_destination(workspace: Path, label: str) -> Path:
    archive_root = workspace.resolve() / ARCHIVE_DIR
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / label
    suffix = 1
    while destination.exists():
        destination = archive_root / f"{label}-{suffix}"
        suffix += 1
    return destination


def archive_owned_directory(workspace: Path, user_id: str, skill_id: str, name: str) -> Path | None:
    source = owned_directory(workspace, user_id, skill_id, name)
    if source is None:
        return None
    destination = _archive_destination(workspace, f"{skill_id}-{name}")
    os.replace(source, destination)
    return destination


async def delete_skill_with_workspace(
    store, workspace: Path | None, user_id: str, skill
) -> tuple[bool, bool]:
    """Archive an owned directory before DB deletion and roll it back on failure."""
    archived: Path | None = None
    original: Path | None = None
    if workspace is not None:
        original = owned_directory(workspace, user_id, skill.id, skill.name)
        if original is not None:
            archived = archive_owned_directory(workspace, user_id, skill.id, skill.name)
            try:
                _ownership_path(workspace, user_id, skill.id).unlink()
            except OSError:
                os.replace(archived, original)
                raise
    try:
        deleted = await store.delete(user_id, skill.id)
    except Exception:
        if archived is not None and original is not None:
            os.replace(archived, original)
            record_managed_directory(workspace, user_id, skill.id, skill.name)
        raise
    if not deleted:
        if archived is not None and original is not None:
            os.replace(archived, original)
            record_managed_directory(workspace, user_id, skill.id, skill.name)
        return False, False
    return True, archived is not None


def find_orphans(workspace: Path, user_id: str, registered: dict[str, str]) -> list[dict]:
    """List workspace skill folders that are not valid active managed folders."""
    root = _skill_root(workspace)
    if not root.is_dir() or root.is_symlink():
        return []
    ownership = {
        record.get("name"): record
        for record in _all_ownership(workspace, user_id)
        if record.get("user_id") == user_id and isinstance(record.get("name"), str)
    }
    results = []
    for candidate in sorted(root.iterdir(), key=lambda item: item.name):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        skill_id = registered.get(candidate.name)
        record = ownership.get(candidate.name) or {}
        owned = bool(
            isinstance(record.get("skill_id"), str)
            and _valid_record(record, user_id, record["skill_id"], candidate.name, candidate)
        )
        active = owned and skill_id == record.get("skill_id")
        if not active:
            results.append({"name": candidate.name, "managed": owned, "registered": skill_id is not None})
    return results


def archive_orphan(workspace: Path, user_id: str, name: str, registered: dict[str, str]) -> Path:
    """Archive an explicitly selected orphan without following links."""
    if name not in {item["name"] for item in find_orphans(workspace, user_id, registered)}:
        raise ValueError("Workspace skill directory is not an orphan")
    root = _skill_root(workspace)
    source = root / name
    if source.is_symlink() or source.resolve(strict=True).parent != root:
        raise ValueError("Invalid orphan directory")
    destination = _archive_destination(workspace, f"orphan-{name}")
    records = [record for record in _all_ownership(workspace, user_id) if record.get("name") == name]
    os.replace(source, destination)
    try:
        for record in records:
            if isinstance(record.get("skill_id"), str):
                _ownership_path(workspace, user_id, record["skill_id"]).unlink(missing_ok=True)
    except OSError:
        os.replace(destination, source)
        raise
    return destination


def delete_managed_orphan(workspace: Path, user_id: str, name: str, registered: dict[str, str]) -> None:
    """Permanently remove only an explicitly selected, ownership-proven orphan."""
    orphan = next(
        (item for item in find_orphans(workspace, user_id, registered) if item["name"] == name),
        None,
    )
    if orphan is None or not orphan["managed"]:
        raise ValueError("Only an ownership-verified managed orphan can be deleted")
    record = next((item for item in _all_ownership(workspace, user_id) if item.get("name") == name), None)
    if record is None or not isinstance(record.get("skill_id"), str):
        raise ValueError("Invalid managed orphan directory")
    source = owned_directory(workspace, user_id, record["skill_id"], name)
    if source is None:
        raise ValueError("Invalid managed orphan directory")
    staged = _archive_destination(workspace, f"delete-{record['skill_id']}-{name}")
    os.replace(source, staged)
    try:
        _ownership_path(workspace, user_id, record["skill_id"]).unlink()
    except OSError:
        os.replace(staged, source)
        raise
    shutil.rmtree(staged)
