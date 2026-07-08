"""Knowledge base API: create bases, upload documents, list/search.

A knowledge base is an OKF bundle. Uploads are parsed + chunked automatically —
the user never deals with formats. Private bases are visible only to their
owner; public ones to everyone. Only the owner may modify or delete a base.
"""

import os
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from claw.api.deps import AppState, current_user, get_state
from claw.db.models import User

router = APIRouter(prefix="/api/knowledge")

_UPLOAD_CHUNK = 1024 * 1024  # 1 MB stream chunks


class CreateKBBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    visibility: str = "private"  # private | public


class UpdateKBBody(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = None
    visibility: str | None = None


def _doc_row(d) -> dict:
    return {
        "id": d.id,
        "title": d.title,
        "filename": d.filename,
        "mime": d.mime,
        "size": d.size,
        "chars": d.chars,
        "chunks": d.chunks,
        "status": getattr(d, "status", "ready"),
        "error": getattr(d, "error", ""),
        "created_at": d.created_at.isoformat(),
    }


async def _owned_base(state: AppState, user: User, kb_id: str):
    kb = await state.knowledge.get_base(kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb.owner_id != user.id:
        raise HTTPException(status_code=403, detail="only the owner can modify this knowledge base")
    return kb


async def _readable_base(state: AppState, user: User, kb_id: str):
    kb = await state.knowledge.get_base(kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if kb.owner_id != user.id and kb.visibility != "public":
        raise HTTPException(status_code=403, detail="you don't have access to this knowledge base")
    return kb


@router.get("")
async def list_bases(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> list:
    return await state.knowledge.list_accessible(user.id)


@router.post("")
async def create_base(
    body: CreateKBBody, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    kb = await state.knowledge.create_base(
        owner_id=user.id, name=body.name, description=body.description, visibility=body.visibility
    )
    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "visibility": kb.visibility,
        "is_owner": True,
        "docs": 0,
    }


@router.patch("/{kb_id}")
async def update_base(
    kb_id: str, body: UpdateKBBody, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    await _owned_base(state, user, kb_id)
    kb = await state.knowledge.update_base(
        kb_id, name=body.name, description=body.description, visibility=body.visibility
    )
    return {"id": kb.id, "name": kb.name, "description": kb.description, "visibility": kb.visibility}


@router.delete("/{kb_id}")
async def delete_base(
    kb_id: str, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    await _owned_base(state, user, kb_id)
    await state.knowledge.delete_base(kb_id)
    return {"deleted": True}


@router.get("/{kb_id}/documents")
async def list_documents(
    kb_id: str, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> list:
    await _readable_base(state, user, kb_id)
    docs = await state.knowledge.list_docs(kb_id)
    return [_doc_row(d) for d in docs]


async def _stage_upload(upload: UploadFile, staging_dir, max_bytes: int) -> tuple[str | None, str]:
    """Stream an upload to a temp file in the staging dir (never fully into
    memory), enforcing the size cap. Returns (temp_path, error)."""
    fd, tmp_path = tempfile.mkstemp(dir=str(staging_dir), suffix=".part")
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None, f"exceeds the {max_bytes // 1_000_000} MB limit"
                out.write(chunk)
        if total == 0:
            return None, "empty file"
        return tmp_path, ""
    finally:
        # On any early return/error, drop the partial temp file.
        if total == 0 or total > max_bytes:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@router.post("/{kb_id}/documents")
async def upload_documents(
    kb_id: str,
    files: list[UploadFile] = File(...),
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
):
    """Accept one or more documents, stage them to disk, and queue them for
    background parsing. Returns 202 with each doc in `pending` status — poll the
    documents list to watch them turn `ready` (or `failed`)."""
    await _owned_base(state, user, kb_id)
    svc = state.knowledge_service
    kcfg = state.settings.knowledge
    if len(files) > kcfg.max_docs_per_upload:
        raise HTTPException(
            status_code=413, detail=f"at most {kcfg.max_docs_per_upload} files per upload"
        )
    queued, errors = [], []
    for upload in files:
        name = upload.filename or "document"
        tmp_path, err = await _stage_upload(upload, svc.staging_dir, svc.max_doc_bytes)
        if err or tmp_path is None:
            errors.append(f"{name}: {err}")
            continue
        size = os.path.getsize(tmp_path)
        result = await svc.enqueue_upload(kb_id, name, upload.content_type or "", tmp_path, size)
        queued.append(result)
    if not queued and errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    # `ingested` is kept in the payload for backward compatibility with the
    # existing client; these are queued (pending), not yet fully ingested.
    return JSONResponse(status_code=202, content={"ingested": queued, "errors": errors})


@router.delete("/{kb_id}/documents/{doc_id}")
async def delete_document(
    kb_id: str, doc_id: str, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    await _owned_base(state, user, kb_id)
    await state.knowledge_service.delete_doc(doc_id)
    return {"deleted": True}
