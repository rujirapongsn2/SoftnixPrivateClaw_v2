"""Scope-aware LLM provider/model management — one implementation for both the
admin-global Control Plane and per-user "bring your own key" (BYOK) providers.

Every handler takes an ``owner_id``: ``None`` operates on admin-global providers
(Control Plane), a user id operates on that user's own private providers. The
admin routes (claw/api/admin.py) call these with ``owner_id=None``; the user
routes (claw/api/manage.py) call them with the caller's id. This keeps provider
management single-sourced — a fix here applies to both scopes at once.
"""

from fastapi import HTTPException
from pydantic import BaseModel, Field

from sbot.api.deps import AppState

_PREFIX_RE = r"^[a-z0-9_]*$"
_COST_RE = r"^(low|medium|high|very_high)$"
_KIND_RE = r"^(chat|image|vision)$"
# Sanity bound on the manual context-window override: far above any real model,
# low enough that a typo can't hand the agent loop an absurd compaction ceiling.
_MAX_CONTEXT_WINDOW = 20_000_000


class ProviderBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    api_key: str = ""
    api_base: str = ""
    enabled: bool = True
    auto_disable_models: bool = False
    # LiteLLM routing prefix (e.g. "openai", "openrouter") applied automatically
    # to every model id added under this provider — see LLMProvider.model_prefix.
    model_prefix: str = Field(default="", max_length=32, pattern=_PREFIX_RE)


class ProviderPatch(BaseModel):
    name: str | None = None
    api_key: str | None = None  # empty/None keeps the existing key
    api_base: str | None = None
    enabled: bool | None = None
    auto_disable_models: bool | None = None
    model_prefix: str | None = Field(default=None, max_length=32, pattern=_PREFIX_RE)


class ModelBody(BaseModel):
    model_id: str = Field(min_length=1, max_length=128)
    label: str = ""
    enabled: bool = True
    cost: str = Field(default="medium", pattern=_COST_RE)
    description: str = ""
    # "chat" = agent chat picker; "image" = text-to-image only (kept out of
    # the chat picker, offered via the separate image-generation path);
    # "vision" = the reader a chat turn delegates an attached image to when the
    # selected chat model can't accept one. Only "chat" is user-selectable.
    kind: str = Field(default="chat", pattern=_KIND_RE)
    # Input-token window. 0/None = fall back to LiteLLM's bundled model table.
    context_window: int | None = Field(default=None, ge=0, le=_MAX_CONTEXT_WINDOW)


class ModelPatch(BaseModel):
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    label: str | None = None
    enabled: bool | None = None
    is_default: bool | None = None  # admin-global only; ignored on user scope
    is_fallback: bool | None = None  # admin-global only; ignored on user scope
    cost: str | None = Field(default=None, pattern=_COST_RE)
    description: str | None = None
    kind: str | None = Field(default=None, pattern=_KIND_RE)
    context_window: int | None = Field(default=None, ge=0, le=_MAX_CONTEXT_WINDOW)


def provider_row(p, models: list) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "api_base": p.api_base,
        "has_key": bool(p.api_key),
        "enabled": p.enabled,
        "auto_disable_models": p.auto_disable_models if p.owner_id is None else False,
        "model_prefix": p.model_prefix,
        "models": [model_row(m) for m in models if m.provider_id == p.id],
    }


def model_row(m) -> dict:
    return {
        "id": m.id,
        "model_id": m.model_id,
        "label": m.label or m.model_id,
        "enabled": m.enabled,
        "health_status": m.health_status,
        "health_reason": m.health_reason,
        "health_checked_at": (
            m.health_checked_at.isoformat() + ("Z" if m.health_checked_at.tzinfo is None else "")
            if m.health_checked_at else None
        ),
        "health_auto_disabled": m.health_auto_disabled,
        "is_default": m.is_default,
        "is_fallback": m.is_fallback,
        "cost": m.cost or "medium",
        "description": m.description or "",
        "kind": m.kind or "chat",
        "context_window": m.context_window,
    }


# -- handlers (owner_id=None → admin-global; owner_id=<uid> → that user's own) --


async def list_llm(state: AppState, owner_id: str | None) -> dict:
    providers = await state.llm_config.list_providers(owner_id)
    models = await state.llm_config.list_models(owner_id)
    return {"providers": [provider_row(p, models) for p in providers]}


async def create_provider(state: AppState, body: ProviderBody, owner_id: str | None) -> dict:
    if await state.llm_config.get_by_name(body.name, owner_id) is not None:
        raise HTTPException(status_code=409, detail="a provider with this name already exists")
    p = await state.llm_config.create_provider(
        body.name, body.api_key, body.api_base, body.enabled, body.model_prefix,
        owner_id=owner_id, auto_disable_models=body.auto_disable_models if owner_id is None else False,
    )
    return provider_row(p, [])


async def update_provider(
    state: AppState, provider_id: str, body: ProviderPatch, owner_id: str | None
) -> dict:
    if body.name:
        existing = await state.llm_config.get_by_name(body.name, owner_id)
        if existing is not None and existing.id != provider_id:
            raise HTTPException(status_code=409, detail="a provider with this name already exists")
    p = await state.llm_config.update_provider(provider_id, owner_id, **body.model_dump(exclude_none=True))
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    models = await state.llm_config.list_models(owner_id)
    return provider_row(p, models)


async def delete_provider(state: AppState, provider_id: str, owner_id: str | None) -> dict:
    ok = await state.llm_config.delete_provider(provider_id, owner_id)
    if not ok:
        raise HTTPException(status_code=404, detail="provider not found")
    return {"deleted": True}


async def create_model(state: AppState, provider_id: str, body: ModelBody, owner_id: str | None) -> dict:
    m = await state.llm_config.create_model(
        provider_id,
        body.model_id,
        body.label,
        body.enabled,
        body.cost,
        body.description,
        kind=body.kind,
        owner_id=owner_id,
        context_window=body.context_window,
    )
    if m is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return model_row(m)


async def update_model(state: AppState, model_pk: str, body: ModelPatch, owner_id: str | None) -> dict:
    m = await state.llm_config.update_model(model_pk, owner_id, **body.model_dump(exclude_none=True))
    if m is None:
        raise HTTPException(status_code=404, detail="model not found")
    return model_row(m)


async def delete_model(state: AppState, model_pk: str, owner_id: str | None) -> dict:
    ok = await state.llm_config.delete_model(model_pk, owner_id)
    if not ok:
        raise HTTPException(status_code=404, detail="model not found")
    return {"deleted": True}
