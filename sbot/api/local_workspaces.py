from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sbot.api.deps import current_user, get_state

router = APIRouter(prefix='/api/local-workspaces', tags=['local-workspaces'])


def broker(state):
    return state.runtime.local_workspaces


@router.get('')
async def list_workspaces(user=Depends(current_user), state=Depends(get_state)):
    return broker(state).list(user.id)


@router.post('/pair')
async def pair(user=Depends(current_user), state=Depends(get_state)):
    return broker(state).pair(user.id)


class PairingStatus(BaseModel):
    code: str = Field(min_length=20, max_length=100)


@router.post('/pair/status')
async def pairing_status(body: PairingStatus, user=Depends(current_user), state=Depends(get_state)):
    return broker(state).pairing_status(user.id, body.code)


class Selection(BaseModel):
    workspace_id: str = Field(default='', max_length=64)


async def session_owner(state, user, sid):
    session = await state.sessions.get(sid)
    if not session or session.user_id != user.id:
        raise HTTPException(404, 'Chat not found')


@router.get('/sessions/{sid}')
async def selected(sid: str, user=Depends(current_user), state=Depends(get_state)):
    await session_owner(state, user, sid)
    return {'workspace_id': broker(state).selected(user.id, sid)}


@router.put('/sessions/{sid}')
async def select(sid: str, body: Selection, user=Depends(current_user), state=Depends(get_state)):
    await session_owner(state, user, sid)
    if state.runtime._session_lock(sid).locked():
        raise HTTPException(409, 'Wait for the current turn before switching workspace')
    try:
        broker(state).select(user.id, sid, body.workspace_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {'workspace_id': body.workspace_id}


@router.get('/agent.py')
async def download_agent():
    return FileResponse(Path(__file__).parents[1]/'local_agent.py', filename='sbot_local_agent.py', media_type='text/x-python')


@router.get('/downloads/{platform}')
async def download_binary(platform: str):
    if platform not in ('macos-arm64', 'macos-x86_64'):
        raise HTTPException(404, 'Unsupported platform')
    name = f'softnix-local-agent-{platform}.zip'
    path = Path(__file__).parents[2] / 'dist' / 'local-agent' / name
    if not path.is_file():
        raise HTTPException(404, 'This platform build is not available yet')
    return FileResponse(path, filename=name, media_type='application/zip')


class Redeem(BaseModel):
    code: str = Field(min_length=20, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    writable: bool = False
    previous_token: str | None = Field(default=None, min_length=20, max_length=100)
    path: str | None = Field(default=None, max_length=4096)


@router.post('/redeem')
async def redeem(body: Redeem, state=Depends(get_state)):
    try:
        return broker(state).redeem(body.code, body.name, body.writable, body.previous_token, body.path)
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc


async def device(request: Request, state=Depends(get_state)):
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer '):
        raise HTTPException(401, 'Device token required')
    try:
        row = broker(state).authenticate(header[7:])
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    user = await state.users.get(row['owner'])
    if not user or not user.is_active:
        raise HTTPException(403, 'Account disabled')
    return row


@router.post('/poll')
async def poll(request: Request, row=Depends(device), state=Depends(get_state)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    path = body.get('path') if isinstance(body, dict) else None
    return {'job': broker(state).poll(row['id'], path)}


@router.post('/validate')
async def validate(row=Depends(device)):
    return {'id': row['id']}


@router.post('/result')
async def result(request: Request, row=Depends(device), state=Depends(get_state)):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 12*1024*1024:
            raise HTTPException(413, 'Result too large')
    import json
    try:
        body = json.loads(data)
        if not isinstance(body['id'], str) or not isinstance(body['result'], dict):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, 'Invalid result')
    return {'accepted': broker(state).result(row['id'], body['id'], body['result'])}


@router.delete('/{wid}')
async def revoke(wid: str, user=Depends(current_user), state=Depends(get_state)):
    try:
        broker(state).revoke(user.id, wid)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {'revoked': True}
