"""Agent Channel HTTP API.

- /api/agent/v1/*        the endpoint an external agent calls, with its API key
- /api/agent-clients     a user's own agent keys (Settings > Agent access)
- /api/admin/agent-channel  the on/off switch and every key (Control Plane)
"""

from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from claw.api.deps import AppState, current_user, get_state, require_admin
from claw.api.settings_acl import acl_for_user
from claw.channels.agent import TURN_STALE_AFTER, AgentBusy, ConversationLimit
from claw.channels.agent_webhook import InvalidCallbackUrl, validate_callback_url
from claw.core.limits import RateLimiter
from claw.db.models import AgentClient, AgentTurn, User
from claw.db.stores import AgentChannelStore, AgentKeyInactive, agent_client_status

router = APIRouter()

# Per key, polls included: a bot polling every two seconds stays well inside it.
_per_client = RateLimiter(60)
# Failed authentications per remote address, checked before any database read.
# Behind a reverse proxy every caller shares the proxy's address, so this is a
# global brake on key guessing; valid keys are counted separately above.
_failed_auth = RateLimiter(30)


def _client_json(client: AgentClient, owner_email: str | None = None) -> dict:
    out = {
        "id": client.id,
        "name": client.name,
        "key_prefix": f"pcak_{client.key_prefix}_…",
        "connector_ids": list(client.connector_ids or []),
        "status": agent_client_status(client),
        "expires_at": client.expires_at.isoformat() if client.expires_at else None,
        "last_used_at": client.last_used_at.isoformat() if client.last_used_at else None,
        "created_at": client.created_at.isoformat() if client.created_at else None,
        # The owner sees the full URL; the Control Plane only its host, as a
        # URL may carry a token of the receiver's in its path or query.
        "callback_url": client.callback_url if owner_email is None else None,
        "callback_host": urlsplit(client.callback_url).hostname if client.callback_url else None,
    }
    if owner_email is not None:
        out["owner_email"] = owner_email
    return out


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _turn_json(turn: AgentTurn, listing: bool = False) -> dict:
    out = {
        "turn_id": turn.id,
        "conversation_id": turn.conversation_id,
        "status": turn.status,
        "reply": turn.reply if turn.status != "working" else None,
        "created_at": _iso(turn.created_at),
        "finished_at": _iso(turn.finished_at),
    }
    if listing:
        out["message_preview"] = turn.message_preview or ""
        out["webhook"] = (
            {"status": turn.webhook_status, "attempts": turn.webhook_attempts, "at": _iso(turn.webhook_at)}
            if turn.webhook_status
            else None
        )
    return out


def _error(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


# ------------------------------------------------------------- agent-facing

async def _agent_client(request: Request, state: AppState) -> AgentClient | JSONResponse:
    """Resolve the calling client from `Authorization: Bearer <key>`, or the error to return.

    The key is accepted from the header only: a query string ends up in proxy logs.
    """
    if not await state.agent_channel_store.enabled():
        return _error(403, "The agent channel is turned off on this server.")
    remote = request.client.host if request.client else "unknown"
    if _failed_auth.exhausted(remote):
        return _error(429, "Too many failed attempts. Try again in a minute.", {"Retry-After": "60"})
    header = request.headers.get("authorization") or ""
    key = header[7:].strip() if header.lower().startswith("bearer ") else ""
    client = await state.agent_channel_store.authenticate(key) if key else None
    if client is None:
        _failed_auth.allow(remote)
        return _error(401, "Invalid, revoked or expired API key.")
    if not _per_client.allow(client.id):
        return _error(429, "Too many requests for this key. Slow down.", {"Retry-After": "60"})
    owner = await state.users.get(client.owner_id)
    if owner is None or not owner.is_active:
        return _error(403, "The account this key belongs to is not active.")
    if not await _may_use_agents(state, owner):
        return _error(403, "Agent access is not allowed for the account this key belongs to.")
    return client


async def _may_use_agents(state: AppState, user: User) -> bool:
    """The admin's Menu ACL gates the feature itself, not only the Settings menu."""
    return "agents" not in (await acl_for_user(state, user))["hidden"]


class AgentMessageBody(BaseModel):
    message: str = Field(min_length=1, max_length=16_000)
    conversation_id: str = Field(default="default", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    locale: str = Field(default="en", max_length=8)


@router.post("/api/agent/v1/messages")
async def agent_message(
    body: AgentMessageBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, max_length=128),
    state: AppState = Depends(get_state),
):
    client = await _agent_client(request, state)
    if isinstance(client, JSONResponse):
        return client
    try:
        turn = await state.agent_channel.send(
            client, body.conversation_id, body.message, body.locale[:2], idempotency_key
        )
    except AgentBusy:
        return _error(429, "This agent already has a message running. Retry when it finishes.", {"Retry-After": "5"})
    except ConversationLimit:
        return _error(409, "This key has too many conversations. Reuse an existing conversation_id.")
    except (IntegrityError, LookupError):
        return _error(409, "The message could not be recorded. Check the key and retry.")
    return JSONResponse(_turn_json(turn), status_code=202 if turn.status == "working" else 200)


@router.get("/api/agent/v1/turns/{turn_id}")
async def agent_turn(turn_id: str, request: Request, state: AppState = Depends(get_state)):
    client = await _agent_client(request, state)
    if isinstance(client, JSONResponse):
        return client
    await state.agent_channel_store.fail_stale_turns(datetime.now(timezone.utc) - TURN_STALE_AFTER, client.id)
    turn = await state.agent_channel_store.get_turn(client.id, turn_id)
    if turn is None:
        return _error(404, "No such turn.")
    return _turn_json(turn)


_CONVERSATION_ID = r"^[A-Za-z0-9._:-]+$"
TurnStatus = Literal["working", "completed", "failed"]


async def _list_turns(
    request: Request,
    state: AppState,
    conversation_id: str | None,
    status: str | None,
    since: datetime | None,
    limit: int,
):
    client = await _agent_client(request, state)
    if isinstance(client, JSONResponse):
        return client
    store = state.agent_channel_store
    await store.fail_stale_turns(datetime.now(timezone.utc) - TURN_STALE_AFTER, client.id)
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    turns = await store.list_turns(client.id, conversation_id, status, since, limit)
    return {"turns": [_turn_json(t, listing=True) for t in turns]}


@router.get("/api/agent/v1/conversations/{conversation_id}/turns")
async def agent_conversation_turns(
    request: Request,
    conversation_id: str = Path(min_length=1, max_length=128, pattern=_CONVERSATION_ID),
    status: TurnStatus | None = None,
    since: datetime | None = None,
    limit: int = Query(default=5, ge=1, le=AgentChannelStore.MAX_LIST),
    state: AppState = Depends(get_state),
):
    """The newest turns of one of the calling key's conversations: find a turn
    whose id was lost, e.g. "is my last question done yet?"."""
    return await _list_turns(request, state, conversation_id, status, since, limit)


@router.get("/api/agent/v1/turns")
async def agent_turns(
    request: Request,
    conversation_id: str | None = Query(default=None, min_length=1, max_length=128, pattern=_CONVERSATION_ID),
    status: TurnStatus | None = None,
    since: datetime | None = None,
    limit: int = Query(default=5, ge=1, le=AgentChannelStore.MAX_LIST),
    state: AppState = Depends(get_state),
):
    """The calling key's newest turns, optionally of one conversation or since a time."""
    return await _list_turns(request, state, conversation_id, status, since, limit)


# ------------------------------------------------------------- the owner's keys

class AgentClientBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    connector_ids: list[str] = Field(default_factory=list, max_length=50)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class AgentConnectorsBody(BaseModel):
    connector_ids: list[str] = Field(default_factory=list, max_length=50)


async def _checked_connectors(state: AppState, user: User, ids: list[str]) -> list[str]:
    reachable = {c.id for c in await state.connectors.enabled_accessible(user.id)}
    unknown = [i for i in ids if i not in reachable]
    if unknown:
        raise HTTPException(status_code=422, detail="Some connectors are not available to you.")
    return list(dict.fromkeys(ids))


@router.get("/api/agent-clients")
async def list_agent_clients(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    connectors = await state.connectors.enabled_accessible(user.id)
    return {
        "enabled": await state.agent_channel_store.enabled(),
        "clients": [_client_json(c) for c in await state.agent_channel_store.list_for_owner(user.id)],
        "connectors": [
            {"id": c.id, "name": c.name, "scope": "global" if c.owner_id is None else "own"} for c in connectors
        ],
    }


@router.post("/api/agent-clients")
async def create_agent_client(
    body: AgentClientBody, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    if not await _may_use_agents(state, user):
        raise HTTPException(status_code=403, detail="Agent access is not available to you.")
    if await state.agent_channel_store.count_active(user.id) >= AgentChannelStore.MAX_ACTIVE_PER_OWNER:
        raise HTTPException(
            status_code=409,
            detail=f"You already have {AgentChannelStore.MAX_ACTIVE_PER_OWNER} active agent keys. Revoke one first.",
        )
    connector_ids = await _checked_connectors(state, user, body.connector_ids)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    )
    client, key = await state.agent_channel_store.create_client(user.id, body.name.strip(), connector_ids, expires_at)
    await state.audit.log("agent_channel.key_created", {"client_id": client.id, "client": client.name}, user_id=user.id)
    return {"client": _client_json(client), "key": key}


@router.put("/api/agent-clients/{client_id}")
async def update_agent_client(
    client_id: str, body: AgentConnectorsBody, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    if not await _may_use_agents(state, user):
        raise HTTPException(status_code=403, detail="Agent access is not available to you.")
    connector_ids = await _checked_connectors(state, user, body.connector_ids)
    client = await state.agent_channel_store.set_connectors(user.id, client_id, connector_ids)
    if client is None:
        raise HTTPException(status_code=404, detail="No such agent key.")
    await state.audit.log(
        "agent_channel.key_connectors_changed",
        {"client_id": client.id, "client": client.name, "connector_ids": connector_ids},
        user_id=user.id,
    )
    return _client_json(client)


class AgentCallbackBody(BaseModel):
    # None or "" removes the webhook.
    callback_url: str | None = Field(default=None, max_length=2048)
    rotate_secret: bool = False


@router.put("/api/agent-clients/{client_id}/callback")
async def set_agent_callback(
    client_id: str, body: AgentCallbackBody, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    """Set, change or remove a key's completion webhook. The signing secret is
    returned only when a new one is made (first URL, or rotate_secret)."""
    if not await _may_use_agents(state, user):
        raise HTTPException(status_code=403, detail="Agent access is not available to you.")
    url: str | None = None
    if body.callback_url and body.callback_url.strip():
        allow_http = bool(getattr(state.agent_channel, "webhook_allow_http", False))
        try:
            url = await validate_callback_url(body.callback_url, allow_http=allow_http)
        except InvalidCallbackUrl as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = await state.agent_channel_store.set_callback(user.id, client_id, url, body.rotate_secret)
    except AgentKeyInactive:
        raise HTTPException(status_code=409, detail="This agent key is revoked or expired.") from None
    if result is None:
        raise HTTPException(status_code=404, detail="No such agent key.")
    client, secret = result
    await state.audit.log(
        "agent_channel.key_callback_changed",
        {
            "client_id": client.id,
            "client": client.name,
            "callback_host": urlsplit(url).hostname if url else None,
            "secret_rotated": secret is not None,
        },
        user_id=user.id,
    )
    out: dict = {"client": _client_json(client)}
    if secret is not None:
        out["secret"] = secret
    return out


@router.delete("/api/agent-clients/{client_id}")
async def revoke_agent_client(
    client_id: str, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    if not await state.agent_channel_store.revoke(client_id, owner_id=user.id):
        raise HTTPException(status_code=404, detail="No such agent key.")
    await state.audit.log("agent_channel.key_revoked", {"client_id": client_id}, user_id=user.id)
    return {"revoked": True}


# ------------------------------------------------------------- Control Plane

class AgentChannelSwitch(BaseModel):
    enabled: bool


@router.get("/api/admin/agent-channel")
async def admin_agent_channel(admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    clients, total = await state.agent_channel_store.list_all()
    return {
        "enabled": await state.agent_channel_store.enabled(),
        "clients": [_client_json(c, email) for c, email in clients],
        "total": total,
    }


@router.put("/api/admin/agent-channel")
async def set_agent_channel(
    body: AgentChannelSwitch, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    await state.agent_channel_store.set_enabled(body.enabled)
    await state.audit.log("agent_channel.switched", {"enabled": body.enabled}, user_id=admin.id)
    return {"enabled": body.enabled}


@router.delete("/api/admin/agent-channel/clients/{client_id}")
async def admin_revoke_agent_client(
    client_id: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    if not await state.agent_channel_store.revoke(client_id):
        raise HTTPException(status_code=404, detail="No such agent key.")
    await state.audit.log("agent_channel.key_revoked", {"client_id": client_id, "by_admin": True}, user_id=admin.id)
    return {"revoked": True}
