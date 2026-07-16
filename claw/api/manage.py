"""Management API: skills, memory, connectors, schedules — all in-chat, no separate control plane."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from claw.api import llm_shared as llm
from claw.api.deps import AppState, current_user, get_state, require_admin
from claw.core.scheduler import compute_next_run
from claw.db.models import User

router = APIRouter(prefix="/api")

# Personal resources are managed by their owner (any authenticated user).
# Only the global control policy is system-wide → admin.
require_operator = current_user


# ---------------------------------------------------------------- skills

class SkillBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9ก-๙_\- ]+$")
    description: str = Field(default="", max_length=500)
    content: str = ""
    enabled: bool = True
    # The MCP connector this skill's instructions rely on, if any — lets the
    # runtime resolve that connector's CURRENT tool names live every turn
    # instead of the skill text hardcoding a connector name that can later be
    # renamed. null = no linked connector.
    connector_id: str | None = None


def _skill_json(s, builtin: bool = False) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "content": s.content,
        "enabled": s.enabled,
        "connector_id": getattr(s, "connector_id", None),
        "updated_at": s.updated_at.isoformat(),
        "builtin": builtin,
    }


@router.get("/skills")
async def list_skills(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> list:
    from claw.core.builtin_skills import builtin_skills

    user_skills = await state.skills.list_for_user(user.id)
    user_names = {s.name for s in user_skills}
    # Built-ins first (read-only), skipping any a user skill shadows by name.
    builtins = [_skill_json(b, builtin=True) for b in builtin_skills() if b.name not in user_names]
    return builtins + [_skill_json(s) for s in user_skills]


@router.put("/skills/{name}")
async def upsert_skill(
    name: str,
    body: SkillBody,
    user: User = Depends(require_operator),
    state: AppState = Depends(get_state),
) -> dict:
    from claw.core.builtin_skills import get_builtin_skill

    if get_builtin_skill(name.strip()) is not None:
        raise HTTPException(status_code=400, detail="that name is reserved by a built-in skill")
    if body.connector_id is not None:
        owned = await state.connectors.list_for_user(user.id)
        if not any(c.id == body.connector_id for c in owned):
            raise HTTPException(status_code=404, detail="connector not found")
    skill = await state.skills.upsert(
        user.id,
        name.strip(),
        description=body.description,
        content=body.content,
        enabled=body.enabled,
        connector_id=body.connector_id,
    )
    return _skill_json(skill)


@router.delete("/skills/{skill_id}")
async def delete_skill(
    skill_id: str, user: User = Depends(require_operator), state: AppState = Depends(get_state)
) -> dict:
    if skill_id.startswith("builtin:"):
        raise HTTPException(status_code=400, detail="built-in skills cannot be deleted")
    if not await state.skills.delete(user.id, skill_id):
        raise HTTPException(status_code=404, detail="skill not found")
    return {"deleted": True}


# ---------------------------------------------------------------- memory

class MemoryBody(BaseModel):
    content: str


@router.get("/memory")
async def get_memory(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    return {
        "core": await state.memories.get_core(user.id),
        "history": await state.memories.recent_history(user.id, limit=50),
    }


@router.put("/memory")
async def update_memory(
    body: MemoryBody, user: User = Depends(require_operator), state: AppState = Depends(get_state)
) -> dict:
    await state.memories.set_core(user.id, body.content)
    return {"core": body.content}


# ---------------------------------------------------------------- connectors

class ConnectorBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_\-]+$")
    transport: str = Field(default="stdio", pattern=r"^(stdio|http)$")
    command: str = ""
    url: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


def _connector_json(c, status: dict | None = None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "transport": c.transport,
        "command": c.command,
        "url": c.url,
        "env": c.env or {},
        "enabled": c.enabled,
        "runtime": status or {"status": "not_connected"},
    }


@router.get("/connectors")
async def list_connectors(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> list:
    # Warm-connect enabled connectors so their runtime status is live here (the
    # composer menu only shows "connected" ones) instead of only after a chat turn.
    if state.runtime is not None:
        await state.runtime.warm_connectors(user.id)
    statuses = await state.connectors_mgr.status(user.id)
    return [_connector_json(c, statuses.get(c.name)) for c in await state.connectors.list_for_user(user.id)]


@router.get("/connectors/presets")
async def connector_presets(user: User = Depends(current_user)) -> list:
    from claw.core.connector_presets import list_presets

    return list_presets()


@router.get("/groups")
async def list_groups(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> list:
    """Org-wide group names — lets a regular user pick "share with additional
    groups" for a knowledge base. Deliberately thinner than the admin
    `/admin/groups` payload (no user_count/plan_id — not this endpoint's business)."""
    return [{"id": g.id, "name": g.name} for g in await state.groups.list()]


@router.put("/connectors/{name}")
async def upsert_connector(
    name: str,
    body: ConnectorBody,
    user: User = Depends(require_operator),
    state: AppState = Depends(get_state),
) -> dict:
    if body.transport == "stdio" and not body.command.strip():
        raise HTTPException(status_code=422, detail="stdio connector requires a command")
    if body.transport == "http" and not body.url.strip():
        raise HTTPException(status_code=422, detail="http connector requires a url")
    row = await state.connectors.upsert(
        user.id,
        name.strip(),
        transport=body.transport,
        command=body.command,
        url=body.url,
        env=body.env,
        enabled=body.enabled,
    )
    await state.connectors_mgr.invalidate(user.id)
    return _connector_json(row)


@router.delete("/connectors/{connector_id}")
async def delete_connector(
    connector_id: str, user: User = Depends(require_operator), state: AppState = Depends(get_state)
) -> dict:
    if not await state.connectors.delete(user.id, connector_id):
        raise HTTPException(status_code=404, detail="connector not found")
    await state.connectors_mgr.invalidate(user.id)
    return {"deleted": True}


# ---------------------------------------------------------------- schedules

class ScheduleBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1)
    cron: str = ""
    interval_seconds: int = Field(default=0, ge=0)
    run_at: datetime | None = None  # one-shot
    session_id: str | None = None
    enabled: bool = True


def _schedule_json(s) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "prompt": s.prompt,
        "cron": s.cron,
        "interval_seconds": s.interval_seconds,
        "session_id": s.session_id,
        "enabled": s.enabled,
        "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "last_status": s.last_status,
    }


def _initial_next_run(body: ScheduleBody, tz: str = "UTC") -> datetime:
    if body.run_at is not None:
        return body.run_at
    try:
        next_run = compute_next_run(body.cron, body.interval_seconds, tz=tz)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if next_run is None:
        raise HTTPException(
            status_code=422, detail="provide cron, interval_seconds, or run_at"
        )
    return next_run


@router.get("/schedules")
async def list_schedules(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> list:
    return [_schedule_json(s) for s in await state.schedules.list_for_user(user.id)]


@router.post("/schedules")
async def create_schedule(
    body: ScheduleBody, user: User = Depends(require_operator), state: AppState = Depends(get_state)
) -> dict:
    if body.session_id and (
        (owned := await state.sessions.get(body.session_id)) is None or owned.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="target session not found")
    row = await state.schedules.create(
        user.id,
        name=body.name,
        prompt=body.prompt,
        cron=body.cron,
        interval_seconds=body.interval_seconds,
        session_id=body.session_id,
        enabled=body.enabled,
        next_run_at=_initial_next_run(body, state.settings.scheduler.timezone),
    )
    state.scheduler.notify_changed()
    return _schedule_json(row)


@router.put("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    body: ScheduleBody,
    user: User = Depends(require_operator),
    state: AppState = Depends(get_state),
) -> dict:
    row = await state.schedules.update(
        user.id,
        schedule_id,
        name=body.name,
        prompt=body.prompt,
        cron=body.cron,
        interval_seconds=body.interval_seconds,
        session_id=body.session_id,
        enabled=body.enabled,
        next_run_at=_initial_next_run(body, state.settings.scheduler.timezone) if body.enabled else None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    state.scheduler.notify_changed()
    return _schedule_json(row)


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str, user: User = Depends(require_operator), state: AppState = Depends(get_state)
) -> dict:
    if not await state.schedules.delete(user.id, schedule_id):
        raise HTTPException(status_code=404, detail="schedule not found")
    state.scheduler.notify_changed()
    return {"deleted": True}


@router.post("/schedules/{schedule_id}/run")
async def run_schedule_now(
    schedule_id: str, user: User = Depends(require_operator), state: AppState = Depends(get_state)
) -> dict:
    row = await state.schedules.get(schedule_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="schedule not found")
    updated = await state.schedules.update(
        user.id, schedule_id, next_run_at=datetime.now(timezone.utc), enabled=True
    )
    state.scheduler.notify_changed()
    return _schedule_json(updated)


# ---------------------------------------------------------------- control policy

class PolicyToggle(BaseModel):
    monitor_only: bool


@router.get("/policy")
async def get_policy(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    return {
        "monitor_only": state.policy.monitor_only,
        "rules": [
            {
                "name": r.name,
                "action": r.action,
                "scopes": list(r.scopes),
                "severity": r.severity,
                "enabled": r.enabled,
            }
            for r in state.policy.rules
        ],
    }


@router.put("/policy")
async def set_policy(
    body: PolicyToggle, user: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    state.policy.monitor_only = body.monitor_only
    return {"monitor_only": state.policy.monitor_only}


# ---------------------------------------------------------------- heartbeat (per user)

class HeartbeatBody(BaseModel):
    # 0 disables the proactive check-in.
    interval_minutes: int = Field(ge=0, le=1440)


@router.get("/usage")
async def get_usage(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    return await state.usage.totals_for_user(user.id)


# ---------------------------------------------------------------- models (chat picker)

@router.get("/models")
async def list_models(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    """Enabled models for the chat model picker, plus the effective default.

    Filtered by the user's usage-plan cost ceiling (BYOK models exempt). Falls
    back to the env-configured model when no providers are set up, so chat
    keeps working out of the box.
    """
    plan = await state.plans.resolve_for_user(user.id) if state.plans is not None else None
    chat_cost = plan["max_chat_cost"] if plan else None
    models = await state.llm_config.enabled_models(user.id, max_cost=chat_cost)
    default = await state.llm_config.default_model_for(chat_cost)
    if not models:
        env_model = state.settings.llm.model
        return {
            "models": [
                {
                    "model_id": env_model,
                    "label": env_model,
                    "provider": "default",
                    "is_default": True,
                    "cost": "medium",
                    "description": "Default configured model.",
                }
            ],
            "default": env_model,
        }
    if not default:
        default = models[0]["model_id"]
    return {"models": models, "default": default}


@router.get("/image-models")
async def list_image_models(
    user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    """Enabled text-to-image models for the composer's "+ Image" picker —
    kept separate from the chat picker (these can't do tool calling). When the
    plan disallows image generation, admin-global models are hidden but the
    caller's own BYOK image models still show (same exemption as the cost
    ceiling below — it's their own key, not the operator's)."""
    plan = await state.plans.resolve_for_user(user.id) if state.plans is not None else None
    image_cost = plan["max_image_cost"] if plan else None
    models = await state.llm_config.enabled_models(user.id, kind="image", max_cost=image_cost)
    if plan is not None and not plan["allow_image"]:
        models = [m for m in models if m["scope"] == "private"]
    return {"models": models}


@router.get("/my/plan")
async def my_plan(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    """The caller's effective usage plan + today's consumption/remaining, for
    the composer's quota hint. Null plan = no restriction."""
    plan = await state.plans.resolve_for_user(user.id) if state.plans is not None else None
    today = await state.usage.usage_today(user.id) if state.usage is not None else {"turns": 0, "images": 0}
    if plan is None:
        return {"plan": None, "used": today}
    return {
        "plan": plan,
        "used": today,
        "messages_remaining": (
            max(0, plan["messages_per_day"] - today["turns"]) if plan["messages_per_day"] else None
        ),
        "images_remaining": (
            max(0, plan["images_per_day"] - today["images"]) if plan["images_per_day"] else None
        ),
    }


# ---------------------------------------------------------------- my LLM providers (BYOK)
# Users manage their own private providers/models here. These call the SAME shared
# handlers as the admin Control Plane routes (claw/api/admin.py), scoped to the
# caller via owner_id=user.id — so provider management stays single-sourced. There
# is no "set default" on this scope: the auto-selected default is admin-global only.


@router.get("/my/llm")
async def my_list_llm(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    return await llm.list_llm(state, owner_id=user.id)


@router.post("/my/providers")
async def my_create_provider(
    body: llm.ProviderBody, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    return await llm.create_provider(state, body, owner_id=user.id)


@router.patch("/my/providers/{provider_id}")
async def my_update_provider(
    provider_id: str,
    body: llm.ProviderPatch,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    return await llm.update_provider(state, provider_id, body, owner_id=user.id)


@router.delete("/my/providers/{provider_id}")
async def my_delete_provider(
    provider_id: str, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    return await llm.delete_provider(state, provider_id, owner_id=user.id)


@router.post("/my/providers/{provider_id}/models")
async def my_create_model(
    provider_id: str,
    body: llm.ModelBody,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    return await llm.create_model(state, provider_id, body, owner_id=user.id)


@router.patch("/my/models/{model_pk}")
async def my_update_model(
    model_pk: str,
    body: llm.ModelPatch,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    return await llm.update_model(state, model_pk, body, owner_id=user.id)


@router.delete("/my/models/{model_pk}")
async def my_delete_model(
    model_pk: str, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    return await llm.delete_model(state, model_pk, owner_id=user.id)


# ---------------------------------------------------------------- feedback (self-learning signal)

class FeedbackBody(BaseModel):
    signal: str = Field(pattern=r"^(up|down)$")
    session_id: str | None = None
    note: str = Field(default="", max_length=2000)
    message_preview: str = Field(default="", max_length=500)


@router.post("/feedback")
async def submit_feedback(
    body: FeedbackBody, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    if body.session_id:
        session = await state.sessions.get(body.session_id)
        if session is None or session.user_id != user.id:
            raise HTTPException(status_code=404, detail="session not found")
    await state.feedback.record(
        user.id, body.session_id, body.signal, body.note, body.message_preview
    )
    return {"recorded": True}


@router.get("/feedback/stats")
async def feedback_stats(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    return await state.feedback.totals_for_user(user.id)


@router.get("/heartbeat")
async def get_heartbeat(user: User = Depends(current_user)) -> dict:
    return {
        "interval_minutes": (user.heartbeat_interval_seconds or 0) // 60,
        "enabled": (user.heartbeat_interval_seconds or 0) > 0,
        "next_run_at": user.heartbeat_next_at.isoformat() if user.heartbeat_next_at else None,
    }


@router.put("/heartbeat")
async def set_heartbeat(
    body: HeartbeatBody, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    from datetime import datetime, timedelta, timezone

    seconds = body.interval_minutes * 60
    next_at = datetime.now(timezone.utc) + timedelta(seconds=seconds) if seconds > 0 else None
    await state.users.set_heartbeat(user.id, seconds, next_at)
    return {
        "interval_minutes": body.interval_minutes,
        "enabled": seconds > 0,
        "next_run_at": next_at.isoformat() if next_at else None,
    }
