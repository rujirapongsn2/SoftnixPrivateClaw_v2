"""HTTP surface of OAuth for per-user remote MCP connectors.

Three routers. `build_user_router` starts and ends a user's authorization for
one connector; claw mounts it at the root and sbot at /modes/sbot, each with
its own auth dependency and its own `ReturnTo`. `callback_router` is the one
fixed redirect URI every authorization server sends the browser back to, plus
the Client ID Metadata Document those servers fetch; both are unauthenticated.
`admin_router` manages the admin-registered client per issuer.

None of these paths can be captured by the preset flow's
/api/connectors/oauth/{provider}/callback.
"""

import hashlib
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from loguru import logger
from pydantic import BaseModel, Field

from claw.api.deps import AppState, get_state, require_admin
from claw.auth.mcp_oauth import McpOAuthStartError
from claw.auth.mcp_oauth_discovery import (
    CALLBACK_PATH,
    CLIENT_METADATA_PATH,
    PENDING_TTL,
    CallbackOutcome,
    ReturnTo,
)
from claw.db.models import User
from claw.db.stores import McpAuthStore
from claw.security.crypto import UndecryptableSecret

NONCE_COOKIE_PREFIX = "claw_mcp_oauth_nonce_"
# Where each app's frontend lives under web_base_url.
_WEB_ROOTS = {ReturnTo.CLAW: "/", ReturnTo.SBOT: "/chat/sbot"}


def nonce_cookie(state: str) -> str:
    """One cookie per flow, so starting a second Connect (another connector,
    another tab) cannot overwrite the nonce a pending callback needs."""
    return NONCE_COOKIE_PREFIX + hashlib.sha256(state.encode()).hexdigest()[:16]


def build_user_router(
    get_state: Callable[..., Any], current_user: Callable[..., Any], return_to: ReturnTo
) -> APIRouter:
    router = APIRouter(prefix="/api/connectors")

    @router.post("/{connector_id}/authorization")
    async def start(
        connector_id: str,
        response: Response,
        user: User = Depends(current_user),
        state: Any = Depends(get_state),
    ) -> dict:
        try:
            started = await state.mcp_oauth.begin(user.id, connector_id, return_to=return_to)
        except LookupError:
            raise HTTPException(status_code=404, detail="connector not found") from None
        except McpOAuthStartError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from None
        response.set_cookie(
            nonce_cookie(started.state),
            started.browser_nonce,
            max_age=int(PENDING_TTL.total_seconds()),
            path=CALLBACK_PATH,
            httponly=True,
            secure=state.settings.public_base_url.lower().startswith("https://"),
            # Lax, not Strict: the callback is a top-level navigation from the
            # authorization server's site, which Strict would strip the cookie from.
            samesite="lax",
        )
        return {"authorize_url": started.authorize_url}

    @router.delete("/{connector_id}/authorization")
    async def disconnect(
        connector_id: str,
        revoke: bool = False,
        user: User = Depends(current_user),
        state: Any = Depends(get_state),
    ) -> dict:
        try:
            revoked = await state.mcp_oauth.disconnect(user.id, connector_id, revoke=revoke)
        except LookupError:
            raise HTTPException(status_code=404, detail="connector not found") from None
        # Close the live session now rather than at the next sync: it would
        # otherwise keep serving tools until then on the grant just dropped.
        await state.connectors_mgr.disconnect_user(user.id)
        return {"disconnected": True, "revoked": revoked}

    return router


callback_router = APIRouter()


@callback_router.get(CALLBACK_PATH)
async def callback(request: Request, state: AppState = Depends(get_state)) -> RedirectResponse:
    """Never answers with an error page: every outcome, including an
    unexpected exception, is a redirect back into the app with a status the
    frontend can render."""
    params = request.query_params
    cookie = nonce_cookie(params.get("state", ""))
    try:
        completed = await state.mcp_oauth.complete(
            state=params.get("state", ""),
            code=params.get("code"),
            error=params.get("error"),
            browser_nonce=request.cookies.get(cookie),
        )
        outcome, connector_id, return_to = completed.outcome, completed.connector_id, completed.return_to
        if completed.owner_id is not None and outcome is CallbackOutcome.CONNECTED:
            await state.connectors_mgr.invalidate(completed.owner_id)
    except Exception:
        logger.exception("MCP OAuth callback failed")
        outcome, connector_id, return_to = CallbackOutcome.ERROR, None, ReturnTo.CLAW
    query = {"connector_status": outcome.value}
    if connector_id is not None:
        query = {"connector_id": connector_id, **query}
    web = state.settings.web_base_url.rstrip("/")
    response = RedirectResponse(f"{web}{_WEB_ROOTS[return_to]}?{urlencode(query)}", status_code=303)
    response.delete_cookie(cookie, path=CALLBACK_PATH)
    return response


@callback_router.get(CLIENT_METADATA_PATH)
async def client_metadata(state: AppState = Depends(get_state)) -> dict:
    """Fetched by authorization servers that accept a URL as client_id; it
    carries nothing secret."""
    return state.mcp_oauth.client_metadata_document()


admin_router = APIRouter(prefix="/api/admin/mcp-oauth-clients")


class McpOAuthClientBody(BaseModel):
    issuer: str = Field(min_length=1, max_length=500)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str = Field(default="", max_length=2000)  # empty keeps the stored secret
    token_endpoint_auth_method: Literal["client_secret_basic", "client_secret_post", "none"] = (
        "client_secret_basic"
    )


def _client_row(store: McpAuthStore, row: Any) -> dict:
    return {
        "id": row.id,
        "issuer": row.issuer,
        "client_id": row.client_id,
        "token_endpoint_auth_method": row.token_endpoint_auth_method,
        # A secret that no longer decrypts is as good as none.
        "has_client_secret": row.client_secret_enc is not None and store.secret_readable(row),
        "redirect_uri": row.redirect_uri,
    }


async def _clients(state: AppState) -> dict:
    store = state.mcp_oauth.store
    rows = await store.list_admin_clients()
    return {"redirect_uri": state.mcp_oauth.redirect_uri, "clients": [_client_row(store, r) for r in rows]}


@admin_router.get("")
async def list_clients(admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    return await _clients(state)


@admin_router.put("")
async def put_client(
    body: McpOAuthClientBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    """Create or update the client for `issuer` (one per issuer)."""
    issuer = body.issuer.strip().rstrip("/")
    parts = urlsplit(issuer)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise HTTPException(status_code=422, detail="issuer must be an https URL without query or fragment")
    store = state.mcp_oauth.store
    redirect_uri = state.mcp_oauth.redirect_uri
    secret = body.client_secret.strip()
    if body.token_endpoint_auth_method != "none" and not secret:
        existing = await store.admin_client_id(issuer, redirect_uri)
        try:
            stored = await store.client_credentials(existing) if existing is not None else None
        except UndecryptableSecret:
            stored = None
        if stored is None or stored.client_secret is None:
            raise HTTPException(status_code=422, detail="client_secret is required for this auth method")
    await store.upsert_admin_client(
        issuer=issuer,
        client_id=body.client_id.strip(),
        client_secret=secret or None,
        auth_method=body.token_endpoint_auth_method,
        redirect_uri=redirect_uri,
    )
    return await _clients(state)


@admin_router.delete("/{client_row_id}")
async def delete_client(
    client_row_id: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    if not await state.mcp_oauth.store.delete_admin_client(client_row_id):
        raise HTTPException(status_code=404, detail="client not found")
    return await _clients(state)
