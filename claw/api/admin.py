"""System administration API — manage all users. Requires the is_admin flag."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from claw.api.deps import AppState, get_state, require_admin
from claw.auth.passwords import hash_password
from claw.db.models import User

router = APIRouter(prefix="/api/admin")

_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class CreateUserBody(BaseModel):
    email: str = Field(pattern=_EMAIL_RE, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""
    is_admin: bool = False


class UpdateUserBody(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None


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
    updated = await state.users.update_flags(
        user_id, is_admin=body.is_admin, is_active=body.is_active
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")
    counts = await state.sessions.count_by_user()
    return _user_row(updated, counts.get(updated.id, 0))


@router.get("/stats")
async def stats(admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    users = await state.users.list_all()
    tokens = await state.usage.totals()
    feedback = await state.feedback.totals()
    return {
        "users": len(users),
        "admins": sum(1 for u in users if u.is_admin),
        "suspended": sum(1 for u in users if not u.is_active),
        "sessions": await state.sessions.total(),
        "messages": await state.messages.total(),
        "prompt_tokens": tokens["prompt_tokens"],
        "completion_tokens": tokens["completion_tokens"],
        "feedback_up": feedback["up"],
        "feedback_down": feedback["down"],
        "policy_enforcing": not state.policy.monitor_only,
        "browser_enabled": state.settings.browser.enabled,
        "telegram_enabled": bool(state.settings.telegram_bot_token),
    }
