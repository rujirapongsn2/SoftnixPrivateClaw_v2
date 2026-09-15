from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from claw.api import project_containers


class _DnsResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"Answer": [{"type": 5, "data": "tunnel-id.cfargotunnel.com."}]}


class _FlattenedDnsResponse(_DnsResponse):
    def json(self):
        # Proxied Cloudflare records resolve to edge A/AAAA records and do not
        # expose the underlying cfargotunnel.com CNAME to recursive resolvers.
        return {"Answer": []}


class _PublicResponse:
    status_code = 404
    text = "Project route not found"


class _FakeClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, **_kwargs):
        return self.response


async def test_verify_public_ingress_checks_cname_tls_and_tunnel(monkeypatch):
    settings = SimpleNamespace(
        enabled=True,
        projects_enabled=True,
        project_public_ingress_enabled=True,
        project_ingress_domain="apps.example.com",
        project_ingress_scheme="https",
    )

    monkeypatch.setattr(project_containers.socket, "getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("203.0.113.10", 443)),
    ])

    responses = iter([_DnsResponse(), _PublicResponse()])
    monkeypatch.setattr(
        project_containers.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeClient(next(responses)),
    )

    result = await project_containers._verify_public_ingress(settings)

    assert result["ready"] is True
    assert [check["status"] for check in result["checks"]] == ["passed"] * 5
    assert result["checks"][2]["id"] == "dns"
    assert result["checks"][2]["params"]["target"] == "tunnel-id.cfargotunnel.com"


async def test_verify_public_ingress_reports_missing_domain_without_network_calls(monkeypatch):
    settings = SimpleNamespace(project_ingress_domain="")

    def unexpected_network_call(*_args, **_kwargs):
        raise AssertionError("network checks must not run without a configured domain")

    monkeypatch.setattr(project_containers.socket, "getaddrinfo", unexpected_network_call)

    result = await project_containers._verify_public_ingress(settings)

    assert result["ready"] is False
    assert [check["id"] for check in result["checks"]] == ["privateclaw", "configuration"]
    assert result["checks"][1]["status"] == "failed"


async def test_verify_public_ingress_accepts_cloudflare_cname_flattening(monkeypatch):
    settings = SimpleNamespace(
        enabled=True,
        projects_enabled=True,
        project_public_ingress_enabled=True,
        project_ingress_domain="apps.example.com",
        project_ingress_scheme="https",
    )

    monkeypatch.setattr(project_containers.socket, "getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("104.21.22.93", 443)),
    ])

    responses = iter([_FlattenedDnsResponse(), _PublicResponse()])
    monkeypatch.setattr(
        project_containers.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeClient(next(responses)),
    )

    result = await project_containers._verify_public_ingress(settings)

    assert result["ready"] is True
    assert [check["status"] for check in result["checks"]] == ["passed"] * 5
    assert result["checks"][2]["id"] == "dns"


async def test_verify_endpoint_throttles_repeated_checks(monkeypatch):
    project_containers._verify_last_started = 0
    project_containers._verify_in_flight = False
    async def fake_verify(_settings):
        return {"ready": True, "checked_at": "now", "checks": []}

    monkeypatch.setattr(project_containers, "_verify_public_ingress", fake_verify)

    audit_events = []

    class _Audit:
        async def log(self, kind, payload, user_id=None):
            audit_events.append((kind, payload, user_id))

    state = SimpleNamespace(
        project_containers=SimpleNamespace(settings=SimpleNamespace()),
        audit=_Audit(),
    )
    admin = SimpleNamespace(id="admin-id")

    assert (await project_containers.verify_project_public_ingress(admin, state))["ready"] is True
    with pytest.raises(HTTPException) as raised:
        await project_containers.verify_project_public_ingress(admin, state)

    assert raised.value.status_code == 429
    assert int(raised.value.headers["Retry-After"]) >= 1
    assert len(audit_events) == 1
