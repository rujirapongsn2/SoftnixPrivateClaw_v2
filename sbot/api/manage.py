"""Management API: skills, memory, connectors, schedules — all in-chat, no separate control plane."""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sbot.api import connector_shared as connectors
from sbot.api import llm_shared as llm
from sbot.api.deps import AppState, current_user, get_state, require_admin
from sbot.core.memory import MAX_CORE_MEMORY_CHARS, sanitize_core_document
from sbot.core.mission_engine import InvalidGraphError
from sbot.core.scheduler import compute_next_run
from sbot.db.models import User

router = APIRouter(prefix="/api")

# Personal resources are managed by their owner (any authenticated user).
# Only the global control policy is system-wide → admin.
require_operator = current_user


# ---------------------------------------------------------------------- Bots
class CreateBotBody(BaseModel):
    name: str
    role_title: str = "Specialist"
    charter: str = ""
    model: str | None = None
    tool_allowlist: list[str] | None = None
    skill_ids: list[str] | None = None
    kind: str = "specialist"
    avatar: dict | None = None


class UpdateBotBody(BaseModel):
    name: str | None = None
    role_title: str | None = None
    charter: str | None = None
    model: str | None = None
    tool_allowlist: list[str] | None = None
    skill_ids: list[str] | None = None
    avatar: dict | None = None


# UpdateBotBody fields whose column is nullable, so a null in the request body
# is a real value ("clear this") rather than an absent field. Must stay in step
# with the Bot model's nullable=True columns.
_NULLABLE_BOT_FIELDS = frozenset({"model", "tool_allowlist", "skill_ids", "avatar"})


@router.get("/bots")
async def list_bots(
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> list:
    await state.bots.get_or_create_cos(user.id)
    bots = await state.bots.list_for_user(user.id)
    return [
        {
            "id": b.id,
            "name": b.name,
            "role_title": b.role_title,
            "charter": b.charter,
            "model": b.model,
            "tool_allowlist": b.tool_allowlist,
            "skill_ids": b.skill_ids,
            "kind": b.kind,
            "avatar": b.avatar,
            "created_by": b.created_by,
            "stats": b.stats,
            "created_at": b.created_at.isoformat(),
        }
        for b in bots
    ]


@router.post("/bots")
async def create_bot(
    body: CreateBotBody,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    await state.bots.get_or_create_cos(user.id)
    try:
        bot = await state.bots.create(
            owner_id=user.id,
            name=body.name.strip(),
            role_title=body.role_title.strip() or "Specialist",
            charter=body.charter.strip(),
            model=body.model,
            tool_allowlist=body.tool_allowlist,
            skill_ids=body.skill_ids,
            kind=body.kind,
            avatar=body.avatar,
            created_by="user",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": bot.id, "name": bot.name, "role_title": bot.role_title}


@router.get("/bots/{bot_id}")
async def get_bot(
    bot_id: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    bot = await state.bots.get(bot_id, user.id)
    if bot is None or bot.is_archived:
        raise HTTPException(status_code=404, detail="Bot not found")
    return {
        "id": bot.id,
        "name": bot.name,
        "role_title": bot.role_title,
        "charter": bot.charter,
        "model": bot.model,
        "tool_allowlist": bot.tool_allowlist,
        "skill_ids": bot.skill_ids,
        "kind": bot.kind,
        "avatar": bot.avatar,
        "created_by": bot.created_by,
        "stats": bot.stats,
        "created_at": bot.created_at.isoformat(),
    }


@router.patch("/bots/{bot_id}")
async def update_bot(
    bot_id: str,
    body: UpdateBotBody,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    bot = await state.bots.get(bot_id, user.id)
    if bot is None or bot.is_archived:
        raise HTTPException(status_code=404, detail="Bot not found")
    # exclude_unset so an explicit null is honoured instead of reading as "not
    # sent": null is the only way to put tool_allowlist or skill_ids back to
    # unrestricted, and since the allowlist is enforced on every turn, dropping
    # it made restricting a bot a one-way door. `[]` is a different instruction
    # ("no tools at all"), so neither value can stand in for the other. A null
    # for a column that cannot hold one is a client bug, not an instruction.
    updates = {
        k: v
        for k, v in body.model_dump(exclude_unset=True).items()
        if v is not None or k in _NULLABLE_BOT_FIELDS
    }
    updated = await state.bots.update(bot_id, user.id, **updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return {"id": updated.id, "name": updated.name, "role_title": updated.role_title}


@router.delete("/bots/{bot_id}")
async def delete_bot(
    bot_id: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    bot = await state.bots.get(bot_id, user.id)
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    if bot.kind == "chief_of_staff":
        raise HTTPException(status_code=400, detail="Cannot delete Chief of Staff")
    await state.bots.archive(bot_id, user.id)
    return {"deleted": True}


# ------------------------------------------------------------------ missions
class PlanMissionBody(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    nodes: list[dict]
    session_id: str | None = None
    budget: dict | None = None


@router.get("/missions")
async def list_missions(
    status: str | None = None,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> list:
    rows = await state.missions.list_missions(user.id, status=status)
    return [
        {
            "id": m.id,
            "goal": m.goal,
            "status": m.status,
            "session_id": m.session_id,
            "spent": m.spent or {},
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in rows
    ]


@router.get("/missions/active")
async def list_active_missions(
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> list:
    """In-flight missions, shaped for the UI rather than for a report.

    A mission runs in a detached task, so nothing on the chat socket ever
    mentions it: a specialist working inside one looked completely idle in the
    sidebar while Chief of Staff was telling the user it was busy. This closes
    that gap, and it is a poll rather than an event because it has to stay right
    across a reload and whichever thread happens to be open.
    """
    active = await state.missions.active_missions(user.id)
    if not active:
        return []
    names = {b.id: b.name for b in await state.bots.list_for_user(user.id)}
    phases = {}
    for mission, nodes in active:
        for node in nodes:
            if node.status == 'running':
                activity = await state.missions.blackboard_read(mission.id, f'scope:activity:{node.id}')
                phases[(mission.id, node.id)] = (activity or {}).get('phase', 'running')
    return [
        {
            "id": mission.id,
            "goal": mission.goal,
            "status": mission.status,
            "session_id": mission.session_id,
            # Counted over the nodes that exist now, so a replan that adds steps
            # moves the denominator — which is the honest one, not the plan's.
            "total": len(nodes),
            "done": sum(1 for n in nodes if n.status in ("done", "skipped")),
            "running": [
                {
                    "node_id": n.id,
                    "title": n.title,
                    "bot_id": n.bot_id or "",
                    "bot_name": names.get(n.bot_id or "", ""),
                }
                for n in nodes
                if n.status == "running" and phases.get((mission.id, n.id)) != 'queued'
            ],
            "queued": [
                {"node_id": n.id, "title": n.title, "bot_id": n.bot_id or "",
                 "bot_name": names.get(n.bot_id or "", ""), "depends_on": n.depends_on or []}
                for n in nodes if n.status in ('pending', 'ready') or (
                    n.status == 'running' and phases.get((mission.id, n.id)) == 'queued')
            ],
            "awaiting": [
                {"node_id": n.id, "title": n.title} for n in nodes if n.status == "awaiting_human"
            ],
        }
        for mission, nodes in active
    ]


@router.post("/missions")
async def plan_mission(
    body: PlanMissionBody,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    try:
        mission = await state.mission_service.plan(
            user.id, body.goal, body.nodes, session_id=body.session_id, budget=body.budget
        )
    except InvalidGraphError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": mission.id, "status": mission.status}


@router.get("/missions/{mission_id}")
async def get_mission(
    mission_id: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    status = await state.mission_service.status(mission_id, user.id)
    if status is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return status


@router.post("/missions/{mission_id}/start")
async def start_mission(
    mission_id: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    result = await state.mission_service.start(mission_id, user.id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="Mission not found")
    return {"status": result}


class GateDecisionBody(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=2000)


@router.post("/missions/{mission_id}/gates/{node_id}")
async def resolve_mission_gate(
    mission_id: str,
    node_id: str,
    body: GateDecisionBody,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """Approve or decline a step parked for review, and let the mission continue.

    The authoritative path for a gate decision: it carries the caller's own
    session, so the approval is the user's act. The `mission_gate` tool exists
    for the chat flow, but there a model is relaying what the user said.
    """
    try:
        status = await state.mission_service.resolve_gate(
            mission_id, user.id, node_id, body.approved, note=body.note
        )
    except InvalidGraphError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"approved": body.approved, "status": status}


@router.post("/missions/{mission_id}/cancel")
async def cancel_mission(
    mission_id: str,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    if not await state.mission_service.cancel(mission_id, user.id):
        raise HTTPException(status_code=404, detail="Mission not found")
    return {"status": "cancelled"}


# ---------------------------------------------------------------- skills

class SkillBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9ก-๙_\- ]+$")
    description: str = Field(default="", max_length=500)
    content: str = ""
    enabled: bool = True
    visibility: Literal["private", "group", "public"] | None = None
    id: str | None = None
    # The MCP connector this skill's instructions rely on, if any — lets the
    # runtime resolve that connector's CURRENT tool names live every turn
    # instead of the skill text hardcoding a connector name that can later be
    # renamed. null = no linked connector.
    connector_id: str | None = None


class SkillSubscriptionBody(BaseModel):
    enabled: bool


def _skill_json(
    s, builtin: bool = False, shadows_builtin: bool = False, with_content: bool = True, viewer_id: str | None = None, owner_name: str = ""
) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "content": s.content if with_content else "",
        "enabled": s.enabled,
        "connector_id": getattr(s, "connector_id", None),
        "updated_at": s.updated_at.isoformat(),
        "builtin": builtin,
        "visibility": getattr(s, "visibility", "private"),
        "owner_name": owner_name,
        "read_only": builtin or (viewer_id is not None and s.user_id != viewer_id),
        "subscription_enabled": getattr(s, "subscription_enabled", s.enabled),
        # Only BuiltinSkill instances carry these (the "CAPABILITIES COVERED"
        # detail view) — user/ORM skills never set them, hence the getattr.
        "capabilities": [{"title": t, "description": d} for t, d in getattr(s, "capabilities", ())],
        "summary": getattr(s, "summary", "") or "",
        # True when this user skill's name matches a built-in's, so it's
        # hiding that built-in from the list below — surfaced in the UI so
        # the collision isn't silent.
        "shadows_builtin": shadows_builtin,
    }


@router.get("/skills")
async def list_skills(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> list:
    from sbot.core.builtin_skills import builtin_skills

    user_skills = await state.skills.available_for_user(user.id)
    owners = await state.users.display_names(list({s.user_id for s in user_skills}))
    user_names = {s.name for s in user_skills}
    builtin_names = {b.name for b in builtin_skills()}
    # Built-ins first (read-only), skipping any a user skill shadows by name.
    #
    # Their `content` is deliberately left out. It is static, identical for every
    # user, and already ~63 KB across the built-ins (one is 34 KB on its own),
    # while this list renders only name/description/summary — so sending it here
    # ships the whole corpus on every panel open and grows with each built-in
    # added. The detail view fetches the one it needs from /skills/{id}/content.
    # User skills keep theirs inline: they are small, and the edit form and the
    # enable/disable Switch both round-trip the full object straight back to PUT.
    builtins = [
        _skill_json(b, builtin=True, with_content=False)
        for b in builtin_skills()
        if b.name not in user_names
    ]
    return builtins + [
        _skill_json(s, shadows_builtin=s.name in builtin_names, viewer_id=user.id, owner_name=owners.get(s.user_id, "")) for s in user_skills
    ]


@router.get("/skills/{skill_id}/content")
async def skill_content(skill_id: str, _: User = Depends(require_operator)) -> dict:
    """Full instructions for one built-in skill, kept out of the list payload.

    Built-ins only: user skills already carry their content in the list, since
    the edit form needs it in hand to submit an unchanged copy back."""
    from sbot.core.builtin_skills import get_builtin_skill

    prefix = "builtin:"
    skill = get_builtin_skill(skill_id[len(prefix) :]) if skill_id.startswith(prefix) else None
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return {"content": skill.content}


@router.put("/skills/{skill_id}/subscription")
async def set_skill_subscription(
    skill_id: str,
    body: SkillSubscriptionBody,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    if skill_id.startswith("builtin:") or not await state.skills.set_subscription(user.id, skill_id, body.enabled):
        raise HTTPException(status_code=404, detail="shared skill not found")
    return {"enabled": body.enabled}


@router.put("/skills/{name}")
async def upsert_skill(
    name: str,
    body: SkillBody,
    user: User = Depends(require_operator),
    state: AppState = Depends(get_state),
) -> dict:
    from sbot.core.builtin_skills import get_builtin_skill

    if get_builtin_skill(name.strip()) is not None:
        raise HTTPException(status_code=400, detail="that name is reserved by a built-in skill")
    if body.id:
        owned_skills = await state.skills.list_for_user(user.id)
        if not any(s.id == body.id and s.name == name.strip() for s in owned_skills):
            raise HTTPException(status_code=403, detail="Only the owner can edit this skill")
    if body.visibility == "group" and not user.group_id:
        raise HTTPException(status_code=400, detail="Join a group before sharing a skill with your group")
    if body.connector_id is not None:
        # A skill may link either the caller's own connector or an
        # admin-global one ("Pre-built Connectors") — the latter has no
        # owner_id == user.id row, so it's invisible to list_for_user alone.
        owned = await state.connectors.list_for_user(user.id)
        global_ones = await state.connectors.list_for_global()
        if not any(c.id == body.connector_id for c in (*owned, *global_ones)):
            raise HTTPException(status_code=404, detail="connector not found")
    skill = await state.skills.upsert(
        user.id,
        name.strip(),
        description=body.description,
        content=body.content,
        enabled=body.enabled,
        visibility=body.visibility,
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
    # The manual editor writes the same document consolidation does, and it is
    # replayed into every later system prompt — so it clears the same bar. Users
    # paste "notes" straight off web pages; without this, text that remember()
    # would reject lands verbatim in every future prompt, permanently.
    if len(body.content) > MAX_CORE_MEMORY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"memory is limited to {MAX_CORE_MEMORY_CHARS} characters",
        )
    content, dropped = sanitize_core_document(body.content)
    await state.memories.set_core(user.id, content)
    return {"core": content, "dropped": dropped}


# ---------------------------------------------------------------- connectors
# Personal connectors (owner_id=<uid>). The identical admin-global "Pre-built
# Connectors" routes (owner_id=None) live in claw/api/admin.py and call the
# same shared handlers — see claw/api/connector_shared.py.


@router.get("/connectors")
async def list_connectors(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> list:
    return await connectors.list_connectors(state, owner_id=user.id)


@router.get("/connectors/presets")
async def connector_presets(user: User = Depends(current_user)) -> list:
    from sbot.core.connector_presets import list_presets

    return list_presets()


@router.get("/connectors/global")
async def list_global_connectors(
    user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> list:
    """Read-only, redacted view of admin-global connectors ("Provided by your
    organization") — for Settings > Connectors' transparency panel and for
    skill authors who need the exact mcp_{connector}_{tool} names. Never
    includes command/url/env: unlike connector_row's callers (always the
    owner), the caller here is never the owner of a global connector."""
    await state.connectors_mgr.sync_global()
    statuses = await state.connectors_mgr.status_global()
    return [
        connectors.connector_global_summary(c, statuses.get(c.name))
        for c in await state.connectors.list_for_global()
    ]


@router.get("/groups")
async def list_groups(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> list:
    """Org-wide group names — lets a regular user pick "share with additional
    groups" for a knowledge base. Deliberately thinner than the admin
    `/admin/groups` payload (no user_count/plan_id — not this endpoint's business)."""
    return [{"id": g.id, "name": g.name} for g in await state.groups.list()]


@router.put("/connectors/{name}")
async def upsert_connector(
    name: str,
    body: connectors.ConnectorBody,
    user: User = Depends(require_operator),
    state: AppState = Depends(get_state),
) -> dict:
    return await connectors.upsert_connector(
        state, name, body, owner_id=user.id, allow_arbitrary_stdio=user.is_admin
    )


@router.delete("/connectors/{connector_id}")
async def delete_connector(
    connector_id: str, user: User = Depends(require_operator), state: AppState = Depends(get_state)
) -> dict:
    return await connectors.delete_connector(state, connector_id, owner_id=user.id)


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
    keeps working out of the box — but only when that env default actually has
    usable credentials (api_key or api_base); otherwise it would show a model
    the runtime (claw/core/runtime.py) can never use, since AgentRuntime rejects
    the turn with error.no_model_configured in that case.
    """
    plan = await state.plans.resolve_for_user(user.id) if state.plans is not None else None
    chat_cost = plan["max_chat_cost"] if plan else None
    models = await state.llm_config.enabled_models(user.id, max_cost=chat_cost)
    default = await state.llm_config.default_model_for(chat_cost)
    if not models:
        llm = state.settings.llm
        if not (llm.api_key or llm.api_base):
            return {"models": [], "default": None}
        env_model = llm.model
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
    return {"interval_minutes": user.sbot_heartbeat_interval_seconds // 60,
            "enabled": user.sbot_heartbeat_interval_seconds > 0,
            "next_run_at": user.sbot_heartbeat_next_at.isoformat() if user.sbot_heartbeat_next_at else None}

@router.put("/heartbeat")
async def set_heartbeat(body: HeartbeatBody, user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    from datetime import datetime, timedelta, timezone
    from claw.modes import SbotHeartbeatUsers
    seconds = body.interval_minutes * 60
    next_at = datetime.now(timezone.utc) + timedelta(seconds=seconds) if seconds else None
    await SbotHeartbeatUsers(state.users).set_heartbeat(user.id, seconds, next_at)
    return {"interval_minutes": body.interval_minutes, "enabled": seconds > 0, "next_run_at": next_at.isoformat() if next_at else None}
