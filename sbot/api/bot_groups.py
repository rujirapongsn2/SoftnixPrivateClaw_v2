"""Group Bot API; all membership and conversation access is owner scoped."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sbot.api.deps import AppState, current_user, get_state
from sbot.db.bot_groups import BotGroupStore
from sbot.db.models import User

router = APIRouter(prefix='/api/bot-groups', tags=['bot-groups'])


class GroupBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    member_ids: list[str] = Field(min_length=2, max_length=12)
    leader_id: str


def payload(group):
    return {key: getattr(group, key) for key in ('id', 'name', 'member_ids', 'leader_id', 'session_id')}


@router.get('')
async def list_groups(user: Annotated[User, Depends(current_user)], state: Annotated[AppState, Depends(get_state)]):
    groups = await BotGroupStore(state.sessions.factory).list_for_user(user.id)
    return [payload(group) for group in groups]


@router.post('', status_code=201)
async def create_group(body: GroupBody, user: Annotated[User, Depends(current_user)], state: Annotated[AppState, Depends(get_state)]):
    try:
        group = await BotGroupStore(state.sessions.factory).save(user.id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return payload(group)


@router.patch('/{group_id}')
async def update_group(group_id: str, body: GroupBody,
                       user: Annotated[User, Depends(current_user)], state: Annotated[AppState, Depends(get_state)]):
    try:
        group = await BotGroupStore(state.sessions.factory).save(user.id, group_id=group_id, **body.model_dump())
    except LookupError as exc:
        raise HTTPException(404, 'Group not found') from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return payload(group)


@router.delete('/{group_id}')
async def delete_group(group_id: str, user: Annotated[User, Depends(current_user)], state: Annotated[AppState, Depends(get_state)]):
    group = await BotGroupStore(state.sessions.factory).get(group_id, user.id)
    if group is None:
        raise HTTPException(404, 'Group not found')
    if group.session_id in state.runtime.active_sessions():
        raise HTTPException(409, 'Wait for the group to finish before deleting it')
    await state.sessions.delete(group.session_id)
    return {'ok': True}
