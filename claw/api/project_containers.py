"""Control Plane endpoints for project-container readiness and image builds."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from claw.api.deps import AppState, get_state, require_admin
from claw.db.models import User

router = APIRouter(prefix="/api/admin/project-containers")


class ProjectContainersBody(BaseModel):
    enabled: bool


def _unavailable() -> dict:
    return {
        "mode_available": False,
        "enabled": False,
        "docker_available": False,
        "image_available": False,
        "image": "sbot-developer:latest",
        "building": False,
        "build_error": "",
        "build_started_at": None,
        "ready": False,
        "metrics_available": False,
        "containers": {"running": 0, "stopped": 0, "total": 0},
        "cpu_percent": 0.0,
        "memory_usage_bytes": 0,
        "memory_limit_bytes": 0,
        "disk_usage_bytes": 0,
        "disk_usage_complete": True,
    }


@router.get("")
async def project_container_status(
    admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    manager = state.project_containers
    if manager is None:
        return _unavailable()
    return {"mode_available": True, **await manager.status()}


@router.put("")
async def set_project_containers(
    body: ProjectContainersBody,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    manager = state.project_containers
    if manager is None:
        raise HTTPException(status_code=409, detail="Bot Mode is disabled")
    result = await manager.set_enabled(body.enabled)
    await state.audit.log(
        "admin",
        {"event": "project_containers_updated", "enabled": body.enabled, "by": admin.id},
        user_id=admin.id,
    )
    return {"mode_available": True, **result}


@router.post("/build")
async def build_project_container_image(
    admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    manager = state.project_containers
    if manager is None:
        raise HTTPException(status_code=409, detail="Bot Mode is disabled")
    result = await manager.start_build()
    await state.audit.log(
        "admin",
        {"event": "project_container_image_build_requested", "image": result["image"], "by": admin.id},
        user_id=admin.id,
    )
    return {"mode_available": True, **result}
