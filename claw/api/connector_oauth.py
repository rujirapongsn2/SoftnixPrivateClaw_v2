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
from claw.db.stores import ConnectorKindMismatch

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
    current = next((c for c in existing if c.name == preset.name), None)
    is_new = current is None
    if current is not None and current.kind != "mcp":
        # The user already has a custom kind="api" connector under this
        # preset's name. Installing over it would overwrite its url/env with
        # the preset's while leaving kind="api" and its now-meaningless
        # `operations` in place — a half-MCP/half-REST row whose tools would
        # fire the stale operations at the preset's host using this OAuth
        # token. Refuse instead; the user can rename their own connector.
        logger.warning(
            "Connector OAuth install for {} blocked: user already has a kind={} connector named {}",
            preset.key,
            current.kind,
            preset.name,
        )
        return bounce("name_conflict", preset.key)

    fields: dict[str, Any] = dict(
        # Explicit (rather than relying on the column default) so this row can
        # never drift into an api-kind shape — the store's kind check treats an
        # omitted kind as "leave untouched".
        kind="mcp",
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

    try:
        await app_state.connectors.upsert(payload["u"], preset.name, **fields)
    except ConnectorKindMismatch:
        # The check above is a read, so it can go stale: the user can create a
        # kind="api" connector under this name between the two. The store's own
        # in-transaction check is what actually holds, and letting it escape
        # here would abandon the user on a raw 500 page mid-OAuth instead of
        # bouncing them back to the app with the same conflict the read path
        # already renders.
        logger.warning("Connector OAuth install for {} lost a kind race on {}", preset.key, preset.name)
        return bounce("name_conflict", preset.key)
    await app_state.connectors_mgr.invalidate(payload["u"])
    return bounce("connected", preset.key)
