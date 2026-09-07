"""Reusable source-document blueprints.

Every use materializes a new workspace copy. Stored versions are immutable and
are never exposed to the agent's writable workspace.
"""

from __future__ import annotations

import mimetypes
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sbot.api.deps import AppState, current_user, get_state
from sbot.db.models import Blueprint, BlueprintVersion, User
from sbot.filenames import safe_filename

router = APIRouter(prefix="/api/blueprints")

Visibility = Literal["private", "group", "public"]
_SUPPORTED_SUFFIXES = {".docx", ".xlsx", ".pptx"}
_MAX_BYTES = 50 * 1024 * 1024
_CHUNK = 1024 * 1024


class UpdateBlueprintBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    visibility: Visibility | None = None


class MaterializeBody(BaseModel):
    version: int | None = Field(default=None, ge=1)


class ArtifactBlueprintBody(BaseModel):
    session_id: str
    path: str
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    visibility: Visibility = "private"


def _safe_name(name: str) -> str:
    return safe_filename(name, fallback='blueprint')


def _check_supported(filename: str) -> None:
    if Path(filename).suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise HTTPException(status_code=415, detail="Blueprints support DOCX, XLSX, and PPTX files")


def _stored_file(state: AppState, version: BlueprintVersion) -> Path:
    root = state.settings.blueprints_root.resolve()
    try:
        path = (root / version.storage_path).resolve()
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="blueprint file not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="blueprint file not found")
    return path


async def _owned(state: AppState, user: User, blueprint_id: str) -> Blueprint:
    row = await state.blueprints.get(blueprint_id)
    if row is None:
        raise HTTPException(status_code=404, detail="blueprint not found")
    if row.owner_id != user.id:
        raise HTTPException(status_code=403, detail="only the owner can modify this blueprint")
    return row


async def _readable(state: AppState, user: User, blueprint_id: str) -> Blueprint:
    row = await state.blueprints.get(blueprint_id)
    if row is None:
        raise HTTPException(status_code=404, detail="blueprint not found")
    if row.owner_id == user.id or row.visibility == "public":
        return row
    if row.visibility == "group" and user.group_id is not None:
        owner = await state.users.get(row.owner_id)
        if owner is not None and owner.group_id == user.group_id:
            return row
    raise HTTPException(status_code=403, detail="you don't have access to this blueprint")


def _version_row(row: BlueprintVersion, current_version: int) -> dict:
    return {
        "id": row.id,
        "version": row.version,
        "filename": row.filename,
        "mime": row.mime,
        "size": row.size,
        "is_current": row.version == current_version,
        "created_at": row.created_at.isoformat(),
    }


async def _stage_upload(upload: UploadFile) -> tuple[Path, str, str, int]:
    filename = _safe_name(upload.filename or "blueprint")
    _check_supported(filename)
    suffix = Path(filename).suffix.lower()
    fd, raw_path = tempfile.mkstemp(prefix="sbot-blueprint-", suffix=suffix)
    path = Path(raw_path)
    size = 0
    try:
        with open(fd, "wb", closefd=True) as out:
            while chunk := await upload.read(_CHUNK):
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise HTTPException(status_code=413, detail="blueprint exceeds the 50 MB limit")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="blueprint file is empty")
        mime = upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return path, filename, mime, size
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def _create_from_path(
    state: AppState,
    user: User,
    source: Path,
    *,
    filename: str,
    mime: str,
    size: int,
    name: str,
    description: str,
    visibility: Visibility,
) -> dict:
    blueprint_id = uuid.uuid4().hex
    storage_rel = f"{blueprint_id}/v1/{filename}"
    destination = state.settings.blueprints_root / storage_rel
    destination.parent.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(source, destination)
        await state.blueprints.create(
            blueprint_id=blueprint_id,
            owner_id=user.id,
            name=name,
            description=description,
            visibility=visibility,
            filename=filename,
            mime=mime,
            size=size,
            storage_path=storage_rel,
        )
    except Exception:
        shutil.rmtree(destination.parents[1], ignore_errors=True)
        raise
    rows = await state.blueprints.list_accessible(user.id)
    return next(row for row in rows if row["id"] == blueprint_id)


@router.get("")
async def list_blueprints(
    user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> list[dict]:
    return await state.blueprints.list_accessible(user.id)


@router.post("")
async def create_blueprint(
    file: UploadFile = File(...),
    name: str = Form(..., min_length=1, max_length=120),
    description: str = Form(""),
    visibility: Visibility = Form("private"),
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    staged, filename, mime, size = await _stage_upload(file)
    try:
        return await _create_from_path(
            state, user, staged, filename=filename, mime=mime, size=size,
            name=name, description=description, visibility=visibility,
        )
    finally:
        staged.unlink(missing_ok=True)


@router.post("/from-artifact")
async def create_from_artifact(
    body: ArtifactBlueprintBody,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    session = await state.sessions.get(body.session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="session not found")
    workspace = (state.settings.workspaces_root / user.id).resolve()
    try:
        source = (workspace / body.path).resolve()
        source.relative_to(workspace)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc
    if not source.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    filename = _safe_name(source.name)
    _check_supported(filename)
    size = source.stat().st_size
    if size > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="blueprint exceeds the 50 MB limit")
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return await _create_from_path(
        state, user, source, filename=filename, mime=mime, size=size,
        name=body.name, description=body.description, visibility=body.visibility,
    )


@router.patch("/{blueprint_id}")
async def update_blueprint(
    blueprint_id: str,
    body: UpdateBlueprintBody,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    await _owned(state, user, blueprint_id)
    await state.blueprints.update(blueprint_id, **body.model_dump(exclude_none=True))
    rows = await state.blueprints.list_accessible(user.id)
    return next(row for row in rows if row["id"] == blueprint_id)


@router.delete("/{blueprint_id}")
async def delete_blueprint(
    blueprint_id: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    await _owned(state, user, blueprint_id)
    await state.blueprints.delete(blueprint_id)
    shutil.rmtree(state.settings.blueprints_root / blueprint_id, ignore_errors=True)
    return {"deleted": True}


@router.get("/{blueprint_id}/versions")
async def list_versions(
    blueprint_id: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> list[dict]:
    row = await _readable(state, user, blueprint_id)
    versions = await state.blueprints.list_versions(blueprint_id)
    return [_version_row(version, row.current_version) for version in versions]


@router.post("/{blueprint_id}/versions")
async def add_version(
    blueprint_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    await _owned(state, user, blueprint_id)
    staged, filename, mime, size = await _stage_upload(file)
    item: BlueprintVersion | None = None
    try:
        item = await state.blueprints.add_version(
            blueprint_id=blueprint_id,
            created_by=user.id,
            filename=filename,
            mime=mime,
            size=size,
            storage_path_for_version=lambda version: f"{blueprint_id}/v{version}/{filename}",
        )
        if item is None:
            raise HTTPException(status_code=404, detail="blueprint not found")
        destination = state.settings.blueprints_root / item.storage_path
        destination.parent.mkdir(parents=True, exist_ok=False)
        shutil.copy2(staged, destination)
        return _version_row(item, item.version)
    except Exception:
        if item is not None:
            await state.blueprints.discard_version(blueprint_id, item.version)
            shutil.rmtree(state.settings.blueprints_root / blueprint_id / f"v{item.version}", ignore_errors=True)
        raise
    finally:
        staged.unlink(missing_ok=True)


@router.post("/{blueprint_id}/versions/{version}/activate")
async def activate_version(
    blueprint_id: str,
    version: int,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    await _owned(state, user, blueprint_id)
    item = await state.blueprints.get_version(blueprint_id, version)
    if item is None:
        raise HTTPException(status_code=404, detail="blueprint version not found")
    _stored_file(state, item)
    await state.blueprints.activate_version(blueprint_id, version)
    return {"activated": True, "current_version": version}


@router.get("/{blueprint_id}/versions/{version}/file")
async def download_version(
    blueprint_id: str,
    version: int,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
):
    await _readable(state, user, blueprint_id)
    item = await state.blueprints.get_version(blueprint_id, version)
    if item is None:
        raise HTTPException(status_code=404, detail="blueprint version not found")
    return FileResponse(_stored_file(state, item), filename=item.filename, media_type=item.mime)


@router.post("/{blueprint_id}/materialize")
async def materialize_blueprint(
    blueprint_id: str,
    body: MaterializeBody,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    row = await _readable(state, user, blueprint_id)
    item = await state.blueprints.get_version(blueprint_id, body.version)
    if item is None:
        raise HTTPException(status_code=404, detail="blueprint version not found")
    source = _stored_file(state, item)
    workspace = state.settings.workspaces_root / user.id
    copies = workspace / "blueprints"
    copies.mkdir(parents=True, exist_ok=True)
    filename = f"{Path(item.filename).stem}-{uuid.uuid4().hex[:8]}{Path(item.filename).suffix}"
    destination = copies / filename
    shutil.copy2(source, destination)
    return {
        "name": item.filename,
        "path": f"blueprints/{filename}",
        "mime": item.mime,
        "size": item.size,
        "is_image": False,
        "blueprint": {"id": row.id, "name": row.name, "version": item.version},
    }
