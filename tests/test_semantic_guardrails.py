import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from claw.config import Settings
from claw.security.policy import PolicyEngine
from claw.security.semantic import QUESTIONS, SemanticGuardrailSettings, SemanticMonitor


@pytest.mark.asyncio
async def test_openthai_minimal_noul_response():
    import json

    settings = SemanticGuardrailSettings(provider="laya", laya={"api_key": "test"})
    audit = AsyncMock()

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "iapp/openthai-systemone"
        return httpx.Response(200, json={
            "model": "iapp/openthai-systemone",
            "answers": {name: {"type": "noul", "noul": 0.99} for name in body["questions"]},
            "usage": {"truncated_state": False, "latency_ms": 890},
        })

    service = SemanticMonitor(settings, PolicyEngine(), audit, transport=httpx.MockTransport(handler))
    result = await service.observe("ขอเงินคืนครับ", "input")
    assert result["status"] == "checked"
    assert result["model"] == "iapp/openthai-systemone"
    assert all(score == 0.99 for score in result["scores"].values())
    audit.log.assert_awaited_once()


def test_status_reports_the_models_and_endpoints_in_use():
    settings = SemanticGuardrailSettings(provider="laya", laya={"api_key": "test"})
    status = SemanticMonitor(settings, PolicyEngine(), AsyncMock()).status()
    assert status["models"] == {"jev": "jev-1.13.0", "laya": "iapp/openthai-systemone"}
    assert status["endpoints"]["laya"] == "https://genai.softnix.ai/laya/v1/decide"
    assert "test" not in str(status)  # the key itself is never reported


@pytest.mark.asyncio
async def test_openthai_truncated_state_is_skipped_and_audited():
    service, audit = monitor("laya", response={
        "answers": {name: {"type": "noul", "noul": 0.99} for name in QUESTIONS},
        "usage": {"truncated_state": True},
    })
    result = await service.observe("hello", "input")
    assert result["status"] == "skipped"
    assert result["reason"] == "truncated_state"
    assert "scores" not in result and "alerts" not in result
    assert audit.log.call_args.args[1]["reason"] == "truncated_state"


def monitor(provider="jev", response=None, handler=None):
    config = SemanticGuardrailSettings(
        provider=provider,
        jev={"endpoint": "https://example.test/decide", "model": "jev-test", "api_key": "secret"},
        laya={"endpoint": "https://example.test/decide", "model": "laya", "api_key": "secret"},
    )
    audit = AsyncMock()

    def default(request):
        return httpx.Response(
            200, json=response or {"answers": {name: {"type": "noul", "noul": 0.25} for name in QUESTIONS}}
        )

    return SemanticMonitor(
        config, PolicyEngine(monitor_only=True), audit, transport=httpx.MockTransport(handler or default)
    ), audit


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jev", "laya"])
async def test_wire_format_redaction_and_metadata_only_audit(provider):
    import json

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == ("jev-test" if provider == "jev" else "laya")
        assert "jane@example.com" not in str(body)
        assert "token-secret" not in str(body)
        assert set(body["questions"]) == set(QUESTIONS)
        return httpx.Response(
            200, json={"answers": {name: {"type": "noul", "noul": 0.99} for name in QUESTIONS}}
        )

    service, audit = monitor(provider, handler=handler)
    result = await service.observe(
        "jane@example.com Bearer token-secret", "input", user_id="u", session_id="s"
    )
    assert result["status"] == "checked"
    assert "jane@example.com" not in str(audit.log.call_args)
    assert "token-secret" not in str(audit.log.call_args)
    assert audit.log.call_args.kwargs == {"user_id": "u", "session_id": "s"}


@pytest.mark.asyncio
async def test_missing_configuration_never_calls_network():
    def unexpected(_):
        raise AssertionError("network called")

    audit = AsyncMock()
    for provider in ["off", "jev", "laya"]:
        service = SemanticMonitor(
            SemanticGuardrailSettings(provider=provider),
            PolicyEngine(),
            audit,
            transport=httpx.MockTransport(unexpected),
        )
        result = await service.observe("hello", "input", force_log=True)
        assert result["status"] == ("disabled" if provider == "off" else "not_configured")
        assert service.status()["configured"] == {"jev": False, "laya": False}
    assert audit.log.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, -1, 2, float("inf"), "0.5", None])
async def test_reject_invalid_scores_without_blocking(value):
    service, audit = monitor(
        response={"answers": {name: {"type": "noul", "noul": value} for name in QUESTIONS}}
    )
    result = await service.observe("hello", "output")
    assert result["status"] == "error"
    assert "scores" not in result
    audit.log.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_and_oversize_are_bounded():
    async def slow(_):
        await asyncio.sleep(1)
        return httpx.Response(500)

    service, audit = monitor(handler=slow)
    service.settings.timeout_seconds = 0.01
    assert (await service.observe("hello", "input"))["status"] == "error"
    assert (await service.observe("x" * 8001, "input"))["status"] == "skipped"
    assert audit.log.await_count == 2


def test_env_and_secret_safe_status(monkeypatch):
    monkeypatch.setenv("CLAW_SEMANTIC_GUARDRAILS__PROVIDER", "laya")
    monkeypatch.setenv("CLAW_SEMANTIC_GUARDRAILS__LAYA__API_KEY", "private-token")
    settings = Settings(_env_file=None).semantic_guardrails
    assert settings.provider == "laya"
    assert "private-token" not in repr(settings)
    service = SemanticMonitor(settings, PolicyEngine(), AsyncMock())
    assert service.status()["status"] == "ready"
    assert "private-token" not in str(service.status())
    with pytest.raises(ValidationError):
        SemanticGuardrailSettings(jev={"endpoint": "https://secret@example.test/api", "model": "jev"})


@pytest.mark.asyncio
async def test_monitor_does_not_block_chat(stores, tmp_path):
    from tests.conftest import FakeProvider, text_turn
    from tests.test_runtime import make_runtime

    service, _ = monitor(response={"answers": {name: {"type": "noul", "noul": 0.99} for name in QUESTIONS}})
    service.audit = stores["audit"]
    policy = PolicyEngine(monitor_only=True)
    policy.semantic = service
    runtime = make_runtime(stores, FakeProvider([text_turn("normal answer")]), tmp_path, policy=policy)
    user = await stores["users"].get_or_create_by_email("semantic@example.test")
    session = await stores["sessions"].create(user.id)
    assert await runtime.handle_message(user.id, session.id, "hello") == "normal answer"
    await runtime.drain()
    events = await stores["audit"].list(kind="semantic_guardrail")
    assert {event["payload"]["scope"] for event in events} == {"input", "output"}


@pytest.mark.asyncio
async def test_admin_configuration_status_and_authorization(db_factory):
    from tests.conftest_app import build_api_app, client
    from tests.test_admin import _bearer, _register

    app = build_api_app(db_factory)
    state = app.state.claw
    service = SemanticMonitor(SemanticGuardrailSettings(provider="laya"), state.policy, state.audit)
    state.policy.semantic = service
    async with client(app) as c:
        admin, _ = await _register(c, "semantic-admin@example.test")
        normal, _ = await _register(c, "semantic-user@example.test")
        response = await c.get("/api/admin/guardrails", headers=_bearer(admin))
        assert response.status_code == 200
        assert response.json()["semantic"]["status"] == "not_configured"
        response = await c.post(
            "/api/admin/guardrails/semantic/test", headers=_bearer(admin), json={"text": "hello"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "not_configured"
        assert (
            await c.post(
                "/api/admin/guardrails/semantic/test", headers=_bearer(normal), json={"text": "hello"}
            )
        ).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["off", "jev", "laya"])
async def test_unconfigured_chat_preserves_masking_and_skips_semantic_io(stores, tmp_path, provider):
    from tests.conftest import FakeProvider, text_turn
    from tests.test_runtime import make_runtime

    def unexpected(_):
        raise AssertionError("Unconfigured semantic provider must not receive requests")

    policy = PolicyEngine()
    audit = AsyncMock()
    service = SemanticMonitor(
        SemanticGuardrailSettings(provider=provider),
        policy,
        audit,
        transport=httpx.MockTransport(unexpected),
    )
    # Prove this path does not wait for a network concurrency slot either.
    service._slots = asyncio.Semaphore(0)
    policy.semantic = service
    fake = FakeProvider([text_turn("reply to jane@example.com")])
    runtime = make_runtime(stores, fake, tmp_path, policy=policy)
    user = await stores["users"].get_or_create_by_email("no-config@example.test")
    session = await stores["sessions"].create(user.id)
    result = await asyncio.wait_for(
        runtime.handle_message(user.id, session.id, "contact jane@example.com"),
        timeout=10,
    )
    await runtime.drain()
    assert "jane@example.com" not in result
    assert "[REDACTED_EMAIL]" in result
    assert all("jane@example.com" not in str(call) for call in fake.calls)
    audit.log.assert_not_awaited()
    decision = policy.enforce("jane@example.com", scope="tool_args")
    assert decision.masked
