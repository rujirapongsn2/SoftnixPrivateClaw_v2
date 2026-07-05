"""Client browser-extension API.

Two audiences share this router:

- The web user (JWT-authed) mints a pairing ticket, checks status, and unpairs.
- The Chrome extension (self-authenticates with extension_id + extension_token,
  so these routes take NO JWT) polls for tasks and posts results.

The durable queue behind it is ``AppState.browser_broker`` (see
``claw/browser/broker.py``).
"""

import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from claw.api.deps import AppState, current_user, get_state
from claw.db.models import User

router = APIRouter(prefix="/api/browser-extension")

# Re-vendored extension source at the repo root; only these files ship.
_EXTENSION_DIR = Path(__file__).resolve().parents[2] / "browser-extension"
_EXTENSION_FILES = ("manifest.json", "background.js", "content_script.js", "popup.html", "popup.js")


# ---- user-facing (JWT-authed) --------------------------------------------


@router.post("/pairing/init")
async def pairing_init(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    if not state.settings.browser.client_extension_enabled:
        raise HTTPException(status_code=400, detail="client browser extension is not enabled on this server")
    item = state.browser_broker.create_pairing(user_id=user.id, label=user.email or user.id)
    return {
        "api_base": state.settings.public_base_url,
        "instance_id": user.id,
        "pairing_ticket": item["ticket"],
        "expires_at": item["expires_at"],
    }


@router.get("/status")
async def status(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    info = state.browser_broker.extension_status(user_id=user.id)
    return {"client_extension_enabled": state.settings.browser.client_extension_enabled, **info}


@router.delete("/pairing")
async def unpair(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    removed = state.browser_broker.unpair(user_id=user.id)
    return {"unpaired": removed}


@router.get("/download")
async def download(state: AppState = Depends(get_state)) -> Response:
    """Build the Chrome extension zip on the fly for unpacked install."""
    base = _EXTENSION_DIR.resolve()
    if not base.is_dir():
        raise HTTPException(status_code=404, detail="extension package not available")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in _EXTENSION_FILES:
            source = (base / name).resolve()
            try:
                source.relative_to(base)
            except ValueError as exc:
                raise HTTPException(status_code=500, detail="invalid extension file") from exc
            if not source.is_file():
                raise HTTPException(status_code=404, detail=f"missing extension file: {name}")
            archive.write(source, f"softnix-privateclaw-browser/{name}")
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="softnix-privateclaw-browser.zip"'},
    )


# ---- extension-facing (no JWT; self-authenticated) -----------------------


class CompletePairingBody(BaseModel):
    instance_id: str = ""
    pairing_ticket: str = ""
    ticket: str = ""
    label: str = ""


class PollBody(BaseModel):
    instance_id: str = ""
    extension_id: str = ""
    extension_token: str = ""
    token: str = ""


class ResultBody(BaseModel):
    instance_id: str = ""
    extension_id: str = ""
    extension_token: str = ""
    token: str = ""
    task_id: str = ""
    result: dict | None = None


@router.post("/pairing/complete")
async def pairing_complete(body: CompletePairingBody, state: AppState = Depends(get_state)) -> dict:
    ticket = (body.pairing_ticket or body.ticket).strip()
    if not ticket:
        raise HTTPException(status_code=400, detail="pairing_ticket is required")
    try:
        return state.browser_broker.complete_pairing(ticket=ticket, extension_label=body.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks/poll")
async def tasks_poll(body: PollBody, state: AppState = Depends(get_state)) -> dict:
    token = (body.extension_token or body.token).strip()
    if not body.extension_id or not token:
        raise HTTPException(status_code=400, detail="extension_id and extension_token are required")
    if not state.settings.browser.client_extension_enabled:
        return {"task": None, "enabled": False}
    try:
        extension = state.browser_broker.authenticate_extension(
            extension_id=body.extension_id, extension_token=token
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    task = state.browser_broker.poll_task(extension=extension)
    return {"task": task, "enabled": True}


@router.post("/tasks/result")
async def tasks_result(body: ResultBody, state: AppState = Depends(get_state)) -> dict:
    token = (body.extension_token or body.token).strip()
    if not body.extension_id or not token or not body.task_id:
        raise HTTPException(status_code=400, detail="extension_id, extension_token, and task_id are required")
    if not isinstance(body.result, dict):
        raise HTTPException(status_code=400, detail="result must be an object")
    try:
        extension = state.browser_broker.authenticate_extension(
            extension_id=body.extension_id, extension_token=token
        )
        stored = state.browser_broker.submit_result(
            task_id=body.task_id, extension=extension, result=body.result
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"result": stored}
