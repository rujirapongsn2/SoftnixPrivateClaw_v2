"""REST + WebSocket API."""

import asyncio
import errno
import json
import mimetypes
import re
import shutil
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, Response
from loguru import logger
from pydantic import BaseModel

from sbot.api.deps import AppState, current_user, current_user_ws, get_state
from sbot.api.file_preview import PreviewError, preview_document, preview_html, preview_table, preview_fingerprint
from sbot.core.loop import visible_artifacts
from sbot.db.models import User

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


_NO_INDEX_HEADERS = {
    # Referrer-Policy matters most on share responses: the capability URL is the
    # snapshot's only credential, so it must never ride along in a Referer to a
    # third party. Note a served page can set <meta name="referrer"
    # content="unsafe-url"> to widen the browser default — that is markup, so
    # script-src 'none' does not stop it; only this header does.
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
}


def _share_no_index(resp: Response) -> None:
    """Keep shared pages out of search engines and referrer chains."""
    resp.headers.update(_NO_INDEX_HEADERS)


# Both file-serving routes below return FileResponse with no filename, so the
# browser renders html/pdf/svg/images inline rather than downloading them —
# that's the whole point (a report the agent wrote should be viewable). But an
# agent-authored .html or .svg file is untrusted content served from the app's
# own origin: without a CSP, a script in that file runs with the app's cookies,
# localStorage, and WebSocket connection, and can call back into the API
# (stored/triggered XSS). script-src/object-src 'none' neutralizes that for
# both a direct new-tab open and a future <iframe> embed — CSP is the
# document's own policy, so an iframe's `sandbox` attribute cannot loosen it.
#
# form-action/base-uri are listed explicitly because `default-src` does NOT
# act as a fallback for them (unlike script-src/object-src/etc.) — without
# these, a served HTML file could still exfiltrate via a plain <form
# action="https://evil.example"> or hijack relative links via <base>, with
# no JavaScript involved at all, silently defeating the point of this CSP.
_ACTIVE_CONTENT_CSP = (
    "default-src 'none'; script-src 'none'; object-src 'none'; frame-src 'none'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
    "frame-ancestors 'self'; form-action 'self'; base-uri 'self'"
)

# Only these media types can execute script in a same-origin document, so only
# they need the CSP. Applying it to everything broke real files: object-src
# 'none' also blocks Chromium's built-in PDF viewer, which renders a top-level
# PDF navigation through an embedded plugin document — and agent-generated PDFs
# (reportlab/weasyprint, see claw/core/builtin_skills.py) are a live feature.
_ACTIVE_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml"}
)


def _workspace_file_headers(path: str) -> dict[str, str]:
    """Response headers for an inline-served workspace/share file.

    nosniff is unconditional: it pins the browser to the Content-Type below,
    which is what makes the media-type test above trustworthy — without it a
    file could be sniffed into HTML and dodge the CSP."""
    # Mirrors Starlette FileResponse's own content-type resolution, so this
    # decision is made on exactly the type the browser will act on.
    media_type = mimetypes.guess_type(path)[0] or "text/plain"
    headers = {"X-Content-Type-Options": "nosniff"}
    if media_type in _ACTIVE_CONTENT_TYPES:
        headers["Content-Security-Policy"] = _ACTIVE_CONTENT_CSP
    return headers


def _user_workspace(state: AppState, user_id: str) -> Path:
    return (state.settings.workspaces_root / user_id).resolve()


def _safe_name(name: str) -> str:
    from sbot.filenames import safe_filename
    return safe_filename(name)


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
    bot_id: str | None = None
    group_id: str | None = None
    kind: str = "direct"


def _team_lead_onboarding(locale: str) -> str:
    if locale.lower().startswith("th"):
        return (
            "สวัสดีครับ ผม Team Lead หัวหน้าทีม AI ของคุณ\n\n"
            "ผมช่วยตอบคำถาม วางแผนงาน สรุปไฟล์ และประสานงานกับบอตผู้เชี่ยวชาญให้ได้\n\n"
            "ลองพิมพ์: “ช่วยวางแผนเปิดตัวสินค้าใหม่ให้หน่อย”\n\n"
            "คุณสามารถสร้างทีมบอตผู้เชี่ยวชาญได้สูงสุด 20 ตัวรวมผม\n"
            "ตัวอย่าง: “สร้างบอตนักวิจัยตลาด เพื่อติดตามคู่แข่ง”"
        )
    return (
        "Hello, I’m Team Lead. I help you plan work and coordinate your AI team.\n\n"
        "Try one of these:\n"
        "• Plan a product launch\n"
        "• Summarize this file and list the next actions\n"
        "• Create a research bot to track competitors\n\n"
        "Share a goal or attach a file. I can answer directly, make a plan, or create a Specialist when it helps."
    )


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
    return {
        "speech_to_text": bool(state.settings.speech_api_key),
        "text_to_speech": bool(state.settings.tts.api_key),
    }


# ---- persistent development projects ---------------------------------------

async def _project_inventory(state: AppState, user: User) -> dict:
    access = await state.project_access.resolve(user.id)
    enabled = bool(state.settings.sandbox.enabled and state.settings.sandbox.projects_enabled)
    if not enabled:
        return {
            "available": False,
            "allowed": access.allowed,
            "source": access.source,
            "max_containers": access.max_containers,
            "projects": [],
        }
    try:
        projects = await state.runtime.sandbox.projects.list(_user_workspace(state, user.id))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"project containers are unavailable: {exc}") from exc
    for project in projects:
        access_urls = []
        if project["state"] == "running":
            for container_port, bindings in (project.get("ports") or {}).items():
                port_text = str(container_port).split("/", 1)[0]
                for binding in bindings or []:
                    if not binding.get("HostPort"):
                        continue
                    host_ip = binding.get("HostIp") or state.settings.sandbox.project_host_bind_ip
                    access_urls.append({
                        "container_port": int(port_text),
                        "host_port": int(binding["HostPort"]),
                        "url": f"http://{host_ip}:{binding['HostPort']}",
                    })
        project["access_urls"] = sorted(access_urls, key=lambda item: item["container_port"])
        # Kept in the response during the UI migration for older clients.
        project["public_url"] = None
    return {
        "available": True,
        "allowed": access.allowed,
        "source": access.source,
        "max_containers": access.max_containers,
        "projects": projects,
    }


@router.get("/api/projects")
async def list_projects(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    """The caller's managed project containers; source folders are not exposed."""
    return await _project_inventory(state, user)


@router.post("/api/projects/{project}/start")
async def start_project(project: str, user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    inventory = await _project_inventory(state, user)
    if not inventory["available"]:
        raise HTTPException(status_code=409, detail="persistent project containers are disabled")
    if not inventory["allowed"]:
        raise HTTPException(status_code=403, detail="project containers are not allowed for this account")
    try:
        result = await state.runtime.sandbox.projects.execute(
            _user_workspace(state, user.id), project, "start", max_projects=inventory["max_containers"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.startswith("Error:"):
        raise HTTPException(status_code=409, detail=result[7:].strip())
    return await _project_inventory(state, user)


@router.post("/api/projects/{project}/stop")
async def stop_project(project: str, user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    inventory = await _project_inventory(state, user)
    if not inventory["available"]:
        raise HTTPException(status_code=409, detail="persistent project containers are disabled")
    try:
        result = await state.runtime.sandbox.projects.execute(
            _user_workspace(state, user.id), project, "stop"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.startswith("Error:"):
        raise HTTPException(status_code=409, detail=result[7:].strip())
    return await _project_inventory(state, user)


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
            "bot_id": s.bot_id,
            "group_id": s.group_id,
            "kind": s.kind,
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
    if body.group_id or body.kind == "group":
        from sbot.db.bot_groups import BotGroupStore
        group = await BotGroupStore(state.sessions.factory).get(body.group_id, user.id)
        if group is None:
            raise HTTPException(404, "Group not found")
        return {"id": group.session_id, "title": group.name, "group_id": group.id, "kind": "group"}
    bot = None
    if body.bot_id:
        bot = await state.bots.get(body.bot_id, user.id)
        if bot is None:
            raise HTTPException(404, "Bot not found")
    if body.kind not in {"direct", "mission"}:
        raise HTTPException(400, "Invalid session kind")
    if bot is not None and body.kind == "direct":
        onboarding = None
        if bot.kind == "chief_of_staff":
            # Onboarding is visible UI content, so it follows the profile's
            # language preference and the Control Plane default just as web
            # replies do. `locale` is only the final compatibility fallback.
            try:
                onboarding_locale = user.ui_language or (await state.branding.get())["language"]
            except Exception:
                onboarding_locale = user.ui_language or user.locale
            onboarding = _team_lead_onboarding(onboarding_locale)
        session = await state.sessions.thread_for_bot(
            user.id,
            bot.id,
            body.title,
            initial_assistant_message=onboarding,
        )
    else:
        session = await state.sessions.create(
            user.id,
            title=body.title,
            bot_id=body.bot_id,
            group_id=body.group_id,
            kind=body.kind,
        )
    return {"id": session.id, "title": session.title, "bot_id": session.bot_id, "kind": session.kind}


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


async def _owned_session(state: AppState, user: User, session_id: str, headers: dict[str, str] | None = None):
    """`headers` exists for the preview routes: their 404 has to carry the same
    nosniff their other responses do, and the header the handler sets on the
    injected Response is dropped when an HTTPException unwinds past it."""
    session = await state.sessions.get(session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="session not found", headers=headers)
    return session


@router.get("/api/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    before_seq: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """One page of a session's transcript, newest page first.

    Paginated rather than capped: a long-running session outgrows any single
    fetch, and the previous fixed ceiling dropped the oldest messages with
    nothing in the response to say so.
    """
    session = await _owned_session(state, user, session_id)
    page, has_more = await state.messages.page_for_display(
        session_id, before_seq=before_seq, limit=limit
    )
    return {
        # Keep a message whose only content is an artifact (a generated image
        # with no caption) — that one would otherwise vanish on reload.
        "messages": [m for m in page if m["content"] or (m["meta"] or {}).get("artifacts")],
        "has_more": has_more,
        # Read before that filter, so a dropped message still advances the walk.
        "next_before_seq": page[0]["seq"] if page else None,
        # The working plan is session state, not transcript, so it rides along
        # with the first page only — the client already has it when paging back.
        # Without it the Execution panel is blank after every reload, and blank
        # is indistinguishable from "no plan" even when one is still running.
        "plan": session.plan if before_seq is None else None,
    }


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
    return FileResponse(resolved, headers=_workspace_file_headers(resolved))


# Preview parsing gets its own small pool instead of asyncio's default executor.
# The default one is shared with the agent's own file tools, knowledge ingestion
# and outbound mail, and openpyxl must materialize sharedStrings.xml in full
# before the first row is readable — measured, 20 concurrent previews of a 4 MB
# workbook pushed a 2 ms read_file tool call to 72 s, for every tenant at once.
# Two workers keep preview throughput useful while leaving that pool untouched.
_PREVIEW_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="claw-preview")

# Admission control on top of the pool, because a bounded pool alone only moves
# the pile-up into its queue: one fast scroll intersects many preview cards at
# once, and each request would then sit for minutes. Beyond this depth the
# endpoint sheds load; the UI already falls back to a plain download chip.
_PREVIEW_MAX_QUEUED = 8
_PREVIEW_QUEUE_TIMEOUT = 30.0

# Per-tenant share of the pool above. Without it the global gate is first-come,
# first-served across tenants, so one user scrolling a transcript full of
# artifacts intersects enough cards to hold all 8 slots and every other tenant's
# preview sheds at 503. Deliberately well under _PREVIEW_MAX_QUEUED so a single
# user can never be the reason another one is refused, and >1 so an ordinary
# scroll still previews several cards at once.
_PREVIEW_MAX_PER_USER = 3

# Every preview error carries this too, not just the success path. FastAPI merges
# the injected Response's headers only when the handler returns; an HTTPException
# unwinds past that and Starlette builds a fresh JSONResponse, so the nosniff set
# in _bounded_preview is dropped from exactly the 400/404/503 bodies that echo a
# caller-supplied `path` or a parser's message back into JSON. Those URLs are
# navigable (`?token=` is accepted), so a sniffed error body is the same
# same-origin HTML hazard as a sniffed success body.
_PREVIEW_ERROR_HEADERS = {"X-Content-Type-Options": "nosniff"}

# The OSErrors that genuinely mean "this path is not a readable file" and so are
# the caller's 400. Everything else an open()/read() can raise is about the host,
# not the artifact, and belongs in the 5xx rate. ENOENT is absent because
# FileNotFoundError is handled on its own arm, as a 404.
_PREVIEW_UNREADABLE_ERRNOS = frozenset(
    {errno.EISDIR, errno.EACCES, errno.EPERM, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG}
)


@dataclass
class _PreviewGate:
    """Admission state, rebuilt whenever the running loop changes.

    A module-level Semaphore would be simpler, but asyncio binds one to the
    first loop that ever *blocks* on it and raises for any other — and the
    uncontended fast path never binds, so the mismatch only appears under the
    load this is here to handle. Serving is single-loop, so this rebinds at most
    once there; per-loop is what keeps it honest under test.
    """

    loop: asyncio.AbstractEventLoop
    overall: asyncio.Semaphore
    # Per-user semaphores are created on demand and dropped again as soon as a
    # user has no preview in flight, so this dict is bounded by *concurrent*
    # previewers rather than growing once per user id ever seen.
    per_user: dict[str, tuple[asyncio.Semaphore, list[int]]] = field(default_factory=dict)


_preview_gate: _PreviewGate | None = None


def _preview_admission() -> _PreviewGate:
    global _preview_gate
    loop = asyncio.get_running_loop()
    if _preview_gate is None or _preview_gate.loop is not loop:
        _preview_gate = _PreviewGate(loop, asyncio.Semaphore(_PREVIEW_MAX_QUEUED))
    return _preview_gate


@asynccontextmanager
async def _preview_slot(user_id: str) -> AsyncIterator[None]:
    """Hold one per-user slot and one global slot, or shed with 503.

    Both waits share a single deadline: acquiring them in sequence with a
    _PREVIEW_QUEUE_TIMEOUT each would let a request sit for twice as long as the
    timeout claims, which is the opposite of what shedding is for.
    """
    gate = _preview_admission()
    entry = gate.per_user.get(user_id)
    if entry is None:
        entry = (asyncio.Semaphore(_PREVIEW_MAX_PER_USER), [0])
        gate.per_user[user_id] = entry
    mine, refs = entry
    refs[0] += 1
    deadline = gate.loop.time() + _PREVIEW_QUEUE_TIMEOUT
    try:
        await _acquire_by(mine, deadline)
        try:
            await _acquire_by(gate.overall, deadline)
            try:
                yield
            finally:
                gate.overall.release()
        finally:
            mine.release()
    finally:
        refs[0] -= 1
        if refs[0] == 0:
            # Only safe because nothing above awaits between this check and the
            # pop: a newly arriving request for the same user would otherwise
            # take the entry we are about to discard and lose its slot count.
            gate.per_user.pop(user_id, None)


def _preview_busy() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="preview is busy, try again shortly",
        headers={"Retry-After": "5", **_PREVIEW_ERROR_HEADERS},
    )


async def _acquire_by(sem: asyncio.Semaphore, deadline: float) -> None:
    """Take a permit from `sem`, or shed once the shared deadline has passed.

    The free-permit case is handled before the clock is consulted at all.
    Deferring to wait_for here instead would shed while capacity sits idle:
    `wait_for` with a non-positive timeout raises without ever polling the
    awaitable, and a non-positive remainder is the normal state of the second
    acquire once the first one has used up the budget. Semaphore.acquire()
    completes without suspending when it is not locked, so this cannot block,
    and `locked()` counts existing waiters too — a free permit is never taken
    ahead of someone already queued for it.
    """
    if not sem.locked():
        await sem.acquire()
        return
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise _preview_busy()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=remaining)
    except TimeoutError:
        raise _preview_busy() from None


async def _bounded_preview(
    state: AppState,
    user: User,
    session_id: str,
    path: str,
    parse: Callable[[str], dict],
    response: Response,
) -> dict:
    """Ownership check, path resolution, load shedding and error mapping, shared
    by every preview kind — so adding a kind can't quietly skip the admission
    control that keeps one fast scroll from queueing minutes of work.

    `parse` runs on _PREVIEW_EXECUTOR even when it is only a bounded read: the
    point of that pool is that preview work of any kind never lands on the
    executor the agent's own file tools share.
    """
    # These responses embed agent-authored markup in their JSON body, and
    # current_user accepts a `?token=` query param, so the URL is navigable as a
    # top-level document. nosniff is what pins the browser to application/json
    # and stops that body being sniffed into an executable same-origin HTML
    # document — the same reason _workspace_file_headers sets it unconditionally.
    response.headers["X-Content-Type-Options"] = "nosniff"
    await _owned_session(state, user, session_id, _PREVIEW_ERROR_HEADERS)
    workspace = _user_workspace(state, user.id)
    resolved = _resolve_attachment(workspace, path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="file not found", headers=_PREVIEW_ERROR_HEADERS)
    async with _preview_slot(user.id):
        return await _run_preview(parse, resolved, path)


async def _run_preview(parse: Callable[[str], dict], resolved: str, path: str) -> dict:
    """Run one bounded parse on the preview pool and map its failures to statuses.
    Callers must already hold a slot from _preview_slot."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_PREVIEW_EXECUTOR, parse, resolved)
    except PreviewError as exc:
        # sbot.api.file_preview converts every malformed-file case it knows
        # about (corrupt zip, bad DEFLATE stream, openpyxl structural errors,
        # csv.Error) into this at the source. Anything else propagates as a
        # genuine 500 instead of being caught here and misreported as the
        # caller's problem — that used to hide real bugs in a 400 rate that
        # looks identical to users previewing junk files.
        logger.info("file preview failed for {}: {}", path, exc)
        raise HTTPException(status_code=400, detail=str(exc), headers=_PREVIEW_ERROR_HEADERS) from exc
    except FileNotFoundError:
        # _resolve_attachment only checked the file existed; the actual open()
        # happens later, after queuing for a slot and a thread-pool hop — a
        # real window for the file to be deleted/replaced concurrently.
        raise HTTPException(
            status_code=404, detail="file not found", headers=_PREVIEW_ERROR_HEADERS
        ) from None
    except OSError as exc:
        # Same race, other outcomes: the path can come back as a directory
        # (IsADirectoryError) or become unreadable (PermissionError — the sandbox
        # writes into this workspace as root over a bind-mount). Neither derives
        # from FileNotFoundError, so without this they surfaced as a 500 for what
        # is really an unreadable file. Logged at warning because, unlike a
        # malformed file, this points at the filesystem rather than the upload.
        #
        # Only those errnos. OSError also covers host-level exhaustion — ENFILE
        # and EMFILE from a leaked descriptor, ENOSPC, EIO from failing storage —
        # which is the server's fault, not this file's. Reporting those as 400
        # told the client the artifact is permanently unreadable, so the UI
        # latched its terminal chip fallback and no retry policy applied, while
        # the incident stayed invisible in the 5xx rate. Let them reach the 500.
        if exc.errno not in _PREVIEW_UNREADABLE_ERRNOS:
            raise
        logger.warning("file preview could not read {}: {}", path, exc)
        raise HTTPException(
            status_code=400, detail="file could not be read", headers=_PREVIEW_ERROR_HEADERS
        ) from exc


@router.get("/api/sessions/{session_id}/file-preview")
async def get_workspace_file_preview(
    session_id: str,
    path: str,
    response: Response,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """Bounded table preview of a CSV/TSV/XLSX artifact, so the chat can render
    a spreadsheet inline instead of only offering a download. Parsing is capped
    server-side (see sbot.api.file_preview) — the browser never receives the
    whole file, however large it is. `path` is a query param, not a path segment,
    because the sibling /files/{path:path} route would otherwise swallow it."""
    return await _bounded_preview(state, user, session_id, path, preview_table, response)


@router.post("/api/sessions/{session_id}/file-preview/png")
async def export_diagram_png(session_id: str, path: str, user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    from claw.skills.render import render_diagram
    await _owned_session(state, user, session_id)
    try:
        return await render_diagram(_user_workspace(state, user.id), path, user_id=user.id)
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="PNG renderer unavailable; download the HTML/SVG source") from exc


@router.get("/api/sessions/{session_id}/file-preview/html")
async def get_workspace_html_preview(
    session_id: str,
    path: str,
    response: Response,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """Bounded source of an .html/.htm artifact for the chat's inline render.

    The markup comes back as a JSON string rather than being served as text/html
    on purpose: served as a document it would render same-origin, so the only
    thing that renders it is the iframe the UI puts it in. Three layers keep that
    honest — nosniff above (this URL *is* navigable, `?token=` is accepted), the
    iframe's bare `sandbox` (opaque origin, no scripts, no forms), and the CSP
    sbot.api.file_preview injects into the markup itself, which is what denies it
    the network. The sibling /files route serves the real file for download,
    where _ACTIVE_CONTENT_CSP contains it instead.
    """
    return await _bounded_preview(state, user, session_id, path, preview_html, response)


@router.get("/api/sessions/{session_id}/file-preview/fingerprint")
async def get_workspace_fingerprint(
    session_id: str, path: str, response: Response,
    user: User = Depends(current_user), state: AppState = Depends(get_state),
) -> dict:
    return await _bounded_preview(state, user, session_id, path, preview_fingerprint, response)


@router.get("/api/sessions/{session_id}/file-preview/document")
async def get_workspace_document_preview(
    session_id: str,
    path: str,
    response: Response,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """Bounded text preview for DOCX, Markdown and plain-text artifacts.

    DOCX is deliberately extracted as text rather than rendered by a document
    engine, so agent-created Office files remain safe to inspect in chat.
    """
    return await _bounded_preview(state, user, session_id, path, preview_document, response)


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

    snapshot_messages: list[dict] = []
    pending: list[tuple[str, str]] = []  # (source path, name inside the share)
    for msg in incoming:
        files: list[dict] = []
        # Filter BEFORE the budget. A share is public and unauthenticated, so
        # the turn's helper scripts and base64 payloads must not be republished
        # — and copying them first would also let them eat the file budget
        # ahead of the deliverable the user actually wanted to show.
        for rel in visible_artifacts(msg.artifacts or []):
            if len(pending) >= _MAX_SHARE_FILES:
                break
            src = _resolve_attachment(workspace, rel)
            if src is None:
                continue  # missing or escapes the workspace — skip silently
            name = f"{len(pending):02d}-{_safe_name(Path(rel).name)}"
            pending.append((src, name))
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
    # Copy only once the row exists, and under ITS id: the share directory used
    # to be keyed by a locally minted uuid while the row got a different one
    # from the model default, so read_share_file looked in a directory that was
    # never written and every shared attachment 404'd. Doing it in this order
    # also means a failed insert leaves no orphaned copies behind.
    if pending:
        files_dir = _shares_root(state) / share.id / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        for src, name in pending:
            shutil.copy2(src, files_dir / name)
    base = state.settings.public_base_url.rstrip("/")
    return {
        "id": share.id,
        "token": token,
        "url": f"{base}/sbot/s/{token}",
        "path": f"/sbot/s/{token}",
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
    # _share_no_index(response) above is a no-op for this endpoint: FastAPI only
    # merges the injected Response's headers when the handler returns a
    # non-Response value, and this one returns a FileResponse. Carry them
    # explicitly or the share token leaks via Referer and the file is indexable.
    return FileResponse(resolved, headers={**_workspace_file_headers(str(resolved)), **_NO_INDEX_HEADERS})


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
    from sbot.providers.base import ProviderError

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
    text to the OpenAI-wire-compatible endpoint configured via SBOT_TTS__* env
    vars and return the generated audio. A one-shot request/response path,
    entirely separate from the agent loop — same shape as /transcribe."""
    import httpx

    tts = state.settings.tts
    if not tts.api_key:
        raise HTTPException(status_code=503, detail="text-to-speech is not configured")

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    if len(text) > tts.max_chars:
        raise HTTPException(status_code=413, detail="text is too long")

    if state.tts_rate_limiter is not None and not state.tts_rate_limiter.allow(user.id):
        raise HTTPException(status_code=429, detail="Too many read-aloud requests; please wait a moment.")

    base = tts.api_base.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=tts.timeout_seconds) as client:
            resp = await client.post(
                f"{base}/audio/speech",
                headers={"Authorization": f"Bearer {tts.api_key}"},
                json={"model": tts.model, "voice": tts.voice, "input": text},
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
    state: AppState = getattr(websocket.app.state, "sbot", None) or getattr(websocket.app.state, "claw", None)
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

    def _turn_done(task: asyncio.Task) -> None:
        turns.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # A TurnError was already published to the bus and logged with a
            # full traceback inside handle_message; retrieving it here only
            # prevents asyncio's "exception was never retrieved" warning.
            pass

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
            raw_blueprints = payload.get("blueprints") or []
            model = str(payload.get("model") or "").strip() or None
            permission_mode = "ask" if str(payload.get("permission_mode") or "") == "ask" else "auto"
            workspace = _user_workspace(state, user.id)
            media = [
                p
                for p in (_resolve_attachment(workspace, str(a)) for a in raw_attachments[:_MAX_ATTACHMENTS])
                if p
            ]
            attachment_paths = {str(path) for path in raw_attachments[:_MAX_ATTACHMENTS]}
            blueprints = []
            for item in raw_blueprints[:_MAX_ATTACHMENTS]:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "")
                name = str(item.get("name") or "").strip()[:120]
                try:
                    version = int(item.get("version") or 0)
                except (TypeError, ValueError):
                    continue
                if path in attachment_paths and path.startswith("blueprints/") and name and version > 0:
                    blueprints.append({"path": path, "name": name, "version": version})
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
                    blueprints=blueprints,
                )
            )
            turns.add(turn)
            turn.add_done_callback(_turn_done)
    except WebSocketDisconnect:
        pass
    finally:
        forwarder.cancel()
        # Turns keep running to completion — reconnecting clients refetch
        # missed messages from the REST API.
