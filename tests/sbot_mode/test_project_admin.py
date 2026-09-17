import ipaddress
import json
from types import SimpleNamespace

import pytest

from sbot.sandbox.project_admin import ProjectContainerManager, _host_private_ipv4_addresses, _size_bytes


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

    async def get(
        self, default_enabled=False, default_public_ingress_enabled=False,
        default_host_bind_ip="127.0.0.1",
    ):
        return {
            "enabled": default_enabled if self.enabled is None else self.enabled,
            "public_ingress_enabled": default_public_ingress_enabled,
            "host_bind_ip": default_host_bind_ip,
        }

    async def set_enabled(self, enabled):
        self.enabled = enabled

    async def set_public_ingress_enabled(self, enabled):
        self.public_ingress_enabled = enabled

    async def set_host_bind_ip(self, host_bind_ip):
        self.host_bind_ip = host_bind_ip


async def test_internal_access_accepts_private_ipv4_and_rejects_public_address(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        enabled=True, projects_enabled=True, project_image="developer:test",
        project_host_bind_ip="127.0.0.1", project_ports=[3000, 8000],
    )
    store = ConfigStore()
    manager = ProjectContainerManager(settings, store, source_root=tmp_path)
    monkeypatch.setattr(manager, "_docker_ready", lambda: _async_value(False))
    monkeypatch.setattr(
        "sbot.sandbox.project_admin._host_private_ipv4_addresses",
        lambda: ["127.0.0.1", "192.168.1.24"],
    )

    status = await manager.set_host_bind_ip("192.168.1.24")

    assert store.host_bind_ip == "192.168.1.24"
    assert status["host_bind_ip"] == "192.168.1.24"
    assert status["available_host_bind_ips"] == ["127.0.0.1", "192.168.1.24"]
    assert status["access_scope"] == "lan"
    with pytest.raises(ValueError, match="private or loopback"):
        await manager.set_host_bind_ip("8.8.8.8")
    with pytest.raises(ValueError, match="assigned to this host"):
        await manager.set_host_bind_ip("192.168.1.25")


def test_host_private_ipv4_addresses_starts_with_loopback():
    addresses = _host_private_ipv4_addresses()

    assert addresses[0] == "127.0.0.1"
    assert all(
        (address := ipaddress.ip_address(value)).version == 4
        and (address.is_private or address.is_loopback)
        for value in addresses
    )


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


async def test_readiness_probe_is_cached_without_collecting_metrics(monkeypatch, tmp_path):
    settings = SimpleNamespace(enabled=True, projects_enabled=True, project_image="developer:test")
    manager = ProjectContainerManager(settings, ConfigStore(), source_root=tmp_path)
    calls = {"docker": 0, "image": 0}

    async def docker_available():
        calls["docker"] += 1
        return True

    async def image_available():
        calls["image"] += 1
        return True

    monkeypatch.setattr(manager, "_docker_ready", docker_available)
    monkeypatch.setattr(manager, "_image_ready", image_available)

    first = await manager.readiness()
    second = await manager.readiness()

    assert first["runtime_state"] == "ready"
    assert second["ready"] is True
    assert calls == {"docker": 1, "image": 1}


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
