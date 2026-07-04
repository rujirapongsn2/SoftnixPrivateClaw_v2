"""Per-user Telegram account linking API."""

from fastapi import APIRouter, Depends, HTTPException

from claw.api.deps import AppState, current_user, get_state
from claw.db.models import User

router = APIRouter(prefix="/api/telegram")


def _bot_username(state: AppState) -> str:
    return getattr(state.telegram, "bot_username", "") if state.telegram is not None else ""


@router.get("/status")
async def status(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    return {
        "enabled": state.telegram is not None,
        "linked": bool(user.telegram_user_id),
        "bot_username": _bot_username(state),
    }


@router.post("/link")
async def create_link(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    if state.telegram is None:
        raise HTTPException(status_code=400, detail="Telegram is not enabled on this server")
    code = state.telegram_link.create(user.id)
    return {
        "code": code,
        "expires_in": state.telegram_link.ttl_seconds,
        "bot_username": _bot_username(state),
    }


@router.delete("/link")
async def unlink(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    await state.users.set_telegram_id(user.id, None)
    return {"linked": False}
