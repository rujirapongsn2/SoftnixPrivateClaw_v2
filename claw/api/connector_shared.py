"""Scope-aware MCP connector management — one implementation for both the
admin-global Control Plane ("Pre-built Connectors") and per-user connectors.

Every handler takes an ``owner_id``: ``None`` operates on the admin-global
connectors (shared pool, see claw/core/connectors.py's _GlobalConnections), a
user id operates on that user's own private ones. The admin routes
(claw/api/admin.py) call these with ``owner_id=None``; the user routes
(claw/api/manage.py) call them with the caller's id — mirrors
claw/api/llm_shared.py's owner_id convention.
"""

import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from claw.api.deps import AppState
from claw.core.connector_presets import is_allowed_stdio_command
from claw.db.stores import ConnectorKindMismatch
from claw.security.ssrf import assert_public_url

_PATH_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Kept in sync with claw/tools/api.py and claw/core/connectors.py, which read
# the same env prefix off a stored row.
_HEADER_ENV_PREFIX = "HEADER_"

# Every mainstream provider caps a tool's name at 64 characters and rejects the
# whole request when one is longer — not just that tool. A connector name (64)
# plus an operation name (49) plus the "api_"/"_" glue can reach 118, so an
# over-long pair would break every turn for that user until the connector was
# found and renamed. Rejected at save time instead: see GenericApiTool.__init__
# for the name being measured here.
_MAX_TOOL_NAME = 64

# path/body parameters are substituted into templates by _PATH_PLACEHOLDER_RE,
# which only matches identifier-shaped names, so those two locations stay
# identifier-only. Header and query names are used verbatim and real APIs need
# "X-Api-Key" / "api-version" / "filter.name", which the identifier pattern
# rejected outright.
_VERBATIM_PARAM_LOCATIONS = ("header", "query")
_IDENTIFIER_PARAM_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# The connector name arrives as a URL path parameter on the route, not through
# ConnectorBody, so ConnectorBody.name's pattern never runs on it. It ends up
# embedded in every tool name this connector registers ("mcp_{name}_{tool}",
# "api_{name}_{op}"), and every mainstream provider restricts a tool name to
# [a-zA-Z0-9_-]. One connector named with a space or a dot therefore makes the
# provider reject the WHOLE request — every turn for that user fails, not just
# calls to this connector — until someone finds and renames it.
_CONNECTOR_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,64}$")


class ApiOperationParam(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.\-]*$", max_length=64)
    location: str = Field(pattern=r"^(path|query|header|body)$")
    type: str = Field(default="string", pattern=r"^(string|number|boolean)$")
    required: bool = False
    description: str = Field(default="", max_length=200)


class ApiOperation(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,48}$")
    method: str = Field(pattern=r"^(GET|POST|PUT|PATCH|DELETE)$")
    path: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    parameters: list[ApiOperationParam] = Field(default_factory=list, max_length=20)
    # A JSON template sent as the request body — e.g. {"limit": {limit}}. Every
    # {placeholder} is substituted with json.dumps() of that body-location
    # parameter's value (see claw/tools/api.py's GenericApiTool.execute), so
    # the template never quotes a placeholder itself: {"name": {name}} is
    # correct for a string param, not {"name": "{name}"}. Only meaningful for
    # methods that carry a request payload (POST/PUT/PATCH) — see the
    # kind="api" branch of upsert_connector below.
    body: str = Field(default="", max_length=20_000)


class ConnectorBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_\-]+$")
    description: str = Field(default="", max_length=2000)
    # "mcp" speaks the MCP protocol over transport/command/url (unchanged
    # behavior); "api" is a plain REST API described by `operations`, called
    # directly over HTTP with no MCP handshake — see claw/tools/api.py.
    # None = "whatever this connector already is" (new connectors default to
    # "mcp"), so a caller that omits the field — a client predating it, or any
    # edit that doesn't care about kind — isn't rejected by the kind lock below.
    kind: str | None = Field(default=None, pattern=r"^(mcp|api)$")
    transport: str = Field(default="stdio", pattern=r"^(stdio|http)$")
    command: str = ""
    url: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    # Only meaningful when kind == "api" — see ApiOperation.
    operations: list[ApiOperation] = Field(default_factory=list, max_length=50)
    # Per-connector connect/tool-call timeout override, in milliseconds. None =
    # use the instance-wide default (claw/config.py's ConnectorSettings).
    timeout_ms: int | None = Field(default=None, ge=1000, le=120000)
    enabled: bool = True

    @field_validator("env")
    @classmethod
    def _no_case_variant_headers(cls, env: dict[str, str]) -> dict[str, str]:
        """HTTP header names are case-insensitive, so HEADER_authorization and
        HEADER_Authorization mean the same header — but they are two distinct
        dict keys, and claw/tools/api.py would put both on the wire. The server
        then picks one by its own rules, so the credential actually used is not
        the one the UI shows as effective. Rejected rather than silently merged:
        which of two conflicting values was meant is not ours to guess."""
        seen: dict[str, str] = {}
        for key in env:
            if not key.startswith(_HEADER_ENV_PREFIX):
                continue
            name = key[len(_HEADER_ENV_PREFIX) :].lower()
            if name in seen:
                raise ValueError(
                    f"'{key}' and '{seen[name]}' are the same HTTP header — "
                    "header names are case-insensitive, so keep only one"
                )
            seen[name] = key
        return env


def connector_row(c, status: dict | None = None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description or "",
        "kind": c.kind,
        "transport": c.transport,
        "command": c.command,
        "url": c.url,
        "env": c.env or {},
        "operations": c.operations or [],
        "timeout_ms": c.timeout_ms,
        "enabled": c.enabled,
        "runtime": status or {"status": "not_connected"},
    }


def connector_global_summary(c, status: dict | None = None) -> dict:
    """Redacted view for regular users (GET /api/connectors/global) and skill
    authors: no command/url/env/operations. Unlike connector_row's callers,
    which are always the connector's owner, a regular user viewing a global
    connector is never its owner, so secrets must never be included here."""
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description or "",
        "kind": c.kind,
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
    name = name.strip()
    if not _CONNECTOR_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=(
                f"connector name '{name}' may only contain lowercase letters, digits, "
                "underscores and hyphens (max 64 characters)"
            ),
        )
    # An omitted `kind` means "keep this connector as whatever it already is";
    # only an explicit, *different* kind is an error. The authoritative check
    # runs inside ConnectorStore.upsert's transaction (raising
    # ConnectorKindMismatch); this read just resolves which validation branch
    # below to apply.
    existing = await state.connectors.get_by_name(owner_id, name)
    kind = body.kind or (existing.kind if existing is not None else "mcp")

    operations: list[dict] = []
    if kind == "api":
        # An api-kind connector is a plain REST base URL, never an MCP
        # handshake target — command is always cleared regardless of what
        # transport/command the caller posted.
        if not body.url.strip():
            raise HTTPException(status_code=422, detail="api connector requires a url")
        # Belt-and-suspenders: the authoritative check runs at call time
        # (claw/tools/api.py's request hook, which resolves DNS, also
        # re-checks every redirect hop, and is immune to rebinding after this
        # save) — resolve=False here so *saving* a connector never depends on
        # DNS/network being reachable, it just rejects the common case of an
        # obviously internal/private literal IP immediately.
        try:
            await assert_public_url(body.url.strip(), resolve=False)
        # ValueError, not just UnsafeUrlError (which subclasses it): urlsplit
        # raises a bare ValueError on a malformed URL such as "http://[::1",
        # and `url` is a plain str field, so nothing upstream rejects it —
        # letting that escape turns a bad request body into a 500.
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"api connector url is not allowed: {exc}") from exc
        if not body.operations:
            raise HTTPException(status_code=422, detail="api connector requires at least one operation")
        seen_names: set[str] = set()
        for op in body.operations:
            lname = op.name.lower()
            if lname in seen_names:
                raise HTTPException(status_code=422, detail=f"duplicate operation name: {op.name}")
            seen_names.add(lname)
            tool_name = f"api_{name}_{op.name}"
            if len(tool_name) > _MAX_TOOL_NAME:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"operation '{op.name}': tool name '{tool_name}' is {len(tool_name)} characters; "
                        f"connector name + operation name must fit in {_MAX_TOOL_NAME} once combined"
                    ),
                )
            if not op.path.startswith("/"):
                raise HTTPException(status_code=422, detail=f"operation '{op.name}' path must start with /")
            if "://" in op.path or op.path.startswith("//"):
                raise HTTPException(status_code=422, detail=f"operation '{op.name}' path must not be an absolute URL")
            # "?" and "#" end the path and start a query/fragment, which this
            # model has no notion of: the placeholder substitution in
            # GenericApiTool percent-encodes every value as a path segment, and
            # the env-supplied QUERY_* credentials are merged into the query
            # separately. A "?" here therefore smuggles in parameters that
            # neither validation nor the credential-precedence rules can see.
            bad_char = next((c for c in "?#" if c in op.path), None)
            if bad_char is not None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"operation '{op.name}' path must not contain '{bad_char}' — declare query "
                        "parameters instead of putting them in the path"
                    ),
                )
            # httpx resolves dot segments before dialling, so "/../admin" on a
            # base url of ".../v1/public" reaches "/admin" — outside the prefix
            # the connector was deliberately scoped to.
            if any(segment in (".", "..") for segment in op.path.split("/")):
                raise HTTPException(
                    status_code=422,
                    detail=f"operation '{op.name}' path must not contain '.' or '..' segments",
                )
            non_identifier = sorted(
                p.name
                for p in op.parameters
                if p.location not in _VERBATIM_PARAM_LOCATIONS and not _IDENTIFIER_PARAM_RE.match(p.name)
            )
            if non_identifier:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"operation '{op.name}': path and body parameter names may only contain letters, "
                        f"digits and underscores: {non_identifier}"
                    ),
                )
            param_names = [p.name for p in op.parameters]
            if len(param_names) != len(set(param_names)):
                dupes = sorted({n for n in param_names if param_names.count(n) > 1})
                raise HTTPException(
                    status_code=422, detail=f"operation '{op.name}': duplicate parameter name(s): {dupes}"
                )
            path_params = set(_PATH_PLACEHOLDER_RE.findall(op.path))
            declared_path_params = {p.name for p in op.parameters if p.location == "path"}
            if path_params != declared_path_params:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"operation '{op.name}': path placeholders {sorted(path_params)} must exactly "
                        f"match declared path parameters {sorted(declared_path_params)}"
                    ),
                )
            # A path parameter that's missing from the call substitutes as ""
            # (see claw/tools/api.py's GenericApiTool.execute), silently
            # hitting the wrong URL — so path parameters must always be
            # required, unlike query/header ones.
            optional_path_params = {p.name for p in op.parameters if p.location == "path" and not p.required}
            if optional_path_params:
                raise HTTPException(
                    status_code=422,
                    detail=f"operation '{op.name}': path parameters must be required: {sorted(optional_path_params)}",
                )
            declared_body_params = {p.name for p in op.parameters if p.location == "body"}
            if op.body.strip():
                if op.method not in ("POST", "PUT", "PATCH"):
                    raise HTTPException(
                        status_code=422,
                        detail=f"operation '{op.name}': request body is only supported for POST/PUT/PATCH",
                    )
                body_params = set(_PATH_PLACEHOLDER_RE.findall(op.body))
                if body_params != declared_body_params:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"operation '{op.name}': body placeholders {sorted(body_params)} must exactly "
                            f"match declared body parameters {sorted(declared_body_params)}"
                        ),
                    )
                # A missing body parameter would leave a literal "{name}" token
                # in the substituted payload (see GenericApiTool.execute),
                # breaking the JSON — same reasoning as path parameters above.
                optional_body_params = {p.name for p in op.parameters if p.location == "body" and not p.required}
                if optional_body_params:
                    raise HTTPException(
                        status_code=422,
                        detail=f"operation '{op.name}': body parameters must be required: {sorted(optional_body_params)}",
                    )
                dummy_by_type = {"string": "x", "number": 1, "boolean": True}
                dummies = {p.name: dummy_by_type.get(p.type, "x") for p in op.parameters if p.location == "body"}
                filled = _PATH_PLACEHOLDER_RE.sub(lambda m: json.dumps(dummies[m.group(1)]), op.body)
                try:
                    json.loads(filled)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail=f"operation '{op.name}': body is not valid JSON: {exc}"
                    ) from exc
            elif declared_body_params:
                raise HTTPException(
                    status_code=422,
                    detail=f"operation '{op.name}': declared body parameters but no body template: {sorted(declared_body_params)}",
                )
            operations.append(op.model_dump())
    else:
        if body.transport == "stdio" and not body.command.strip():
            raise HTTPException(status_code=422, detail="stdio connector requires a command")
        if body.transport == "http" and not body.url.strip():
            raise HTTPException(status_code=422, detail="http connector requires a url")
        if body.transport == "http" and owner_id is not None:
            # An http MCP endpoint is dialled from this host exactly like a
            # kind="api" base url is, so it needs the same SSRF guard — the
            # authoritative one runs at connect time (ConnectorManager._connect,
            # which resolves DNS); this is the same resolve=False fast reject of
            # an obviously internal literal IP, so the user finds out on save
            # rather than seeing the connector sit in "error".
            #
            # Only for a user-owned connector: an admin-global one is operator
            # configuration and may legitimately point at internal
            # infrastructure (see _connect for the full reasoning).
            try:
                await assert_public_url(body.url.strip(), resolve=False)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail=f"http connector url is not allowed: {exc}"
                ) from exc
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
    try:
        row = await state.connectors.upsert(
            owner_id,
            name,
            description=body.description,
            kind=kind,
            transport="http" if kind == "api" else body.transport,
            command="" if kind == "api" else body.command,
            url=body.url,
            env=body.env,
            # Force-cleared server-side for kind="mcp" regardless of what was
            # posted — defense in depth, mirrors the command clear above.
            operations=operations if kind == "api" else None,
            timeout_ms=body.timeout_ms,
            enabled=body.enabled,
        )
    except ConnectorKindMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
