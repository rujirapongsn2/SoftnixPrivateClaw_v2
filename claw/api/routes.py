"""REST + WebSocket API."""

import asyncio
import json
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from loguru import logger
from pydantic import BaseModel

from claw.api.deps import AppState, current_user, current_user_ws, get_state
from claw.db.models import User

router = APIRouter()

_MAX_ATTACHMENT_BYTES = 20_000_000  # 20 MB per file
_MAX_ATTACHMENTS = 8
_SHARE_TTL_DAYS = 7
_MAX_SHARE_MESSAGES = 100
_MAX_SHARE_FILES = 20


def _shares_root(state: AppState) -> Path:
    """Per-share snapshot files live here, alongside (not inside) any user's
    workspace, so the owner-scoped file endpoint can never reach them."""
    return (state.settings.workspaces_root / "_shares").resolve()


def _share_no_index(resp: Response) -> None:
    """Keep shared pages out of search engines and referrer chains."""
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"


def _user_workspace(state: AppState, user_id: str) -> Path:
    return (state.settings.workspaces_root / user_id).resolve()


def _safe_name(name: str) -> str:
    base = Path(name or "file").name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"


def _prune_generated_images(uploads: Path, keep: int) -> None:
    """Delete the oldest generated-*.* files beyond `keep` — every successful
    /images call writes a new one and nothing else ever removes them, so
    without this the directory grows without bound. Runs synchronously; call
    via asyncio.to_thread."""
    files = sorted(uploads.glob("generated-*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        stale.unlink(missing_ok=True)


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


class ShareMessage(BaseModel):
    role: str  # user|assistant
    content: str = ""
    artifacts: list[str] = []


class ShareRequest(BaseModel):
    title: str = "Shared answer"
    messages: list[ShareMessage] = []


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
    """Optional capabilities the UI conditionally shows (e.g. the composer mic,
    the per-message "read aloud" speaker)."""
    try:
        tts_available = (await state.llm_config.resolve_admin_openai_provider()) is not None
    except Exception:
        # Fail soft: a DB hiccup resolving the TTS provider must not also take
        # down the (unrelated, DB-free) speech_to_text flag for every caller.
        logger.exception("Failed to resolve TTS provider for /api/features")
        tts_available = False
    return {
        "speech_to_text": bool(state.settings.speech_api_key),
        "text_to_speech": tts_available,
    }


# ---- public branding (Control Plane > Preferences) --------------------------
# Deliberately unauthenticated: the login screen renders BEFORE any auth, so the
# logo/language/font/background must be readable with no credentials. Only
# non-sensitive appearance settings are exposed here; mutation lives behind
# require_admin in claw/api/admin.py.

_LOGO_CONTENT_TYPE = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


@router.get("/api/branding")
async def public_branding(response: Response, state: AppState = Depends(get_state)) -> dict:
    """Global appearance for every client (incl. pre-auth login). Logo fields
    become asset URLs carrying the stored filename as a `?v=` version token, so
    a replaced logo gets a new URL instead of being masked by a stale browser
    cache of the old one (see branding_asset's long-lived cache below).

    Not HTTP-cached: an admin's Save must apply everywhere on the next fetch,
    not up to a cache-lifetime later. BrandingStore.get() is in-process cached
    (invalidated on every write), so this stays a cheap in-memory read even
    though every client hits it on load — no unbounded DB cost at scale."""
    response.headers["Cache-Control"] = "no-store"
    cfg = await state.branding.get()
    logos = {
        slot: (f"/api/branding/assets/{slot}?v={cfg[f'logo_{slot}']}" if cfg.get(f"logo_{slot}") else None)
        for slot in ("login", "chat", "sidebar")
    }
    return {
        "language": cfg["language"],
        "font_size": cfg["font_size"],
        "chat_background": cfg["chat_background"],
        "logos": logos,
    }


@router.get("/api/branding/assets/{slot}")
async def branding_asset(slot: str, state: AppState = Depends(get_state)) -> FileResponse:
    """Serve an admin-uploaded logo. Public; `slot` is a fixed enum and the
    stored filename is server-generated, so there is no path-traversal surface.
    Raster-only + explicit Content-Type + nosniff (the asset is served to every
    visitor, so a mistyped/hostile file must never be sniffed as active
    content). The URL is versioned by public_branding()'s `?v=` (ignored here,
    unbound query params are simply dropped by FastAPI), so it's safe to cache
    this response aggressively — a replace always produces a new filename and
    therefore a new URL, never a stale hit."""
    if slot not in ("login", "chat", "sidebar"):
        raise HTTPException(status_code=404, detail="unknown logo slot")
    cfg = await state.branding.get()
    filename = cfg.get(f"logo_{slot}")
    if not filename:
        raise HTTPException(status_code=404, detail="no custom logo set")
    path = (state.settings.branding_root / filename).resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="logo file missing")
    ext = path.suffix.lstrip(".").lower()
    return FileResponse(
        path,
        media_type=_LOGO_CONTENT_TYPE.get(ext, "application/octet-stream"),
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=31536000, immutable"},
    )


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
    # Keep user/assistant messages that have text OR an artifact (e.g. a
    # generated image with no caption) — an artifact-only assistant message
    # would otherwise vanish on reload.
    return [
        m
        for m in messages
        if m["role"] in ("user", "assistant")
        and (m.get("content") or (m.get("meta") or {}).get("artifacts"))
    ]


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


# --- Public share links -------------------------------------------------------
# A share is an immutable, redacted snapshot of one answer (plus its question),
# reachable by anyone holding the capability URL. It never touches the live
# session or the owner-scoped file endpoint.


@router.post("/api/sessions/{session_id}/share")
async def create_share(
    session_id: str,
    payload: ShareRequest,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """Snapshot the given messages into a public, expiring share link.

    Only user/assistant text is captured. Any referenced artifact files are
    validated against the owner's workspace (path-escape-safe) and *copied* into
    a per-share directory, then exposed through the public share-file route — the
    owner's token is never embedded in the shared page."""
    await _owned_session(state, user, session_id)

    incoming = [m for m in payload.messages if m.role in ("user", "assistant") and m.content]
    if not incoming:
        raise HTTPException(status_code=400, detail="nothing to share")
    incoming = incoming[:_MAX_SHARE_MESSAGES]

    workspace = _user_workspace(state, user.id)
    share_id = uuid.uuid4().hex
    files_dir = _shares_root(state) / share_id / "files"

    snapshot_messages: list[dict] = []
    files_copied = 0
    for msg in incoming:
        files: list[dict] = []
        for rel in msg.artifacts or []:
            if files_copied >= _MAX_SHARE_FILES:
                break
            src = _resolve_attachment(workspace, rel)
            if src is None:
                continue  # missing or escapes the workspace — skip silently
            name = f"{files_copied:02d}-{_safe_name(Path(rel).name)}"
            files_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, files_dir / name)
            files_copied += 1
            files.append(
                {
                    "name": name,
                    "is_image": bool(re.search(r"\.(png|jpe?g|gif|webp|svg|bmp)$", name, re.I)),
                }
            )
        snapshot_messages.append({"role": msg.role, "content": msg.content, "files": files})

    share, token = await state.shares.create(
        user_id=user.id,
        session_id=session_id,
        title=payload.title,
        snapshot={"messages": snapshot_messages},
        ttl_days=_SHARE_TTL_DAYS,
    )
    base = state.settings.public_base_url.rstrip("/")
    return {
        "id": share.id,
        "token": token,
        "url": f"{base}/s/{token}",
        "path": f"/s/{token}",
        "expires_at": share.expires_at.isoformat() if share.expires_at else None,
    }


@router.delete("/api/shares/{share_id}")
async def revoke_share(
    share_id: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    ok = await state.shares.revoke(share_id, user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="share not found")
    return {"revoked": True}


@router.get("/api/share/{token}")
async def read_share(
    token: str,
    response: Response,
    state: AppState = Depends(get_state),
) -> dict:
    """Public, unauthenticated read of a share snapshot. No `current_user`, no
    token/email fallback — the capability URL is the only credential."""
    _share_no_index(response)
    share = await state.shares.get_active_by_token(token)
    if share is None:
        raise HTTPException(status_code=404, detail="This link has expired or is no longer available.")
    return {
        "title": share.title,
        "messages": (share.snapshot or {}).get("messages", []),
        "created_at": share.created_at.isoformat() if share.created_at else None,
    }


@router.get("/api/share/{token}/files/{name}")
async def read_share_file(
    token: str,
    name: str,
    response: Response,
    state: AppState = Depends(get_state),
) -> FileResponse:
    """Serve a file copied into a share snapshot. Public, but scoped to files
    that belong to *this* token's share and path-escape-safe."""
    _share_no_index(response)
    share = await state.shares.get_active_by_token(token, bump=False)
    if share is None:
        raise HTTPException(status_code=404, detail="link expired")
    files_dir = (_shares_root(state) / share.id / "files").resolve()
    try:
        resolved = (files_dir / name).resolve()
        resolved.relative_to(files_dir)
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="file not found")
    if not resolved.is_file():
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


# DALL·E-style /images-endpoint providers; everything else (openrouter,
# gemini, …) returns images through the chat-multimodal path instead.
_IMAGES_ENDPOINT_PREFIXES = {"openai", "azure"}


class GenerateImageRequest(BaseModel):
    prompt: str
    model: str
    size: str | None = None


@router.post("/api/sessions/{session_id}/images")
async def generate_image(
    session_id: str,
    body: GenerateImageRequest,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """Text-to-image generation — a one-shot request/response path entirely
    separate from the agent loop (no tools, no streaming, no EventBus). Writes
    the image into the user's workspace and persists it as an artifact message
    so it renders (and reloads) like any other agent-produced image."""
    from claw.providers.base import ProviderError

    await _owned_session(state, user, session_id)
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="prompt is required")
    if len(prompt) > state.settings.image.max_prompt_chars:
        raise HTTPException(status_code=413, detail="prompt is too long")

    # Per-user throttle before doing any (paid) work — mirrors the chat path's
    # turn rate limit so this endpoint can't be looped to run up provider cost.
    if state.image_rate_limiter is not None and not state.image_rate_limiter.allow(user.id):
        raise HTTPException(status_code=429, detail="Too many image requests; please wait a moment.")

    # Run the prompt through the same control policy as chat input — BEFORE
    # any model resolution/plan gate — so a policy-violating prompt is always
    # scanned and audited even when it's paired with an invalid or plan-gated
    # model id; a blocked prompt never reaches the provider, and a masked
    # prompt is what we send AND store (raw PII is not persisted), exactly
    # like the chat path.
    if state.policy is not None:
        decision = state.policy.enforce(prompt, scope="input")
        if decision.matched_rules:
            await state.audit.log(
                "policy",
                {"scope": "input", "action": decision.action, "rules": decision.matched_rules},
                user_id=user.id,
                session_id=session_id,
            )
        if decision.blocked:
            raise HTTPException(
                status_code=400, detail=decision.message or "Request blocked by the control policy."
            )
        prompt = decision.text

    # resolve_image only matches kind="image" models the caller may use, and
    # returns the provider prefix so we pick the right generation strategy.
    # max_cost enforces the plan's image cost ceiling (BYOK models exempt).
    plan = await state.plans.resolve_for_user(user.id) if state.plans is not None else None
    resolved = await state.llm_config.resolve_image(
        body.model, user.id, max_cost=plan["max_image_cost"] if plan else None
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="image model not found")

    # Usage-plan gate: the caller's plan must permit image generation at all —
    # but only for admin-global models; the caller's own BYOK model is exempt
    # (same reasoning as the cost ceiling: they pay for their own key, so the
    # plan tier that meters the operator's shared models doesn't apply). The
    # images/day quota is enforced atomically after resolve, below — a
    # reserve-then-verify to avoid a check-then-act race on this paid resource.
    if resolved["scope"] == "global" and plan is not None and not plan["allow_image"]:
        await state.audit.log(
            "quota",
            {"event": "image_disallowed", "plan": plan["name"]},
            user_id=user.id,
            session_id=session_id,
        )
        raise HTTPException(status_code=403, detail="Your plan does not include image generation.")

    mode = "images_endpoint" if resolved["model_prefix"] in _IMAGES_ENDPOINT_PREFIXES else "chat"

    # Reserve the images/day slot atomically BEFORE the paid provider call:
    # increment the counter, then verify the user is still within their cap.
    # Two concurrent requests both increment and both see the post-increment
    # total, so at most `limit` can pass — no check-then-act overshoot on a
    # paid resource. The reservation is released on any failure below (and on
    # over-quota here). Counting also happens for unlimited plans (limit 0) so
    # the usage report stays accurate. `reserved` tracks whether to release.
    # The cap itself only applies to admin-global models — the caller's own
    # BYOK model is exempt, same reasoning as the allow_image gate above.
    img_limit = plan["images_per_day"] if plan else 0
    reserved = False
    if state.usage is not None:
        await state.usage.record_image(user.id, resolved["model_id"])
        reserved = True
        if img_limit > 0 and resolved["scope"] == "global":
            used = (await state.usage.usage_today(user.id))["images"]
            if used > img_limit:
                await state.usage.release_image(user.id, resolved["model_id"])
                await state.audit.log(
                    "quota",
                    {"event": "images_per_day", "plan": plan["name"], "limit": img_limit},
                    user_id=user.id,
                    session_id=session_id,
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Daily image limit reached ({img_limit}/day). Try again tomorrow.",
                )

    async def _release() -> None:
        if reserved and state.usage is not None:
            await state.usage.release_image(user.id, resolved["model_id"])

    try:
        images = await state.runtime.provider.generate_image(
            prompt,
            resolved["model_id"],
            api_key=resolved["api_key"] or None,
            api_base=resolved["api_base"] or None,
            size=body.size or state.settings.image.default_size,
            mode=mode,
            timeout=state.settings.image.timeout_seconds,
        )
    except ProviderError as exc:
        await _release()
        raise HTTPException(status_code=502, detail=f"image generation failed: {exc}") from exc

    data, ext = images[0]
    # Guard against an oversized payload (a misbehaving provider or a
    # user-controlled BYOK api_base) before writing it to disk.
    if len(data) > state.settings.image.max_bytes:
        await _release()
        raise HTTPException(status_code=502, detail="generated image exceeds the size limit")

    workspace = _user_workspace(state, user.id)
    uploads = workspace / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    name = f"generated-{uuid.uuid4().hex[:8]}.{ext}"
    # Up to max_bytes (20MB default) — off the event loop, matching the
    # to_thread pattern used elsewhere for large writes (knowledge ingestion,
    # filesystem tool).
    await asyncio.to_thread((uploads / name).write_bytes, data)
    await asyncio.to_thread(_prune_generated_images, uploads, state.settings.image.max_stored_per_user)
    rel = f"uploads/{name}"

    # Persist as a user prompt + an artifact-only assistant message so it shows
    # in the transcript and survives reload (see list_messages' filter). Store
    # the possibly-masked prompt, never the raw input. The images/day counter
    # was already incremented by the reservation above.
    await state.messages.append(
        session_id,
        [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "", "meta": {"artifacts": [rel], "image_model": body.model}},
        ],
    )
    return {"path": rel, "prompt": prompt}


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


class SpeakRequest(BaseModel):
    text: str


@router.post("/api/tts")
async def speak(
    body: SpeakRequest,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> Response:
    """Text-to-speech for the assistant message "read aloud" button: forward
    text to the OpenAI-wire-compatible provider configured in Control Plane >
    LLM Providers and return the generated audio. A one-shot request/response
    path, entirely separate from the agent loop — same shape as /transcribe."""
    import httpx

    provider = await state.llm_config.resolve_admin_openai_provider()
    if provider is None:
        raise HTTPException(status_code=503, detail="text-to-speech is not configured")

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    if len(text) > state.settings.tts.max_chars:
        raise HTTPException(status_code=413, detail="text is too long")

    if state.tts_rate_limiter is not None and not state.tts_rate_limiter.allow(user.id):
        raise HTTPException(status_code=429, detail="Too many read-aloud requests; please wait a moment.")

    base = provider["api_base"].rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=state.settings.tts.timeout_seconds) as client:
            resp = await client.post(
                f"{base}/audio/speech",
                headers={"Authorization": f"Bearer {provider['api_key']}"},
                json={"model": state.settings.tts.model, "voice": state.settings.tts.voice, "input": text},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"speech provider unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"speech provider error: {resp.text[:300]}")
    return Response(content=resp.content, media_type="audio/mpeg")


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
            # The user's own Settings > Profile > Preferences language (if
            # saved) drives the AI's response language for web turns; else
            # the admin-set global default (Control Plane > Preferences).
            # Re-read the user row fresh each turn (cheap indexed-PK lookup,
            # dwarfed by the LLM call that follows) rather than trusting the
            # `user` object captured once at connect time, so a preference
            # saved from another tab mid-session takes effect on the very
            # next message instead of only after a reconnect. BrandingStore.
            # get() always returns a language (defaults merged in), so this
            # can only fall back to the connect-time locale if either read
            # fails outright. Only the locale VALUE changes here — the turn
            # orchestration is untouched.
            try:
                current = await state.users.get(user.id)
                turn_locale = (current.ui_language if current else user.ui_language) or (
                    await state.branding.get()
                )["language"]
            except Exception:
                turn_locale = user.ui_language or user.locale
            turn = asyncio.create_task(
                state.runtime.handle_message(
                    user_id=user.id,
                    session_id=session_id,
                    content=content,
                    channel="web",
                    locale=turn_locale,
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
