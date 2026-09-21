"""Read/control API shared by both modes; submission is runtime-owned."""
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from claw.api.deps import current_user, get_state

router = APIRouter(prefix='/api/jobs', tags=['jobs'])


def store_for(state):
    store = getattr(state, 'jobs', None)
    if store is None:
        raise HTTPException(503, 'job service unavailable')
    return store


@router.get('')
async def list_jobs(session_id: str | None = None, user=Depends(current_user), state=Depends(get_state)):
    return await store_for(state).list_jobs(user.id, session_id)


@router.get('/{job_id}')
async def get_job(job_id: str, user=Depends(current_user), state=Depends(get_state)):
    value = await store_for(state).snapshot(user.id, job_id)
    if value is None:
        raise HTTPException(404, 'job not found')
    return value


@router.get('/{job_id}/events')
async def events(job_id: str, after: int = Query(default=0, ge=0),
                 user=Depends(current_user), state=Depends(get_state)):
    value = await store_for(state).events(user.id, job_id, after)
    if value is None:
        raise HTTPException(404, 'job not found')
    return value


@router.post('/{job_id}/{action}')
async def control(job_id: str, action: str, user=Depends(current_user), state=Depends(get_state)):
    store = store_for(state)
    if action not in {'cancel', 'resume'}:
        raise HTTPException(404, 'unknown action')
    if await store.snapshot(user.id, job_id) is None:
        raise HTTPException(404, 'job not found')
    if not await store.control(job_id, user.id, action):
        raise HTTPException(409, 'job cannot perform this action in its current state')
    return await store.snapshot(user.id, job_id)




class ApprovalDecision(BaseModel):
    key: str
    approved: bool


@router.post('/{job_id}/steps/{step_id}/approval')
async def approval(job_id: str, step_id: str, body: ApprovalDecision,
                   user=Depends(current_user), state=Depends(get_state)):
    if not await store_for(state).approve(user.id, job_id, step_id, body.key, body.approved):
        raise HTTPException(409, 'approval is unavailable or already resolved')
    return await store_for(state).snapshot(user.id, job_id)
