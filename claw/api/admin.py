"""System administration API — manage all users. Requires the is_admin flag."""

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from claw.api import llm_shared as llm
from claw.api.deps import AppState, get_state, require_admin
from claw.auth import oidc
from claw.auth.passwords import hash_password
from claw.channels.telegram import validate_bot_token
from claw.db.models import User
from claw.security.policy import DEFAULT_TOOL_ARGS_EXEMPT, rule_from_row

router = APIRouter(prefix="/api/admin")

_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


async def _reload_policy(state: AppState) -> None:
    """Rebuild the live PolicyEngine from persisted guardrail config."""
    rules = await state.guardrails.list_rules()
    monitor_only = await state.guardrails.get_monitor_only(
        default=not state.settings.policy_enforce
    )
    exempt = await state.guardrails.get_tool_args_exempt(default=list(DEFAULT_TOOL_ARGS_EXEMPT))
    state.policy.reload(
        [rule_from_row(r) for r in rules],
        monitor_only=monitor_only,
        tool_args_exempt=exempt,
    )


class CreateUserBody(BaseModel):
    email: str = Field(pattern=_EMAIL_RE, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""
    is_admin: bool = False
    # Organizational group (optional). Empty/None = ungrouped.
    group_id: str | None = None


class UpdateUserBody(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None
    display_name: str | None = Field(default=None, max_length=255)
    # Admin-set new password (reset). Validated only when present.
    password: str | None = Field(default=None, min_length=8, max_length=128)
    # Group assignment. Sentinel "__unset__" (the default) means "leave as-is";
    # None/"" means "move to ungrouped"; a real id moves to that group.
    group_id: str | None = "__unset__"


def _user_row(user: User, sessions: int, group_names: dict[str, str]) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "role": user.role,
        "signup_method": user.signup_method,
        "sessions": sessions,
        "group_id": user.group_id,
        "group_name": group_names.get(user.group_id) if user.group_id else None,
        "created_at": user.created_at.isoformat(),
    }


async def _group_names(state: AppState) -> dict[str, str]:
    return {g.id: g.name for g in await state.groups.list()}


async def _valid_group_id(state: AppState, group_id: str | None) -> str | None:
    """Normalize an incoming group id: blank → None; otherwise 404 if unknown."""
    if not group_id:
        return None
    names = await _group_names(state)
    if group_id not in names:
        raise HTTPException(status_code=404, detail="group not found")
    return group_id


@router.get("/users")
async def list_users(admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> list:
    users = await state.users.list_all()
    counts = await state.sessions.count_by_user()
    names = await _group_names(state)
    return [_user_row(u, counts.get(u.id, 0), names) for u in users]


@router.post("/users")
async def create_user(
    body: CreateUserBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    if await state.users.get_by_email(body.email) is not None:
        raise HTTPException(status_code=409, detail="email already registered")
    group_id = await _valid_group_id(state, body.group_id)
    user = await state.users.create(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        is_admin=body.is_admin,
        role="admin" if body.is_admin else "user",
        group_id=group_id,
        signup_method="admin_created",
    )
    return _user_row(user, 0, await _group_names(state))


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
    # "__unset__" means the caller didn't touch the group; anything else assigns.
    if body.group_id != "__unset__":
        updated = await state.users.assign_group(
            user_id, await _valid_group_id(state, body.group_id)
        )
    counts = await state.sessions.count_by_user()
    return _user_row(updated, counts.get(updated.id, 0), await _group_names(state))


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


# ---------------------------------------------------------------- user groups
# Groups are organizational only — they carry no policy/permission meaning.

class GroupBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class DefaultGroupBody(BaseModel):
    # The group new self-registered users join. None clears the default.
    group_id: str | None = None


def _group_row(g, counts: dict[str, int]) -> dict:
    return {
        "id": g.id,
        "name": g.name,
        "is_default": g.is_default,
        "user_count": counts.get(g.id, 0),
    }


@router.get("/groups")
async def list_groups(admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> list:
    groups = await state.groups.list()
    counts = await state.groups.counts_by_group()
    return [_group_row(g, counts) for g in groups]


@router.post("/groups")
async def create_group(
    body: GroupBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    if await state.groups.get_by_name(body.name.strip()) is not None:
        raise HTTPException(status_code=409, detail="a group with this name already exists")
    g = await state.groups.create(body.name.strip())
    return _group_row(g, {})


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    ok = await state.groups.delete(group_id)
    if not ok:
        raise HTTPException(status_code=404, detail="group not found")
    return {"deleted": True}


@router.put("/groups/default")
async def set_default_group(
    body: DefaultGroupBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    if body.group_id:
        valid_ids = {g.id for g in await state.groups.list()}
        if body.group_id not in valid_ids:
            raise HTTPException(status_code=404, detail="group not found")
    await state.groups.set_default(body.group_id)
    return {"default_group_id": body.group_id}


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


async def _guardrail_hits_by_user(state: AppState) -> list[dict]:
    """Top users by guardrail-match count (last 14 days), with a display label
    attached — mirrors the token-usage report's id→label pattern."""
    hits = await state.audit.policy_hits_by_user(14)
    labels = await state.users.labels([h["user_id"] for h in hits if h["user_id"]])
    return [
        {"user_id": h["user_id"], "label": labels.get(h["user_id"], h["user_id"] or "(unknown)"), "count": h["count"]}
        for h in hits
    ]


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
        "guardrail_hits_by_user": await _guardrail_hits_by_user(state),
        "guardrail_hits_by_rule": await state.audit.policy_hits_by_rule(14),
    }


# ---------------------------------------------------------------- token usage report

# Default look-back window per granularity, and a hard cap on the span so an
# explicit start/end can't force an unbounded scan.
_GRAN_WINDOW_DAYS = {"daily": 30, "weekly": 84, "monthly": 366, "yearly": 1827}
_TOKENS_TOP_N = 15


# _model_provider_map / list_all_providers / list_all_models are the ONE
# deliberate cross-tenant exception in this admin.py file — every other
# helper here stays owner_id=None (admin-global) scoped. It exists only to
# label/filter Tokens Usage report rows, is always bounded to owners with
# actual usage.record() activity (never a blanket every-account scan), and
# reads provider/model *names* only, never API keys.
async def _model_provider_map(
    state: AppState, owner_ids: Sequence[str]
) -> dict[str | None, dict[str, str]]:
    """(owner_id | None) → {model_id: provider name}, admin-global plus the
    given owners' BYOK config. Kept partitioned by owner rather than folded
    into one flat model_id→provider dict: two different users can each
    configure a different BYOK provider for the exact same model_id, and
    folding them would misattribute one user's usage to another user's
    private provider name."""
    if state.llm_config is None:
        return {}
    providers = await state.llm_config.list_all_providers(owner_ids)
    models = await state.llm_config.list_all_models(owner_ids)
    provider_name = {p.id: p.name for p in providers}
    provider_owner = {p.id: p.owner_id for p in providers}
    mapping: dict[str | None, dict[str, str]] = {}
    for m in models:
        name = provider_name.get(m.provider_id)
        if not name:
            continue
        owner = provider_owner.get(m.provider_id)
        mapping.setdefault(owner, {})[m.model_id] = name
    return mapping


def _provider_of(user_id: str | None, model: str, mapping: dict[str | None, dict[str, str]]) -> str:
    """Resolve model→provider name for a specific user, mirroring
    LLMConfigStore.resolve()'s precedence: a user's own BYOK provider wins
    over the admin-global one for that user's own usage."""
    name = mapping.get(user_id, {}).get(model) or mapping.get(None, {}).get(model)
    if name:
        return name
    prefix = (model or "").split("/", 1)[0]
    return prefix or "(unknown)"


def _fold_by_key(rows: list[dict], key_fn) -> list[dict]:
    """Sum per-(bucket, key_fn(row)) — used to re-aggregate rows onto a
    dimension resolved in Python (provider) or narrowed by one (a provider
    filter applied before re-folding onto user/model)."""
    folded: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = (r["bucket"], key_fn(r))
        acc = folded.setdefault(
            k, {"bucket": r["bucket"], "key": k[1], "prompt_tokens": 0, "completion_tokens": 0, "turns": 0}
        )
        acc["prompt_tokens"] += r["prompt_tokens"]
        acc["completion_tokens"] += r["completion_tokens"]
        acc["turns"] += r["turns"]
    return list(folded.values())


@router.get("/usage/dimensions")
async def usage_dimensions(
    admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    """Filter options for the Tokens Usage report: every model id and a
    representative provider name across every scope (admin-global + every
    active user's BYOK) — names only, never keys or ownership — so
    BYOK-served usage can still be selected/filtered. This just lists
    *available* values (not per-user attribution), so a model_id shared
    across scopes is flattened here with global preferred, BYOK filling any
    gap — real per-user attribution happens in usage_tokens()."""
    owner_ids = await state.usage.distinct_user_ids()
    mapping = await _model_provider_map(state, owner_ids)
    # "models" needs one representative provider per model_id (global
    # preferred, BYOK filling any gap) since it's a flat list of options —
    # but "providers" must NOT go through that same dedup: two different
    # owners' distinctly-named providers both remain valid, independently
    # selectable filter values even when they happen to share a model_id.
    flat: dict[str, str] = {}
    all_provider_names: set[str] = set()
    for owner in sorted(mapping, key=lambda o: (o is not None, o or "")):
        for model_id, name in mapping[owner].items():
            flat.setdefault(model_id, name)
            all_provider_names.add(name)
    models = [{"model_id": m, "provider": p} for m, p in sorted(flat.items())]
    return {"providers": sorted(all_provider_names), "models": models}


@router.get("/usage/tokens")
async def usage_tokens(
    granularity: str = "daily",
    group_by: str = "user",
    user_id: str = "",
    model: str = "",
    provider: str = "",
    start: str = "",
    end: str = "",
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    """Token usage over time, grouped by user, model, or provider, with filters.
    Reads the usage_daily rollup so any range/granularity stays cheap."""
    if granularity not in _GRAN_WINDOW_DAYS:
        granularity = "daily"
    if group_by not in ("user", "model", "provider"):
        group_by = "user"

    # Resolve the date range (span-capped).
    cap = _GRAN_WINDOW_DAYS[granularity]
    today = datetime.now(timezone.utc).date()

    def _parse(s: str) -> date | None:
        try:
            return date.fromisoformat(s) if s else None
        except ValueError:
            return None

    end_d = _parse(end) or today
    start_d = _parse(start) or (end_d - timedelta(days=cap - 1))
    if start_d > end_d:
        start_d = end_d
    if (end_d - start_d).days >= cap:
        start_d = end_d - timedelta(days=cap - 1)

    models: list[str] | None = [model] if model else None

    if group_by == "provider" or provider:
        # A provider name doesn't map to a fixed set of model_ids: the same
        # model_id can belong to a different provider for different BYOK
        # users. So both grouping by provider AND filtering by provider must
        # go through per-(user_id, model) rows, resolve each row's provider
        # individually, and only THEN fold/filter — never via a pre-query
        # model-id list, which can't distinguish "model X under provider A"
        # from "model X under provider B" and would leak/misattribute across
        # users. Bounded to owners with usage in this exact range, not every
        # registered account.
        owner_ids = await state.usage.distinct_user_ids(start_d, end_d)
        mapping = await _model_provider_map(state, owner_ids)
        raw = await state.usage.token_series_by_user_model(
            granularity=granularity, start=start_d, end=end_d, user_id=user_id or None, models=models
        )
        resolved = [
            {**r, "provider": _provider_of(r["user_id"] or None, r["model"], mapping)} for r in raw
        ]
        if provider:
            resolved = [r for r in resolved if r["provider"] == provider]
        key_fn = (
            (lambda r: r["provider"])
            if group_by == "provider"
            else (lambda r: r["user_id"]) if group_by == "user" else (lambda r: r["model"])
        )
        rows = _fold_by_key(resolved, key_fn)
    else:
        group_col = "user_id" if group_by == "user" else "model"
        rows = await state.usage.token_series(
            granularity=granularity,
            start=start_d,
            end=end_d,
            group_col=group_col,
            user_id=user_id or None,
            models=models,
        )

    # Labels: users need id→name; models/providers are self-labelling.
    labels: dict[str, str] = {}
    if group_by == "user":
        labels = await state.users.labels(sorted({r["key"] for r in rows}))

    return _shape_token_series(rows, granularity, group_by, labels)


def _shape_token_series(
    rows: list[dict], granularity: str, group_by: str, labels: dict[str, str]
) -> dict:
    """Pivot flat (bucket, key, tokens) rows into a top-N series + totals for the
    chart, rolling the long tail into a single 'others' entry."""
    buckets = sorted({r["bucket"] for r in rows})
    per_key: dict[str, dict] = {}
    for r in rows:
        e = per_key.setdefault(
            r["key"],
            {"key": r["key"], "prompt_tokens": 0, "completion_tokens": 0, "turns": 0, "points": {}},
        )
        e["prompt_tokens"] += r["prompt_tokens"]
        e["completion_tokens"] += r["completion_tokens"]
        e["turns"] += r["turns"]
        e["points"][r["bucket"]] = {
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "turns": r["turns"],
        }

    ranked = sorted(per_key.values(), key=lambda e: e["prompt_tokens"] + e["completion_tokens"], reverse=True)
    top = ranked[:_TOKENS_TOP_N]
    tail = ranked[_TOKENS_TOP_N:]

    def _entry(e: dict, label: str) -> dict:
        return {
            "key": e["key"],
            "label": label,
            "prompt_tokens": e["prompt_tokens"],
            "completion_tokens": e["completion_tokens"],
            "turns": e["turns"],
            "points": [
                {"bucket": b, **e["points"].get(b, {"prompt_tokens": 0, "completion_tokens": 0, "turns": 0})}
                for b in buckets
            ],
        }

    series = [_entry(e, labels.get(e["key"], e["key"])) for e in top]
    if tail:
        merged = {"key": "__others__", "prompt_tokens": 0, "completion_tokens": 0, "turns": 0, "points": {}}
        for e in tail:
            merged["prompt_tokens"] += e["prompt_tokens"]
            merged["completion_tokens"] += e["completion_tokens"]
            merged["turns"] += e["turns"]
            for b, pt in e["points"].items():
                m = merged["points"].setdefault(b, {"prompt_tokens": 0, "completion_tokens": 0, "turns": 0})
                m["prompt_tokens"] += pt["prompt_tokens"]
                m["completion_tokens"] += pt["completion_tokens"]
                m["turns"] += pt["turns"]
        series.append(_entry(merged, f"Others ({len(tail)})"))

    totals = {
        "prompt_tokens": sum(e["prompt_tokens"] for e in per_key.values()),
        "completion_tokens": sum(e["completion_tokens"] for e in per_key.values()),
        "turns": sum(e["turns"] for e in per_key.values()),
    }
    return {"granularity": granularity, "group_by": group_by, "buckets": buckets, "series": series, "totals": totals}


# ---------------------------------------------------------------- LLM providers/models
# These admin routes manage admin-global providers (owner_id=None). The identical
# per-user "My Models" (BYOK) routes live in claw/api/manage.py and call the same
# shared handlers with the caller's id — see claw/api/llm_shared.py.


@router.get("/llm")
async def list_llm(admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    return await llm.list_llm(state, owner_id=None)


@router.post("/providers")
async def create_provider(
    body: llm.ProviderBody,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    return await llm.create_provider(state, body, owner_id=None)


@router.patch("/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    body: llm.ProviderPatch,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    return await llm.update_provider(state, provider_id, body, owner_id=None)


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    return await llm.delete_provider(state, provider_id, owner_id=None)


@router.post("/providers/{provider_id}/models")
async def create_model(
    provider_id: str,
    body: llm.ModelBody,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    return await llm.create_model(state, provider_id, body, owner_id=None)


@router.patch("/models/{model_pk}")
async def update_model(
    model_pk: str,
    body: llm.ModelPatch,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    return await llm.update_model(state, model_pk, body, owner_id=None)


@router.delete("/models/{model_pk}")
async def delete_model(
    model_pk: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    return await llm.delete_model(state, model_pk, owner_id=None)


# ---------------------------------------------------------------- guardrails

class MonitorBody(BaseModel):
    monitor_only: bool
    # Optional: replace the tool-args exemption list (tool-name globs). Omitted =
    # leave unchanged. Each entry is a short glob like "mcp_outlook_*".
    tool_args_exempt: list[str] | None = Field(default=None, max_length=100)


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
    exempt = await state.guardrails.get_tool_args_exempt(default=list(DEFAULT_TOOL_ARGS_EXEMPT))
    return {
        "monitor_only": monitor_only,
        "tool_args_exempt": exempt,
        "rules": [_rule_row(r) for r in rules],
    }


@router.put("/guardrails")
async def set_guardrails(
    body: MonitorBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    await state.guardrails.set_monitor_only(body.monitor_only)
    if body.tool_args_exempt is not None:
        # Normalize: trim, drop blanks, dedupe, bound length.
        cleaned = list(dict.fromkeys(g.strip()[:100] for g in body.tool_args_exempt if g.strip()))
        await state.guardrails.set_tool_args_exempt(cleaned)
    await _reload_policy(state)
    exempt = await state.guardrails.get_tool_args_exempt(default=list(DEFAULT_TOOL_ARGS_EXEMPT))
    return {"monitor_only": body.monitor_only, "tool_args_exempt": exempt}


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
