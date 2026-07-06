"""REST + WebSocket API."""

import asyncio
import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from claw.api.deps import AppState, current_user, current_user_ws, get_state
from claw.db.models import User

router = APIRouter()

_MAX_ATTACHMENT_BYTES = 20_000_000  # 20 MB per file
_MAX_ATTACHMENTS = 8


def _user_workspace(state: AppState, user_id: str) -> Path:
    return (state.settings.workspaces_root / user_id).resolve()


def _safe_name(name: str) -> str:
    base = Path(name or "file").name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"


def _resolve_attachment(workspace: Path, rel: str) -> str | None:
    """Resolve a workspace-relative attachment path, rejecting escapes."""
    try:
        resolved = (workspace / rel).resolve()
        resolved.relative_to(workspace)
    except (ValueError, OSError):
        return None
    return str(resolved) if resolved.is_file() else None


class CreateSessionRequest(BaseModel):
    title: str = "New chat"


class SendMessageRequest(BaseModel):
    content: str


@router.get("/api/health")
async def health() -> dict:
    """Liveness — the process is up and serving."""
    return {"status": "ok"}


@router.get("/api/ready")
async def ready(state: AppState = Depends(get_state)) -> dict:
    """Readiness — verifies the database is reachable."""
    from sqlalchemy import text

    try:
        async with state.users.factory() as db:
            await db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return {"status": "ready"}


@router.get("/api/me")
async def me(user: User = Depends(current_user)) -> dict:
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "role": user.role}


@router.get("/api/features")
async def features(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    """Optional capabilities the UI conditionally shows (e.g. the composer mic)."""
    return {"speech_to_text": bool(state.settings.speech_api_key)}


@router.get("/api/sessions")
async def list_sessions(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> list:
    sessions = await state.sessions.list_for_user(user.id)
    running = state.runtime.active_sessions()
    return [
        {
            "id": s.id,
            "title": s.title,
            "channel": s.channel,
            "model": s.model,
            "running": s.id in running,
            "updated_at": s.updated_at.isoformat(),
        }
        for s in sessions
    ]


@router.post("/api/sessions")
async def create_session(
    body: CreateSessionRequest,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    session = await state.sessions.create(user.id, title=body.title)
    return {"id": session.id, "title": session.title}


class RenameSessionRequest(BaseModel):
    title: str


@router.patch("/api/sessions/{session_id}")
async def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    await _owned_session(state, user, session_id)
    await state.sessions.rename(session_id, body.title.strip() or "New chat")
    return {"id": session_id, "title": body.title.strip() or "New chat"}


@router.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    await _owned_session(state, user, session_id)
    await state.sessions.delete(session_id)
    return {"deleted": True}


async def _owned_session(state: AppState, user: User, session_id: str):
    session = await state.sessions.get(session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@router.get("/api/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> list:
    await _owned_session(state, user, session_id)
    messages = await state.messages.recent(session_id, limit=500)
    return [m for m in messages if m["role"] in ("user", "assistant") and m.get("content")]


@router.get("/api/sessions/{session_id}/files/{path:path}")
async def get_workspace_file(
    session_id: str,
    path: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> FileResponse:
    """Serve a file the agent created in the user's workspace (e.g. a report the
    agent wrote). Owner-scoped + path-escape-safe. Auth accepts a ?token= query
    param (current_user), so a plain new-tab link works. No filename → inline, so
    html/pdf/images render in the browser instead of force-downloading."""
    await _owned_session(state, user, session_id)
    workspace = _user_workspace(state, user.id)
    resolved = _resolve_attachment(workspace, path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(resolved)


@router.post("/api/sessions/{session_id}/attachments")
async def upload_attachments(
    session_id: str,
    files: list[UploadFile] = File(...),
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> list:
    """Save uploaded files into the user's workspace; return workspace-relative refs."""
    await _owned_session(state, user, session_id)
    if len(files) > _MAX_ATTACHMENTS:
        raise HTTPException(status_code=413, detail=f"at most {_MAX_ATTACHMENTS} files per message")

    workspace = _user_workspace(state, user.id)
    uploads = workspace / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    import mimetypes

    result = []
    for upload in files:
        data = await upload.read()
        if len(data) > _MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail=f"{upload.filename} exceeds the size limit")
        name = f"{uuid.uuid4().hex[:8]}-{_safe_name(upload.filename or 'file')}"
        path = uploads / name
        path.write_bytes(data)
        rel = f"uploads/{name}"
        mime = upload.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        result.append(
            {
                "name": upload.filename or name,
                "path": rel,
                "mime": mime,
                "size": len(data),
                "is_image": mime.startswith("image/"),
            }
        )
    return result


_MAX_AUDIO_BYTES = 25_000_000  # 25 MB — Groq's per-request audio limit


@router.post("/api/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """Speech-to-text for the composer mic: forward recorded audio to Groq's
    OpenAI-compatible Whisper endpoint and return the transcript text."""
    import httpx

    key = state.settings.speech_api_key
    if not key:
        raise HTTPException(status_code=503, detail="speech-to-text is not configured")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio upload")
    if len(data) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio exceeds the 25 MB limit")

    base = state.settings.speech_api_base.rstrip("/")
    filename = file.filename or "audio.webm"
    content_type = file.content_type or "audio/webm"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{base}/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                data={"model": state.settings.speech_model, "response_format": "json"},
                files={"file": (filename, data, content_type)},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"speech provider unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"speech provider error: {resp.text[:300]}")
    text = (resp.json().get("text") or "").strip()
    return {"text": text}


@router.websocket("/ws/chat/{session_id}")
async def chat_ws(websocket: WebSocket, session_id: str) -> None:
    """Bidirectional chat: client sends {content}, server streams AgentEvents as JSON."""
    user = await current_user_ws(websocket)
    state: AppState = websocket.app.state.claw
    session = await state.sessions.get(session_id)
    if session is None or session.user_id != user.id:
        await websocket.close(code=4404)
        return
    await websocket.accept()

    async def forward_events() -> None:
        async with state.bus.subscribe(session_id) as queue:
            while True:
                event = await queue.get()
                await websocket.send_text(json.dumps(event.to_dict(), ensure_ascii=False))

    forwarder = asyncio.create_task(forward_events())
    # Re-render any confirmations still awaiting an answer (e.g. this is a
    # reconnect while a turn is paused on an Ask-mode gate).
    for pending in state.runtime.pending_confirmations(session_id):
        await websocket.send_text(json.dumps(pending.to_dict(), ensure_ascii=False))
    turns: set[asyncio.Task] = set()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # Ask-mode: the client answering a pending tool confirmation.
            if payload.get("type") == "tool_decision":
                request_id = str(payload.get("request_id") or "")
                if request_id:
                    state.runtime.resolve_confirmation(request_id, bool(payload.get("approved")))
                continue
            content = str(payload.get("content") or "").strip()
            raw_attachments = payload.get("attachments") or []
            model = str(payload.get("model") or "").strip() or None
            permission_mode = "ask" if str(payload.get("permission_mode") or "") == "ask" else "auto"
            workspace = _user_workspace(state, user.id)
            media = [
                p for p in (_resolve_attachment(workspace, str(a)) for a in raw_attachments[:_MAX_ATTACHMENTS]) if p
            ]
            if not content and not media:
                continue
            turn = asyncio.create_task(
                state.runtime.handle_message(
                    user_id=user.id,
                    session_id=session_id,
                    content=content,
                    channel="web",
                    locale=user.locale,
                    media=media,
                    model=model,
                    permission_mode=permission_mode,
                )
            )
            turns.add(turn)
            turn.add_done_callback(turns.discard)
    except WebSocketDisconnect:
        pass
    finally:
        forwarder.cancel()
        # Turns keep running to completion — reconnecting clients refetch
        # missed messages from the REST API.
