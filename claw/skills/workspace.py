"""Safe lifecycle helpers for legacy workspace skill directories.

Bundle resources live in the database. Ownership records for directories that
PrivateClaw created live outside each user's writable workspace, so workspace
tools cannot forge the evidence used by destructive maintenance operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path

OWNERSHIP_DIR = ".privateclaw-managed-skills"
ARCHIVE_DIR = ".skill-archive"
MANAGED_GENERATION_FILE = ".privateclaw-managed-generation"
ARCHIVE_GENERATION_FILE = ".privateclaw-archive-generation"


def archive_failure_detail(exc: OSError, operation: str = "archived") -> str:
    """Return an actionable workspace error without promising rollback state."""
    action = operation if operation in {"archived", "deleted", "restored"} else "updated"
    if isinstance(exc, PermissionError):
        return (
            f"Workspace folder could not be {action} because the service account lacks permission. "
            "Ask an administrator to repair the workspace owner or use the maintenance tool."
        )
    if action == "archived":
        return (
            "Workspace folder could not be archived. Refresh the orphan list before retrying; "
            "the folder may have been moved to the archive."
        )
    return f"Workspace folder could not be {action}. Refresh the archive list before retrying."


def _skill_root(workspace: Path) -> Path:
    return workspace.resolve() / "skills"


def _ownership_root(workspace: Path, user_id: str) -> Path:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return workspace.resolve().parent / OWNERSHIP_DIR / digest


def _ownership_path(workspace: Path, user_id: str, skill_id: str) -> Path:
    digest = hashlib.sha256(skill_id.encode("utf-8")).hexdigest()
    return _ownership_root(workspace, user_id) / f"{digest}.json"


def _archive_record_path(workspace: Path, user_id: str, archive_id: str) -> Path:
    return _ownership_root(workspace, user_id) / "archives" / f"{archive_id}.json"


def _delete_staging_path(workspace: Path, user_id: str, archive_id: str) -> Path:
    return _ownership_root(workspace, user_id) / "delete-staging" / archive_id


def _directory_identity(directory: Path) -> dict[str, int]:
    directory_stat = directory.stat()
    return {
        "device": directory_stat.st_dev,
        "inode": directory_stat.st_ino,
    }


def _generation(directory: Path, marker_name: str) -> str | None:
    marker = directory / marker_name
    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        value = marker.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value if len(value) == 64 and all(char in "0123456789abcdef" for char in value) else None


def _valid_archive_id(value: str) -> bool:
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def _valid_skill_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 64
        and all(char.isalnum() or char in "_- " for char in value)
    )


def _write_record(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _read_archive_record(workspace: Path, user_id: str, archive_id: str) -> dict:
    if not _valid_archive_id(archive_id):
        raise ValueError("Invalid skill archive ID")
    path = _archive_record_path(workspace, user_id, archive_id)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Skill archive not found")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid skill archive record") from exc
    if (
        not isinstance(record, dict)
        or record.get("version") != 1
        or record.get("user_id") != user_id
        or record.get("archive_id") != archive_id
    ):
        raise ValueError("Invalid skill archive record")
    return record


def _validated_archive_directory(workspace: Path, user_id: str, record: dict) -> Path:
    archive_id = record.get("archive_id")
    name = record.get("name")
    relative_path = record.get("relative_path")
    if (
        not isinstance(archive_id, str)
        or not _valid_skill_name(name)
        or not isinstance(relative_path, str)
    ):
        raise ValueError("Invalid skill archive record")
    archive_root = workspace.resolve() / ARCHIVE_DIR
    candidate = workspace.resolve() / relative_path
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("Skill archive not found")
    try:
        resolved = candidate.resolve(strict=True)
        identity = _directory_identity(resolved)
        generation = _generation(resolved, ARCHIVE_GENERATION_FILE)
    except OSError as exc:
        raise ValueError("Skill archive not found") from exc
    if generation is None:
        raise ValueError("Invalid skill archive record")
    expected = {
        "version": 1,
        "status": "archived",
        "user_id": user_id,
        "archive_id": archive_id,
        "name": name,
        "relative_path": relative_path,
        "generation": generation,
        **identity,
    }
    if resolved.parent != archive_root or record != expected:
        raise ValueError("Invalid skill archive record")
    return resolved


def record_managed_directory(workspace: Path, user_id: str, skill_id: str, name: str) -> None:
    """Record a directory created by PrivateClaw in service-owned metadata."""
    root = _skill_root(workspace)
    candidate = root / name
    if candidate.is_symlink() or not candidate.is_dir() or candidate.resolve().parent != root:
        raise ValueError("Managed skill directory must be a direct child of workspace/skills")
    path = _ownership_path(workspace, user_id, skill_id)
    marker = candidate / MANAGED_GENERATION_FILE
    generation = _generation(candidate, MANAGED_GENERATION_FILE)
    marker_created = generation is None
    if marker.exists() or marker.is_symlink():
        if generation is None:
            raise ValueError("Managed skill directory contains an invalid ownership marker")
    else:
        generation = secrets.token_hex(32)
        with marker.open("x", encoding="ascii") as stream:
            stream.write(generation)
    payload = {
        "version": 2,
        "user_id": user_id,
        "skill_id": skill_id,
        "name": name,
        "relative_path": f"skills/{name}",
        "generation": generation,
        **_directory_identity(candidate),
    }
    try:
        _write_record(path, payload)
    except OSError:
        if marker_created:
            marker.unlink(missing_ok=True)
        raise


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
        identity = _directory_identity(directory)
        generation = _generation(directory, MANAGED_GENERATION_FILE)
    except OSError:
        return False
    if generation is None:
        return False
    return record == {
        "version": 2,
        "user_id": user_id,
        "skill_id": skill_id,
        "name": name,
        "relative_path": f"skills/{name}",
        "generation": generation,
        **identity,
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
            item = {"name": candidate.name, "managed": owned, "registered": skill_id is not None}
            if record.get("version") == 1:
                item["legacy"] = True
            results.append(item)
    return results


def archive_orphan(workspace: Path, user_id: str, name: str, registered: dict[str, str]) -> str:
    """Move an orphan to a recoverable service-tracked archive."""
    if name not in {item["name"] for item in find_orphans(workspace, user_id, registered)}:
        raise ValueError("Workspace skill directory is not an orphan")
    root = _skill_root(workspace)
    source = root / name
    if source.is_symlink() or source.resolve(strict=True).parent != root:
        raise ValueError("Invalid orphan directory")
    archive_id = secrets.token_hex(16)
    destination = _archive_destination(workspace, f"orphan-{archive_id}-{name}")
    records = [record for record in _all_ownership(workspace, user_id) if record.get("name") == name]
    os.replace(source, destination)
    marker_created = False
    try:
        archive_generation = secrets.token_hex(32)
        archive_marker = destination / ARCHIVE_GENERATION_FILE
        with archive_marker.open("x", encoding="ascii") as stream:
            stream.write(archive_generation)
        marker_created = True
        for record in records:
            if isinstance(record.get("skill_id"), str):
                _ownership_path(workspace, user_id, record["skill_id"]).unlink(missing_ok=True)
        _write_record(
            _archive_record_path(workspace, user_id, archive_id),
            {
                "version": 1,
                "status": "archived",
                "user_id": user_id,
                "archive_id": archive_id,
                "name": name,
                "relative_path": f"{ARCHIVE_DIR}/{destination.name}",
                "generation": archive_generation,
                **_directory_identity(destination),
            },
        )
    except OSError:
        if marker_created:
            (destination / ARCHIVE_GENERATION_FILE).unlink(missing_ok=True)
        os.replace(destination, source)
        for record in records:
            skill_id = record.get("skill_id")
            if isinstance(skill_id, str):
                _write_record(_ownership_path(workspace, user_id, skill_id), record)
        raise
    return archive_id


def list_archived_orphans(workspace: Path, user_id: str) -> list[dict]:
    """List service-created archives without exposing host paths."""
    root = _ownership_root(workspace, user_id) / "archives"
    if root.is_symlink() or not root.is_dir():
        return []
    archives: list[dict] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.name):
        archive_id = path.stem
        try:
            record = _read_archive_record(workspace, user_id, archive_id)
            status = record.get("status")
            if status == "archived":
                _validated_archive_directory(workspace, user_id, record)
                recoverable = True
            elif status == "deleting":
                staging = _delete_staging_path(workspace, user_id, archive_id)
                if staging.is_symlink():
                    raise ValueError("Invalid skill deletion staging path")
                recoverable = False
            else:
                raise ValueError("Invalid skill archive status")
            archives.append(
                {
                    "archive_id": archive_id,
                    "name": str(record.get("name") or archive_id),
                    "status": status,
                    "recoverable": recoverable,
                }
            )
        except (OSError, ValueError):
            continue
    return archives


def restore_archived_orphan(workspace: Path, user_id: str, archive_id: str) -> None:
    """Restore a recoverable archive to workspace/skills."""
    record = _read_archive_record(workspace, user_id, archive_id)
    if record.get("status") != "archived":
        raise ValueError("Skill archive is already being deleted")
    source = _validated_archive_directory(workspace, user_id, record)
    name = record.get("name")
    if not _valid_skill_name(name):
        raise ValueError("Invalid skill archive record")
    destination = _skill_root(workspace) / name
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Workspace directory skills/{name} already exists")
    generation = _generation(source, ARCHIVE_GENERATION_FILE)
    marker = source / ARCHIVE_GENERATION_FILE
    marker.unlink()
    moved = False
    try:
        os.replace(source, destination)
        moved = True
        _archive_record_path(workspace, user_id, archive_id).unlink()
    except OSError:
        if moved:
            os.replace(destination, source)
        marker.write_text(generation or "", encoding="ascii")
        raise


def delete_managed_orphan(workspace: Path, user_id: str, name: str, registered: dict[str, str]) -> None:
    """Reject direct workspace deletion; callers must archive the folder first."""
    orphan = next(
        (item for item in find_orphans(workspace, user_id, registered) if item["name"] == name),
        None,
    )
    if orphan is None or not orphan["managed"]:
        raise ValueError("Only an ownership-verified managed orphan can be deleted")
    raise ValueError("Archive the managed folder before permanent deletion")


def delete_archived_orphan(workspace: Path, user_id: str, archive_id: str) -> None:
    """Idempotently delete a service archive via service-owned staging."""
    record = _read_archive_record(workspace, user_id, archive_id)
    record_path = _archive_record_path(workspace, user_id, archive_id)
    staging = _delete_staging_path(workspace, user_id, archive_id)
    if staging.is_symlink():
        raise ValueError("Invalid skill deletion staging path")
    if record.get("status") == "archived":
        source = _validated_archive_directory(workspace, user_id, record)
        staging.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            raise ValueError("Skill deletion staging path already exists")
        os.replace(source, staging)
        deleting_record = {
            "version": 1,
            "status": "deleting",
            "user_id": user_id,
            "archive_id": archive_id,
            "name": record.get("name"),
        }
        try:
            _write_record(record_path, deleting_record)
        except OSError:
            os.replace(staging, source)
            raise
        record = deleting_record
    elif record.get("status") != "deleting":
        raise ValueError("Invalid skill archive status")
    expected = {
        "version": 1,
        "status": "deleting",
        "user_id": user_id,
        "archive_id": archive_id,
        "name": record.get("name"),
    }
    if record != expected:
        raise ValueError("Invalid skill archive record")
    if staging.exists():
        if not staging.is_dir():
            raise ValueError("Invalid skill deletion staging path")
        shutil.rmtree(staging)
    record_path.unlink()
