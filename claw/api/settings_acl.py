"""Settings-menu ACL: shared logic for the admin API in both modes.

Hiding a menu is a UI policy — it does not block the underlying user APIs."""

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

# Must match SettingsSection in web/src/Settings.tsx. "profile" is locked
# visible: it holds password change, and hiding it could strand a user.
SETTINGS_SECTION_KEYS = [
    "profile", "projects", "blueprints", "skills", "knowledge", "memory",
    "models", "connectors", "schedules", "heartbeat", "telegram", "browser-extension",
]
LOCKED_SECTIONS = {"profile"}


class SettingsAclBody(BaseModel):
    scope: Literal["global", "group", "user"]
    target_id: str | None = None
    # section -> visible. Sections omitted inherit from the next scope up.
    rules: dict[str, bool] = Field(default_factory=dict)


async def acl_overview(state) -> dict:
    data = await state.settings_acl.get()
    group_names = {g.id: g.name for g in await state.groups.list()}
    users = []
    for uid, rules in data["users"].items():
        u = await state.users.get(uid)  # bounded by the number of overrides
        if u is not None:
            users.append({"id": uid, "label": u.display_name or u.email, "email": u.email, "rules": rules})
    groups = [
        {"id": gid, "label": group_names[gid], "rules": rules}
        for gid, rules in data["groups"].items()
        if gid in group_names
    ]
    return {"sections": SETTINGS_SECTION_KEYS, "locked": sorted(LOCKED_SECTIONS),
            "global": data["global"], "groups": groups, "users": users}


async def acl_update(state, body: SettingsAclBody) -> dict:
    unknown = set(body.rules) - set(SETTINGS_SECTION_KEYS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown sections: {sorted(unknown)}")
    if any(k in LOCKED_SECTIONS and v is False for k, v in body.rules.items()):
        raise HTTPException(status_code=422, detail="the profile menu cannot be hidden")
    if body.scope == "group":
        if not body.target_id or body.target_id not in {g.id for g in await state.groups.list()}:
            raise HTTPException(status_code=404, detail="group not found")
    elif body.scope == "user":
        if not body.target_id or await state.users.get(body.target_id) is None:
            raise HTTPException(status_code=404, detail="user not found")
    await state.settings_acl.set_scope(body.scope, body.target_id, body.rules)
    return await acl_overview(state)


async def acl_for_user(state, user) -> dict:
    hidden = await state.settings_acl.hidden_for(user.id, user.group_id)
    return {"hidden": [k for k in hidden if k not in LOCKED_SECTIONS]}


async def acl_search_users(state, q: str) -> list[dict]:
    """Bounded (10) user lookup for the ACL target picker — never lists everyone."""
    from sqlalchemy import or_, select

    from claw.db.models import User

    escaped = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    term = f"%{escaped}%"
    if len(q.strip()) < 2:
        return []
    async with state.users.factory() as db:
        rows = await db.scalars(
            select(User)
            .where(or_(User.email.ilike(term, escape="\\"), User.display_name.ilike(term, escape="\\")))
            .order_by(User.email)
            .limit(10)
        )
        return [{"id": u.id, "label": u.display_name or u.email, "email": u.email} for u in rows]
