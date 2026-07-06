"""System administration API — manage all users. Requires the is_admin flag."""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from claw.api.deps import AppState, get_state, require_admin
from claw.auth import oidc
from claw.auth.passwords import hash_password
from claw.channels.telegram import validate_bot_token
from claw.db.models import User
from claw.security.policy import rule_from_row

router = APIRouter(prefix="/api/admin")

_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


async def _reload_policy(state: AppState) -> None:
    """Rebuild the live PolicyEngine from persisted guardrail config."""
    rules = await state.guardrails.list_rules()
    monitor_only = await state.guardrails.get_monitor_only(
        default=not state.settings.policy_enforce
    )
    state.policy.reload([rule_from_row(r) for r in rules], monitor_only=monitor_only)


class CreateUserBody(BaseModel):
    email: str = Field(pattern=_EMAIL_RE, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""
    is_admin: bool = False


class UpdateUserBody(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None
    display_name: str | None = Field(default=None, max_length=255)
    # Admin-set new password (reset). Validated only when present.
    password: str | None = Field(default=None, min_length=8, max_length=128)


def _user_row(user: User, sessions: int) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "role": user.role,
        "sessions": sessions,
        "created_at": user.created_at.isoformat(),
    }


@router.get("/users")
async def list_users(admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> list:
    users = await state.users.list_all()
    counts = await state.sessions.count_by_user()
    return [_user_row(u, counts.get(u.id, 0)) for u in users]


@router.post("/users")
async def create_user(
    body: CreateUserBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    if await state.users.get_by_email(body.email) is not None:
        raise HTTPException(status_code=409, detail="email already registered")
    user = await state.users.create(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        is_admin=body.is_admin,
        role="admin" if body.is_admin else "user",
    )
    return _user_row(user, 0)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    body: UpdateUserBody,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    # Guard against an admin locking themselves out or demoting the last admin.
    if user_id == admin.id and (body.is_admin is False or body.is_active is False):
        raise HTTPException(status_code=400, detail="you cannot demote or suspend yourself")
    if body.is_admin is not None or body.is_active is not None:
        updated = await state.users.update_flags(
            user_id, is_admin=body.is_admin, is_active=body.is_active
        )
    else:
        updated = await state.users.get(user_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")
    if body.display_name is not None or body.password:
        updated = await state.users.update_profile(
            user_id,
            display_name=body.display_name,
            password_hash=hash_password(body.password) if body.password else None,
        )
    counts = await state.sessions.count_by_user()
    return _user_row(updated, counts.get(updated.id, 0))


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="you cannot delete your own account")
    target = await state.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    # Never leave the deployment without an administrator.
    if target.is_admin and await state.users.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last administrator")
    await state.users.delete(user_id)
    return {"deleted": True}


async def _build_stats(state: AppState) -> dict:
    users = await state.users.list_all()
    tokens = await state.usage.totals()
    feedback = await state.feedback.totals()
    memory = await state.memories.stats()
    return {
        "users": len(users),
        "admins": sum(1 for u in users if u.is_admin),
        "suspended": sum(1 for u in users if not u.is_active),
        "active_users": await state.sessions.active_user_count(7),
        "sessions": await state.sessions.total(),
        "messages": await state.messages.total(),
        "prompt_tokens": tokens["prompt_tokens"],
        "completion_tokens": tokens["completion_tokens"],
        "turns": tokens.get("turns", 0),
        "feedback_up": feedback["up"],
        "feedback_down": feedback["down"],
        "consolidations": memory["consolidations"],
        "memory_users": memory["memory_users"],
        "policy_enforcing": not state.policy.monitor_only,
        # Browser automation is "on" via EITHER path: the server-side Playwright
        # browser, or a paired client Chrome extension. Checking only `.enabled`
        # under-reports capability for deployments that use just the extension.
        "browser_enabled": state.settings.browser.enabled
        or state.settings.browser.client_extension_enabled,
        "telegram_enabled": bool(state.settings.telegram_bot_token),
    }


@router.get("/stats")
async def stats(admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    return await _build_stats(state)


async def _provider_summaries(state: AppState) -> list[dict]:
    """Configured LLM providers + how many models each exposes, for the
    overview's "which providers are in use" card. No secrets included."""
    if state.llm_config is None:
        return []
    providers = await state.llm_config.list_providers()
    all_models = await state.llm_config.list_models()
    out = []
    for p in providers:
        models = [m for m in all_models if m.provider_id == p.id]
        out.append(
            {
                "name": p.name,
                "enabled": p.enabled,
                "has_key": bool(p.api_key),
                "model_count": len(models),
                "enabled_model_count": sum(1 for m in models if m.enabled),
            }
        )
    return out


@router.get("/overview")
async def overview(admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    return {
        "stats": await _build_stats(state),
        "activity_by_day": await state.messages.activity_by_day(14),
        "activity_by_hour": await state.messages.activity_by_hour(),
        "usage_by_model": await state.usage.by_model(),
        "providers": await _provider_summaries(state),
        "sessions_by_user_7d": await state.sessions.by_user_since(7),
        "sessions_by_day_7d": await state.sessions.by_day_since(7),
        "guardrail_hits_by_day": await state.audit.policy_hits_by_day(14),
    }


# ---------------------------------------------------------------- LLM providers/models

class ProviderBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    api_key: str = ""
    api_base: str = ""
    enabled: bool = True


class ProviderPatch(BaseModel):
    name: str | None = None
    api_key: str | None = None  # empty/None keeps the existing key
    api_base: str | None = None
    enabled: bool | None = None


_COST_RE = r"^(low|medium|high|very_high)$"


class ModelBody(BaseModel):
    model_id: str = Field(min_length=1, max_length=128)
    label: str = ""
    enabled: bool = True
    cost: str = Field(default="medium", pattern=_COST_RE)
    description: str = ""


class ModelPatch(BaseModel):
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    label: str | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    cost: str | None = Field(default=None, pattern=_COST_RE)
    description: str | None = None


def _provider_row(p, models: list) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "api_base": p.api_base,
        "has_key": bool(p.api_key),
        "enabled": p.enabled,
        "models": [_model_row(m) for m in models if m.provider_id == p.id],
    }


def _model_row(m) -> dict:
    return {
        "id": m.id,
        "model_id": m.model_id,
        "label": m.label or m.model_id,
        "enabled": m.enabled,
        "is_default": m.is_default,
        "cost": m.cost or "medium",
        "description": m.description or "",
    }


@router.get("/llm")
async def list_llm(admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    providers = await state.llm_config.list_providers()
    models = await state.llm_config.list_models()
    return {"providers": [_provider_row(p, models) for p in providers]}


@router.post("/providers")
async def create_provider(
    body: ProviderBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    if await state.llm_config.get_by_name(body.name) is not None:
        raise HTTPException(status_code=409, detail="a provider with this name already exists")
    p = await state.llm_config.create_provider(body.name, body.api_key, body.api_base, body.enabled)
    return _provider_row(p, [])


@router.patch("/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    body: ProviderPatch,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    if body.name:
        existing = await state.llm_config.get_by_name(body.name)
        if existing is not None and existing.id != provider_id:
            raise HTTPException(status_code=409, detail="a provider with this name already exists")
    p = await state.llm_config.update_provider(provider_id, **body.model_dump(exclude_none=True))
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    models = await state.llm_config.list_models()
    return _provider_row(p, models)


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    ok = await state.llm_config.delete_provider(provider_id)
    if not ok:
        raise HTTPException(status_code=404, detail="provider not found")
    return {"deleted": True}


@router.post("/providers/{provider_id}/models")
async def create_model(
    provider_id: str,
    body: ModelBody,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    m = await state.llm_config.create_model(
        provider_id, body.model_id, body.label, body.enabled, body.cost, body.description
    )
    return _model_row(m)


@router.patch("/models/{model_pk}")
async def update_model(
    model_pk: str,
    body: ModelPatch,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    m = await state.llm_config.update_model(model_pk, **body.model_dump(exclude_none=True))
    if m is None:
        raise HTTPException(status_code=404, detail="model not found")
    return _model_row(m)


@router.delete("/models/{model_pk}")
async def delete_model(
    model_pk: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    ok = await state.llm_config.delete_model(model_pk)
    if not ok:
        raise HTTPException(status_code=404, detail="model not found")
    return {"deleted": True}


# ---------------------------------------------------------------- guardrails

class MonitorBody(BaseModel):
    monitor_only: bool


class RuleBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="keyword", pattern=r"^(keyword|regex)$")
    pattern: str = Field(min_length=1)
    action: str = Field(default="block", pattern=r"^(mask|block|monitor)$")
    severity: str = Field(default="medium")
    placeholder: str = "[REDACTED]"


class RulePatch(BaseModel):
    enabled: bool | None = None
    action: str | None = Field(default=None, pattern=r"^(mask|block|monitor)$")
    name: str | None = Field(default=None, min_length=1, max_length=64)
    pattern: str | None = Field(default=None, min_length=1)
    severity: str | None = None
    # "keyword" escapes the pattern literally; "regex" uses it as-is (validated).
    kind: str | None = Field(default=None, pattern=r"^(keyword|regex)$")


def _rule_row(r) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "pattern": r.pattern,
        "action": r.action,
        "scopes": r.scopes,
        "severity": r.severity,
        "placeholder": r.placeholder,
        "enabled": r.enabled,
        "is_builtin": r.is_builtin,
    }


@router.get("/guardrails")
async def get_guardrails(
    admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    rules = await state.guardrails.list_rules()
    monitor_only = await state.guardrails.get_monitor_only(default=not state.settings.policy_enforce)
    return {"monitor_only": monitor_only, "rules": [_rule_row(r) for r in rules]}


@router.put("/guardrails")
async def set_guardrails(
    body: MonitorBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    await state.guardrails.set_monitor_only(body.monitor_only)
    await _reload_policy(state)
    return {"monitor_only": body.monitor_only}


@router.post("/guardrails/rules")
async def create_rule(
    body: RuleBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    # Keyword rules are matched literally (escaped); regex rules use the pattern as-is
    # but are validated so a bad pattern can never break enforcement at runtime.
    pattern = re.escape(body.pattern) if body.kind == "keyword" else body.pattern
    try:
        re.compile(pattern)
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"invalid regex: {exc}")
    r = await state.guardrails.create_rule(
        name=body.name,
        pattern=pattern,
        action=body.action,
        scopes=["input", "output", "tool_args"],
        placeholder=body.placeholder,
        severity=body.severity,
        enabled=True,
        is_builtin=False,
    )
    await _reload_policy(state)
    return _rule_row(r)


@router.patch("/guardrails/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    body: RulePatch,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    fields = body.model_dump(exclude_none=True)
    kind = fields.pop("kind", None)
    if "pattern" in fields:
        # Escape keyword patterns; validate regex so a bad edit can't break enforcement.
        if kind == "keyword":
            fields["pattern"] = re.escape(fields["pattern"])
        try:
            re.compile(fields["pattern"])
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"invalid regex: {exc}")
    r = await state.guardrails.update_rule(rule_id, **fields)
    if r is None:
        raise HTTPException(status_code=404, detail="rule not found")
    await _reload_policy(state)
    return _rule_row(r)


@router.delete("/guardrails/rules/{rule_id}")
async def delete_rule(
    rule_id: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    ok = await state.guardrails.delete_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=400, detail="rule not found or is built-in")
    await _reload_policy(state)
    return {"deleted": True}


class GuardrailTestBody(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@router.post("/guardrails/test")
async def test_guardrails(
    body: GuardrailTestBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    """Run the live policy against sample text across all scopes so admins can
    see exactly what the current rules would mask or block — proof it works."""
    # Order matters for the summary: block wins over mask wins over monitor.
    rank = {"block": 3, "mask": 2, "monitor": 1}
    matched: dict[str, str] = {}
    masked_text = body.text
    top_action: str | None = None
    severity = "info"
    for scope in ("input", "output", "tool_args"):
        decision = state.policy.enforce(body.text, scope)
        for name in decision.matched_rules:
            matched.setdefault(name, scope)
        if decision.action is not None:
            action = decision.action.value
            if top_action is None or rank[action] > rank[top_action]:
                top_action = action
                severity = decision.severity
        # Prefer showing the masked rendering (output scope covers replies).
        if scope == "output":
            masked_text = decision.text
    return {
        "action": top_action,
        "matched_rules": [{"name": n, "scope": s} for n, s in matched.items()],
        "masked": masked_text,
        "severity": severity,
        "monitor_only": state.policy.monitor_only,
    }


# ---------------------------------------------------------------- OAuth apps (connector sign-in)

class OAuthAppBody(BaseModel):
    client_id: str = ""
    client_secret: str = ""  # empty keeps the existing secret
    tenant: str = ""  # microsoft only


def _connector_redirect_uri(state: AppState, provider: str) -> str:
    return f"{state.settings.public_base_url.rstrip('/')}/api/connectors/oauth/{provider}/callback"


@router.get("/oauth-apps")
async def get_oauth_apps(
    admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    return {
        "google": await state.oauth_apps.public("google"),
        "microsoft": await state.oauth_apps.public("microsoft"),
        # One OAuth app powers two flows, each with its own callback — the admin
        # must register BOTH redirect URIs in the provider's app.
        "redirect_uris": {
            "google": _connector_redirect_uri(state, "google"),
            "microsoft": _connector_redirect_uri(state, "microsoft"),
        },
        "login_redirect_uris": {
            "google": oidc.redirect_uri(state.settings, "google"),
            "microsoft": oidc.redirect_uri(state.settings, "microsoft"),
        },
    }


@router.put("/oauth-apps/{provider}")
async def set_oauth_app(
    provider: str,
    body: OAuthAppBody,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    if provider not in ("google", "microsoft"):
        raise HTTPException(status_code=404, detail="unknown provider")
    await state.oauth_apps.set(provider, body.client_id.strip(), body.client_secret.strip(), body.tenant.strip())
    return await state.oauth_apps.public(provider)


# ---------------------------------------------------------------- Telegram

class TelegramConfigBody(BaseModel):
    bot_token: str = ""  # empty keeps the existing token (e.g. only toggling `enabled`)
    enabled: bool = True


async def _telegram_config_json(state: AppState) -> dict:
    cfg = await state.telegram_config.get()
    if cfg is None:
        # Never configured through this admin UI — report whether the env var
        # fallback (CLAW_TELEGRAM_BOT_TOKEN) currently supplies a token, so the
        # admin knows saving here will take over from it.
        has_env_token = bool(state.settings.telegram_bot_token)
        pub = {"has_token": has_env_token, "enabled": has_env_token, "source": "env" if has_env_token else "none"}
    else:
        pub = {**await state.telegram_config.public(), "source": "database"}
    return {
        **pub,
        "running": state.telegram is not None,
        "bot_username": getattr(state.telegram, "bot_username", "") if state.telegram is not None else "",
    }


@router.get("/telegram")
async def get_telegram_config(
    admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    return await _telegram_config_json(state)


@router.put("/telegram")
async def set_telegram_config(
    body: TelegramConfigBody,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    token = body.bot_token.strip()
    if token:
        # Confirm the token actually works before persisting it — a pasted-wrong
        # token fails loudly here instead of the bot silently never connecting.
        try:
            await validate_bot_token(token)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=f"Could not verify this bot token with Telegram: {exc}"
            ) from exc
    elif body.enabled:
        existing = await state.telegram_config.get()
        if not existing or not existing.get("bot_token"):
            raise HTTPException(status_code=422, detail="A bot token is required to enable Telegram.")

    await state.telegram_config.set(token, body.enabled)
    cfg = await state.telegram_config.get()
    effective_token = (cfg["bot_token"] if cfg["enabled"] else "") if cfg else ""
    state.telegram = await state.telegram_mgr.ensure_running(effective_token)
    return await _telegram_config_json(state)


# ---------------------------------------------------------------- audit logs

@router.get("/audit")
async def audit_logs(
    kind: str | None = None,
    user_id: str | None = None,
    search: str | None = None,
    before: str | None = None,
    limit: int = 50,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    from datetime import datetime

    before_dt = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
        except ValueError:
            before_dt = None
    rows = await state.audit.list(
        kind=kind, user_id=user_id, search=search, before=before_dt, limit=limit
    )
    # Attach a human-readable actor to each row so admins see WHO, not a raw id.
    labels = {u.id: (u.display_name or u.email) for u in await state.users.list_all()}
    for r in rows:
        r["user_label"] = labels.get(r["user_id"], "System" if r["user_id"] is None else "Unknown user")
    # has_more drives the "Load more" button; next cursor is the last row's time.
    return {
        "events": rows,
        "kinds": await state.audit.kinds(),
        "has_more": len(rows) == limit,
        "next_before": rows[-1]["created_at"] if rows else None,
    }
