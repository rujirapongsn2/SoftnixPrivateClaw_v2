from types import SimpleNamespace

from sbot.sandbox.project_admin import ProjectContainerManager


class ConfigStore:
    def __init__(self):
        self.enabled = None

    async def get(self, default_enabled=False):
        return {"enabled": default_enabled if self.enabled is None else self.enabled}

    async def set_enabled(self, enabled):
        self.enabled = enabled


async def test_enabling_missing_image_starts_background_build(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        enabled=True,
        projects_enabled=False,
        project_image="sbot-developer:latest",
    )
    store = ConfigStore()
    manager = ProjectContainerManager(settings, store, source_root=tmp_path)
    builds = []

    async def available():
        return True

    async def missing():
        return False

    async def start_build():
        builds.append(True)
        return await manager.status()

    monkeypatch.setattr(manager, "_docker_ready", available)
    monkeypatch.setattr(manager, "_image_ready", missing)
    monkeypatch.setattr(manager, "start_build", start_build)

    status = await manager.set_enabled(True)

    assert settings.projects_enabled is True
    assert store.enabled is True
    assert builds == [True]
    assert status["enabled"] is True
    assert status["ready"] is False


async def test_status_is_ready_only_when_enabled_docker_and_image_are_ready(monkeypatch, tmp_path):
    settings = SimpleNamespace(enabled=True, projects_enabled=True, project_image="developer:test")
    manager = ProjectContainerManager(settings, ConfigStore(), source_root=tmp_path)

    async def available():
        return True

    monkeypatch.setattr(manager, "_docker_ready", available)
    monkeypatch.setattr(manager, "_image_ready", available)

    status = await manager.status()

    assert status["ready"] is True
    assert status["image"] == "developer:test"
