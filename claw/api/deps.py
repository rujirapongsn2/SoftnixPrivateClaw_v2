"""Shared FastAPI dependencies: app state accessors and auth."""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, WebSocket

from claw.browser.broker import BrowserBrokerStore
from claw.config import Settings
from claw.core.bus import EventBus
from claw.core.connectors import ConnectorManager
from claw.core.runtime import AgentRuntime
from claw.core.scheduler import SchedulerService
from claw.db.models import User
from claw.security.policy import PolicyEngine
from claw.db.stores import (
    AuditStore,
    ConnectorStore,
    FeedbackStore,
    GroupStore,
    GuardrailStore,
    KnowledgeStore,
    LLMConfigStore,
    MemoryStore,
    MessageStore,
    OAuthAppStore,
    ScheduleStore,
    SessionStore,
    ShareStore,
    SkillStore,
    TelegramConfigStore,
    UsageStore,
    UserStore,
)


@dataclass
class AppState:
    settings: Settings
    runtime: AgentRuntime
    bus: EventBus
    users: UserStore
    groups: GroupStore
    sessions: SessionStore
    messages: MessageStore
    skills: SkillStore
    memories: MemoryStore
    connectors: ConnectorStore
    connectors_mgr: ConnectorManager
    schedules: ScheduleStore
    scheduler: SchedulerService
    policy: PolicyEngine
    telegram_link: "LinkCodeService"
    usage: UsageStore
    feedback: FeedbackStore
    guardrails: GuardrailStore
    llm_config: LLMConfigStore
    audit: AuditStore
    oauth_apps: OAuthAppStore
    browser_broker: BrowserBrokerStore
    knowledge: KnowledgeStore
    knowledge_service: "KnowledgeService"
    shares: ShareStore
    telegram_config: TelegramConfigStore
    telegram_mgr: "TelegramManager"
    telegram: "TelegramChannel | None" = None


def get_state(request: Request) -> AppState:
    return request.app.state.claw


def _bearer(authorization: str | None, query_token: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (query_token or "").strip()


async def _authenticate(
    state: AppState, *, token: str, email: str, dev_token: str
) -> User:
    """Resolve a caller to a User.

    Primary path: verify a JWT bearer token → user id. Fallback (only when
    auth_mode == 'dev'): a static dev token + email, for local scripts/tests.
    Suspended users are always rejected.
    """
    from claw.auth.tokens import TokenError, decode_access_token

    user: User | None = None
    if token:
        try:
            payload = decode_access_token(token, state.settings.secret_key)
            user = await state.users.get(str(payload.get("sub")))
        except TokenError:
            user = None

    if user is None and state.settings.auth_mode == "dev":
        if token and token == dev_token and email:
            user = await state.users.get_or_create_by_email(email)

    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="account suspended")
    return user


async def current_user(request: Request, state: AppState = Depends(get_state)) -> User:
    token = _bearer(request.headers.get("authorization"), request.query_params.get("token"))
    email = request.headers.get("x-user-email") or request.query_params.get("email") or ""
    return await _authenticate(state, token=token, email=email, dev_token=state.settings.dev_token)


async def require_admin(request: Request, state: AppState = Depends(get_state)) -> User:
    """Gate system-administration endpoints behind the is_admin flag."""
    user = await current_user(request, state)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="requires administrator privileges")
    return user


async def current_user_ws(websocket: WebSocket) -> User:
    state: AppState = websocket.app.state.claw
    token = _bearer(websocket.headers.get("authorization"), websocket.query_params.get("token"))
    email = websocket.headers.get("x-user-email") or websocket.query_params.get("email") or ""
    try:
        return await _authenticate(
            state, token=token, email=email, dev_token=state.settings.dev_token
        )
    except HTTPException:
        await websocket.close(code=4401)
        raise
