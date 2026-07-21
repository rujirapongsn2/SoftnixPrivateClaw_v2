"""One-click connector OAuth endpoints — start the flow and handle the callback.

The end-user clicks "Connect" → `start` returns the provider authorize URL (built
from the admin-registered OAuth app + the connector's scopes). After consent the
provider redirects to `callback`, which exchanges the code and creates the
connector for the user, then bounces back to the web app.
"""

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from loguru import logger

from claw.api.deps import AppState, current_user, get_state
from claw.auth import connector_oauth as flow
from claw.core.connector_presets import get_preset
from claw.db.models import User

router = APIRouter(prefix="/api/connectors/oauth")


@router.get("/{preset_key}/start")
async def start(
    preset_key: str, user: User = Depends(current_user), app_state: AppState = Depends(get_state)
) -> dict:
    preset = get_preset(preset_key)
    if preset is None or preset.setup != "oauth":
        raise HTTPException(status_code=404, detail="unknown OAuth connector")
    app = await app_state.oauth_apps.get(preset.oauth_provider)
    if not app.get("client_id") or not app.get("client_secret"):
        # UI turns this into "ask your administrator to enable {provider} sign-in".
        raise HTTPException(status_code=400, detail=f"{preset.oauth_provider}_not_configured")
    token = flow.make_state(user.id, preset.key, preset.oauth_provider, app_state.settings.secret_key)
    return {"url": flow.authorize_url(preset, app, app_state.settings, token)}


@router.get("/{provider}/callback")
async def callback(
    provider: str,
    code: str = "",
    state: str = "",
    app_state: AppState = Depends(get_state),
) -> RedirectResponse:
    """Handle the provider redirect: verify state, exchange the code, create the
    connector for the user, then bounce back to the web app with a status flag."""
    web = app_state.settings.web_base_url.rstrip("/")

    def bounce(status: str, key: str = "") -> RedirectResponse:
        q = f"connector={key}&connector_status={status}" if key else f"connector_status={status}"
        return RedirectResponse(f"{web}/?{q}", status_code=307)

    payload = flow.read_state(state, app_state.settings.secret_key)
    if not code or payload is None or payload.get("p") != provider:
        return bounce("error")

    preset = get_preset(payload["k"])
    if preset is None or preset.setup != "oauth":
        return bounce("error")
    app = await app_state.oauth_apps.get(provider)
    if not app.get("client_id"):
        return bounce("error", preset.key)

    redirect = flow.redirect_uri(app_state.settings, provider)
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            tokens = await flow.exchange_code(preset, app, code, redirect, http)
    except httpx.HTTPError as exc:
        logger.warning("Connector OAuth exchange failed for {}: {}", preset.key, exc)
        return bounce("error", preset.key)

    env = flow.tokens_to_env(preset, app, tokens)
    if not env.get(f"{preset.env_prefix}_TOKEN"):
        return bounce("error", preset.key)

    existing = await app_state.connectors.list_for_user(payload["u"])
    is_new = not any(c.name == preset.name for c in existing)

    fields: dict[str, Any] = dict(
        transport=preset.transport,
        command=preset.command,
        url=preset.url,
        env=env,
        enabled=True,
    )
    if is_new:
        # Only seed the preset's description on first install — re-running
        # this flow (e.g. a token refresh) must not clobber a description the
        # user has since edited themselves.
        fields["description"] = preset.description

    await app_state.connectors.upsert(payload["u"], preset.name, **fields)
    await app_state.connectors_mgr.invalidate(payload["u"])
    return bounce("connected", preset.key)
