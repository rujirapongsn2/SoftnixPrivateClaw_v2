"""Scope-aware MCP connector management — one implementation for both the
admin-global Control Plane ("Pre-built Connectors") and per-user connectors.

Every handler takes an ``owner_id``: ``None`` operates on the admin-global
connectors (shared pool, see claw/core/connectors.py's _GlobalConnections), a
user id operates on that user's own private ones. The admin routes
(claw/api/admin.py) call these with ``owner_id=None``; the user routes
(claw/api/manage.py) call them with the caller's id — mirrors
claw/api/llm_shared.py's owner_id convention.
"""

from fastapi import HTTPException
from pydantic import BaseModel, Field

from claw.api.deps import AppState
from claw.core.connector_presets import is_allowed_stdio_command


class ConnectorBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_\-]+$")
    description: str = Field(default="", max_length=2000)
    transport: str = Field(default="stdio", pattern=r"^(stdio|http)$")
    command: str = ""
    url: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    # Per-connector connect/tool-call timeout override, in milliseconds. None =
    # use the instance-wide default (claw/config.py's ConnectorSettings).
    timeout_ms: int | None = Field(default=None, ge=1000, le=120000)
    enabled: bool = True


def connector_row(c, status: dict | None = None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description or "",
        "transport": c.transport,
        "command": c.command,
        "url": c.url,
        "env": c.env or {},
        "timeout_ms": c.timeout_ms,
        "enabled": c.enabled,
        "runtime": status or {"status": "not_connected"},
    }


def connector_global_summary(c, status: dict | None = None) -> dict:
    """Redacted view for regular users (GET /api/connectors/global) and skill
    authors: no command/url/env. Unlike connector_row's callers, which are
    always the connector's owner, a regular user viewing a global connector
    is never its owner, so secrets must never be included here."""
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description or "",
        "transport": c.transport,
        "enabled": c.enabled,
        "runtime": status or {"status": "not_connected"},
    }


async def list_connectors(state: AppState, owner_id: str | None) -> list:
    """Warm-connect first so runtime status is live, then list. Global scope
    warms the shared pool directly (sync_global); user scope warms through
    the runtime's per-user agent registry (warm_connectors), same as before."""
    if owner_id is None:
        await state.connectors_mgr.sync_global()
        rows = await state.connectors.list_for_global()
        statuses = await state.connectors_mgr.status_global()
    else:
        if state.runtime is not None:
            await state.runtime.warm_connectors(owner_id)
        rows = await state.connectors.list_for_user(owner_id)
        statuses = await state.connectors_mgr.status(owner_id)
    return [connector_row(c, statuses.get(c.name)) for c in rows]


async def upsert_connector(
    state: AppState,
    name: str,
    body: ConnectorBody,
    owner_id: str | None,
    *,
    allow_arbitrary_stdio: bool,
) -> dict:
    if body.transport == "stdio" and not body.command.strip():
        raise HTTPException(status_code=422, detail="stdio connector requires a command")
    if body.transport == "http" and not body.url.strip():
        raise HTTPException(status_code=422, detail="http connector requires a url")
    if body.transport == "stdio" and not allow_arbitrary_stdio:
        # A stdio connector's command is a real subprocess spawned unsandboxed
        # on the host (see ConnectorManager._connect) — never let a caller
        # without this grant supply their own; only the fixed,
        # developer-authored preset commands are permitted.
        if not is_allowed_stdio_command(body.command):
            raise HTTPException(
                status_code=403,
                detail="custom stdio (local command) connectors require an administrator",
            )
    # Full-replace PUT, like the other upsert_* routes in claw/api/manage.py
    # (e.g. upsert_skill) — every ConnectorBody field is always forwarded, so
    # a caller that omits a field gets that field's Pydantic default written
    # through (e.g. omitting "description" clears it to ""), not "leave
    # untouched". The shipped frontend always sends every field on every save.
    row = await state.connectors.upsert(
        owner_id,
        name.strip(),
        description=body.description,
        transport=body.transport,
        command=body.command,
        url=body.url,
        env=body.env,
        timeout_ms=body.timeout_ms,
        enabled=body.enabled,
    )
    if owner_id is None:
        # Scoped to this one connector by name — never disturbs any OTHER
        # global connector's already-live session (see sync_global).
        await state.connectors_mgr.invalidate_global(row.name)
    else:
        await state.connectors_mgr.invalidate(owner_id)
    return connector_row(row)


async def delete_connector(state: AppState, connector_id: str, owner_id: str | None) -> dict:
    name = await state.connectors.delete(owner_id, connector_id)
    if name is None:
        raise HTTPException(status_code=404, detail="connector not found")
    if owner_id is not None:
        await state.connectors_mgr.invalidate(owner_id)
    else:
        # Tear down this one connector's live session right away rather than
        # relying solely on sync_global's own "no longer enabled" cleanup,
        # which can be skipped on its next call if it loses the global lock
        # race (see sync_global) — that would leave the deleted connector's
        # session and any already-registered user proxies live and callable
        # for longer than expected.
        await state.connectors_mgr.close_global(name, connector_id)
    return {"deleted": True}
