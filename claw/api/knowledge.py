"""Knowledge base API: create bases, upload documents, list/search.

A knowledge base is an OKF bundle. Uploads are parsed + chunked automatically —
the user never deals with formats. Private bases are visible only to their
owner; public ones to everyone. Only the owner may modify or delete a base.
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from claw.api.deps import AppState, current_user, get_state
from claw.db.models import User

router = APIRouter(prefix="/api/knowledge")

_MAX_DOC_BYTES = 25_000_000  # 25 MB per document
_MAX_DOCS_PER_UPLOAD = 10


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


@router.post("/{kb_id}/documents")
async def upload_documents(
    kb_id: str,
    files: list[UploadFile] = File(...),
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """Upload one or more documents; each is parsed, chunked, and indexed."""
    await _owned_base(state, user, kb_id)
    if len(files) > _MAX_DOCS_PER_UPLOAD:
        raise HTTPException(status_code=413, detail=f"at most {_MAX_DOCS_PER_UPLOAD} files per upload")
    ingested, errors = [], []
    for upload in files:
        data = await upload.read()
        if len(data) > _MAX_DOC_BYTES:
            errors.append(f"{upload.filename}: exceeds the 25 MB limit")
            continue
        try:
            result = await state.knowledge_service.ingest(
                kb_id, upload.filename or "document", upload.content_type or "", data
            )
            ingested.append(result)
        except ValueError as exc:
            errors.append(str(exc))
    if not ingested and errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return {"ingested": ingested, "errors": errors}


@router.delete("/{kb_id}/documents/{doc_id}")
async def delete_document(
    kb_id: str, doc_id: str, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    await _owned_base(state, user, kb_id)
    await state.knowledge_service.delete_doc(doc_id)
    return {"deleted": True}
