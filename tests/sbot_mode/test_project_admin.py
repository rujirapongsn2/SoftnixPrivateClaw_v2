from types import SimpleNamespace

import json

from sbot.sandbox.project_admin import ProjectContainerManager, _size_bytes


EMPTY_METRICS = {
    "metrics_available": False,
    "containers": {"running": 0, "stopped": 0, "total": 0},
    "cpu_percent": 0.0,
    "memory_usage_bytes": 0,
    "memory_limit_bytes": 0,
    "disk_usage_bytes": 0,
    "disk_usage_complete": True,
}


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
    monkeypatch.setattr(manager, "_runtime_metrics", lambda: _async_value(EMPTY_METRICS))

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
    monkeypatch.setattr(manager, "_runtime_metrics", lambda: _async_value(EMPTY_METRICS))

    status = await manager.status()

    assert status["ready"] is True
    assert status["image"] == "developer:test"


async def _async_value(value):
    return value


def test_size_bytes_supports_docker_decimal_and_binary_units():
    assert _size_bytes("1.5GiB") == 1_610_612_736
    assert _size_bytes("250MB") == 250_000_000
    assert _size_bytes("") == 0


async def test_runtime_metrics_aggregate_managed_containers(monkeypatch, tmp_path):
    settings = SimpleNamespace(enabled=True, projects_enabled=True, project_image="developer:test")
    manager = ProjectContainerManager(settings, ConfigStore(), source_root=tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.bin").write_bytes(b"x" * 1024)

    ps_output = "\n".join(
        [
            json.dumps({"Names": "project-running", "State": "running"}),
            json.dumps({"Names": "project-stopped", "State": "exited"}),
        ]
    )
    stats_output = json.dumps({"CPUPerc": "12.5%", "MemUsage": "256MiB / 4GiB"})
    inspect_output = json.dumps(
        [
            {"SizeRw": 512, "Mounts": [{"Type": "bind", "Source": str(workspace), "Destination": "/workspace"}]},
            {"SizeRw": 256, "Mounts": []},
        ]
    )

    async def run(*argv, timeout=20, output_cap=20_000):
        if argv[1] == "ps":
            return 0, ps_output
        if argv[1] == "stats":
            return 0, stats_output
        if argv[1:3] == ("container", "inspect"):
            return 0, inspect_output
        raise AssertionError(argv)

    monkeypatch.setattr(manager, "_run", run)
    metrics = await manager._collect_runtime_metrics()

    assert metrics["containers"] == {"running": 1, "stopped": 1, "total": 2}
    assert metrics["metrics_available"] is True
    assert metrics["cpu_percent"] == 12.5
    assert metrics["memory_usage_bytes"] == 256 * 1024**2
    assert metrics["memory_limit_bytes"] == 4 * 1024**3
    assert metrics["disk_usage_bytes"] >= 768
    assert metrics["disk_usage_complete"] is True
