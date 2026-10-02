"""Workspace storage policy (quota + retention), edited in the Control Plane.

Its own router, always mounted: with Sbot mode on, `/api/admin` is served by
the Sbot admin router, so an endpoint added only to `claw.api.admin` would
vanish from the production app.
"""

from fastapi import APIRouter, Depends

from claw.api.deps import AppState, get_state, require_admin
from claw.db.models import User
from claw.workspace.policy import WorkspacePolicy

router = APIRouter(prefix="/api/admin/workspace-policy")


@router.get("")
async def get_workspace_policy(state: AppState = Depends(get_state), admin: User = Depends(require_admin)) -> dict:
    """The effective policy, with the environment defaults it falls back to."""
    return {
        **(await state.workspace_policy.effective()).model_dump(),
        "defaults": state.workspace_policy.defaults().model_dump(),
    }


@router.put("")
async def put_workspace_policy(
    body: WorkspacePolicy, state: AppState = Depends(get_state), admin: User = Depends(require_admin)
) -> dict:
    saved = await state.workspace_policy.save(body, admin.id)
    await state.audit.log("workspace_policy_change", saved.model_dump(), user_id=admin.id)
    return {**saved.model_dump(), "defaults": state.workspace_policy.defaults().model_dump()}
