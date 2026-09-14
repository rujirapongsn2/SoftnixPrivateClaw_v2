import asyncio
from types import SimpleNamespace
import uuid

import httpx

from claw.api.project_ingress import ProjectIngressMiddleware
from claw.db.stores import ProjectContainerConfigStore
from sbot.config import SandboxSettings
from sbot.sandbox.project_ingress import (
    project_ingress_host,
    project_ingress_url,
    resolve_project_ingress_host,
)
from sbot.sandbox.project_admin import ProjectContainerManager


def test_signed_project_host_resolves_only_for_its_tenant_and_project(tmp_path):
    settings = SandboxSettings(project_ingress_domain="apps.example.com")
    alice = uuid.uuid4().hex
    bob = uuid.uuid4().hex
    (tmp_path / alice / "projects" / "dashboard").mkdir(parents=True)
    (tmp_path / bob / "projects" / "dashboard").mkdir(parents=True)

    host = project_ingress_host(settings, "secret", alice, "dashboard")

    assert host is not None
    assert resolve_project_ingress_host(host, settings, "secret", tmp_path) == (alice, "dashboard")
    assert resolve_project_ingress_host(host, settings, "wrong-secret", tmp_path) is None
    assert project_ingress_host(settings, "secret", bob, "dashboard") != host
    assert project_ingress_url(settings, "secret", alice, "dashboard") == f"https://{host}"


def test_project_host_rejects_tampering_and_unconfigured_domain(tmp_path):
    owner = uuid.uuid4().hex
    (tmp_path / owner / "projects" / "demo").mkdir(parents=True)
    settings = SandboxSettings(project_ingress_domain="apps.example.com")
    host = project_ingress_host(settings, "secret", owner, "demo")
    assert host is not None

    assert resolve_project_ingress_host(f"x{host}", settings, "secret", tmp_path) is None
    assert resolve_project_ingress_host(host.replace(".apps.", ".other."), settings, "secret", tmp_path) is None
    assert project_ingress_url(SandboxSettings(), "secret", owner, "demo") is None


class _ConfigStore:
    def __init__(self):
        self.value = {"enabled": True, "public_ingress_enabled": False}

    async def get(self, **defaults):
        return self.value

    async def set_public_ingress_enabled(self, enabled):
        self.value["public_ingress_enabled"] = enabled


async def test_public_ingress_toggle_requires_domain_and_persists(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        enabled=True, projects_enabled=True, project_image="developer:test",
        project_ingress_domain="", project_public_ingress_enabled=False,
        project_ingress_scheme="https", project_ingress_port=8000,
    )
    store = _ConfigStore()
    manager = ProjectContainerManager(settings, store, source_root=tmp_path)

    async def unavailable():
        return False

    monkeypatch.setattr(manager, "_docker_ready", unavailable)
    try:
        await manager.set_public_ingress_enabled(True)
        assert False, "expected missing domain to be rejected"
    except ValueError:
        pass

    settings.project_ingress_domain = "apps.example.com"
    status = await manager.set_public_ingress_enabled(True)
    assert store.value["public_ingress_enabled"] is True
    assert status["public_ingress_enabled"] is True


async def test_http_ingress_proxies_signed_host_without_exposing_an_upstream_choice(monkeypatch, tmp_path):
    owner = uuid.uuid4().hex
    (tmp_path / owner / "projects" / "demo").mkdir(parents=True)
    settings = SandboxSettings(
        projects_enabled=True,
        project_public_ingress_enabled=True,
        project_ingress_domain="apps.example.com",
    )
    upstream_requests = []
    real_client = httpx.AsyncClient

    class ResponseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"project-ok"

    def upstream(request):
        upstream_requests.append(request)
        return httpx.Response(200, stream=ResponseStream())

    def proxy_client(**kwargs):
        return real_client(transport=httpx.MockTransport(upstream), **kwargs)

    monkeypatch.setattr("claw.api.project_ingress.httpx.AsyncClient", proxy_client)

    class Projects:
        async def proxy_target(self, workspace, project, ingress_port):
            assert workspace == tmp_path / owner
            assert (project, ingress_port) == ("demo", 8000)
            return "http://project.internal:8000"

    async def fallback(scope, receive, send):
        raise AssertionError("signed project host should not reach the control-plane app")

    middleware = ProjectIngressMiddleware(
        fallback, settings=settings, projects=Projects(), workspaces_root=tmp_path, secret_key="secret"
    )
    host = project_ingress_host(settings, "secret", owner, "demo")
    async with real_client(transport=httpx.ASGITransport(app=middleware), base_url="http://test") as client:
        response = await client.get("/health?full=1", headers={"host": host})

    assert response.text == "project-ok"
    assert str(upstream_requests[0].url) == "http://project.internal:8000/health?full=1"
    assert upstream_requests[0].headers["host"] == host
    assert upstream_requests[0].headers["x-forwarded-host"] == host
    assert upstream_requests[0].headers["x-forwarded-proto"] == "https"
    await middleware.client.aclose()


async def test_ingress_global_switch_and_stopped_project_fail_closed(tmp_path):
    owner = uuid.uuid4().hex
    (tmp_path / owner / "projects" / "demo").mkdir(parents=True)
    settings = SandboxSettings(
        projects_enabled=True,
        project_public_ingress_enabled=False,
        project_ingress_domain="apps.example.com",
    )
    calls = []

    class Projects:
        async def proxy_target(self, *args):
            calls.append(args)
            return None

    async def fallback(scope, receive, send):
        raise AssertionError("project ingress host must fail closed")

    middleware = ProjectIngressMiddleware(
        fallback, settings=settings, projects=Projects(), workspaces_root=tmp_path, secret_key="secret"
    )
    host = project_ingress_host(settings, "secret", owner, "demo")
    transport = httpx.ASGITransport(app=middleware)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", headers={"host": host})
        assert response.status_code == 404
        assert calls == []

        settings.project_public_ingress_enabled = True
        response = await client.get("/", headers={"host": host})
        assert response.status_code == 503
        assert len(calls) == 1
    await middleware.client.aclose()


async def test_websocket_preserves_public_host_and_bounds_messages(monkeypatch, tmp_path):
    owner = uuid.uuid4().hex
    (tmp_path / owner / "projects" / "demo").mkdir(parents=True)
    settings = SandboxSettings(
        projects_enabled=True,
        project_public_ingress_enabled=True,
        project_ingress_domain="apps.example.com",
        project_ingress_ws_max_bytes=131_072,
    )
    observed = {}

    class Upstream:
        subprotocol = None
        close_code = 1000
        close_reason = ""

        async def close(self, code=1000):
            self.close_code = code

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    class Connection:
        async def __aenter__(self):
            self.socket = Upstream()
            return self.socket

        async def __aexit__(self, *args):
            return False

    def connect(url, **kwargs):
        observed.update(url=url, **kwargs)
        return Connection()

    monkeypatch.setattr("claw.api.project_ingress.websocket_connect", connect)

    class Projects:
        async def proxy_target(self, workspace, project, port):
            return "http://project.internal:8000"

    async def fallback(scope, receive, send):
        raise AssertionError("unexpected fallback")

    middleware = ProjectIngressMiddleware(
        fallback, settings=settings, projects=Projects(), workspaces_root=tmp_path, secret_key="secret"
    )
    host = project_ingress_host(settings, "secret", owner, "demo")
    events = iter([
        {"type": "websocket.connect"},
        {"type": "websocket.disconnect", "code": 1000},
    ])
    sent = []

    async def receive():
        return next(events)

    async def send(event):
        sent.append(event)

    await middleware(
        {
            "type": "websocket", "scheme": "wss", "path": "/socket", "raw_path": b"/socket",
            "query_string": b"token=ok", "headers": [(b"host", host.encode())],
            "client": ("203.0.113.1", 1234), "subprotocols": [],
        },
        receive,
        send,
    )

    assert observed["url"] == f"ws://{host}/socket?token=ok"
    assert observed["host"] == "project.internal"
    assert observed["port"] == 8000
    assert observed["max_size"] == 131_072
    assert sent[0]["type"] == "websocket.accept"
    await middleware.client.aclose()


async def test_per_project_connection_limit_rejects_without_leaking_global_slot(tmp_path):
    owner = uuid.uuid4().hex
    (tmp_path / owner / "projects" / "demo").mkdir(parents=True)
    settings = SandboxSettings(
        projects_enabled=True,
        project_public_ingress_enabled=True,
        project_ingress_domain="apps.example.com",
        project_ingress_max_connections=1,
        project_ingress_max_connections_per_project=1,
    )

    class Projects:
        async def proxy_target(self, workspace, project, port):
            return "http://project.internal:8000"

    async def fallback(scope, receive, send):
        raise AssertionError("unexpected fallback")

    middleware = ProjectIngressMiddleware(
        fallback, settings=settings, projects=Projects(), workspaces_root=tmp_path, secret_key="secret"
    )
    project_limit = asyncio.Semaphore(1)
    await project_limit.acquire()
    middleware._project_connections[(owner, "demo")] = project_limit
    host = project_ingress_host(settings, "secret", owner, "demo")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware), base_url="http://test"
    ) as client:
        response = await client.get("/", headers={"host": host})

    assert response.status_code == 503
    assert response.text == "Project ingress is busy"
    assert middleware._global_connections._value == 1
    project_limit.release()
    await middleware.client.aclose()


async def test_project_container_toggles_use_independent_rows(db_factory):
    store = ProjectContainerConfigStore(db_factory)

    await asyncio.gather(store.set_enabled(True), store.set_public_ingress_enabled(True))

    assert await store.get() == {"enabled": True, "public_ingress_enabled": True}
