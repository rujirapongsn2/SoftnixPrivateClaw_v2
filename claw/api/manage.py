"""Management API: skills, memory, connectors, schedules — all in-chat, no separate control plane."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

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


def _skill_json(s) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "content": s.content,
        "enabled": s.enabled,
        "updated_at": s.updated_at.isoformat(),
    }


@router.get("/skills")
async def list_skills(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> list:
    return [_skill_json(s) for s in await state.skills.list_for_user(user.id)]


@router.put("/skills/{name}")
async def upsert_skill(
    name: str,
    body: SkillBody,
    user: User = Depends(require_operator),
    state: AppState = Depends(get_state),
) -> dict:
    skill = await state.skills.upsert(
        user.id, name.strip(), description=body.description, content=body.content, enabled=body.enabled
    )
    return _skill_json(skill)


@router.delete("/skills/{skill_id}")
async def delete_skill(
    skill_id: str, user: User = Depends(require_operator), state: AppState = Depends(get_state)
) -> dict:
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
    statuses = await state.connectors_mgr.status(user.id)
    return [_connector_json(c, statuses.get(c.name)) for c in await state.connectors.list_for_user(user.id)]


@router.get("/connectors/presets")
async def connector_presets(user: User = Depends(current_user)) -> list:
    from claw.core.connector_presets import list_presets

    return list_presets()


@router.put("/connectors/{name}")
async def upsert_connector(
    name: str,
    body: ConnectorBody,
    user: User = Depends(require_admin),
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
    connector_id: str, user: User = Depends(require_admin), state: AppState = Depends(get_state)
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


def _initial_next_run(body: ScheduleBody) -> datetime:
    if body.run_at is not None:
        return body.run_at
    try:
        next_run = compute_next_run(body.cron, body.interval_seconds)
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
        next_run_at=_initial_next_run(body),
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
        next_run_at=_initial_next_run(body) if body.enabled else None,
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
