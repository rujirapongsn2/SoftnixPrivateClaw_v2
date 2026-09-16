from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sbot.api.routes import _project_inventory, start_project


class AccessPolicy:
    async def resolve(self, _user_id):
        return SimpleNamespace(allowed=True, source="group", max_containers=1)


class ProjectRuntime:
    def __init__(self):
        self.started = False

    async def list(self, _workspace):
        return []

    async def execute(self, *_args, **_kwargs):
        self.started = True
        return "ok"


class Readiness:
    def __init__(self, ready: bool, runtime_state: str):
        self.value = {"ready": ready, "runtime_state": runtime_state}

    async def readiness(self):
        return self.value


def state(tmp_path: Path, *, ready: bool, runtime_state: str):
    projects = ProjectRuntime()
    return SimpleNamespace(
        project_access=AccessPolicy(),
        project_containers=Readiness(ready, runtime_state),
        settings=SimpleNamespace(
            sandbox=SimpleNamespace(
                enabled=True,
                projects_enabled=True,
                project_host_bind_ip="127.0.0.1",
            ),
            workspaces_root=tmp_path,
        ),
        runtime=SimpleNamespace(sandbox=SimpleNamespace(projects=projects)),
    )


@pytest.mark.asyncio
async def test_inventory_exposes_building_readiness_without_starting_a_project(tmp_path):
    app_state = state(tmp_path, ready=False, runtime_state="building")
    user = SimpleNamespace(id="user-1")

    inventory = await _project_inventory(app_state, user)

    assert inventory["ready"] is False
    assert inventory["runtime_state"] == "building"
    assert app_state.runtime.sandbox.projects.started is False


@pytest.mark.asyncio
async def test_start_route_rejects_an_unready_runtime(tmp_path):
    app_state = state(tmp_path, ready=False, runtime_state="error")
    user = SimpleNamespace(id="user-1")

    with pytest.raises(HTTPException) as rejected:
        await start_project("demo", user, app_state)

    assert rejected.value.status_code == 409
    assert "build failed" in rejected.value.detail
    assert app_state.runtime.sandbox.projects.started is False


@pytest.mark.asyncio
async def test_start_route_runs_when_runtime_is_ready(tmp_path):
    app_state = state(tmp_path, ready=True, runtime_state="ready")
    user = SimpleNamespace(id="user-1")

    inventory = await start_project("demo", user, app_state)

    assert inventory["ready"] is True
    assert app_state.runtime.sandbox.projects.started is True
