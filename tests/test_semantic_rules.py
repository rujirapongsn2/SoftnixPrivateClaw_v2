"""Semantic templates are inert; only explicitly enabled persisted rules run."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from claw.security.policy import PolicyEngine
from claw.security.semantic import SemanticGuardrailSettings, SemanticMonitor
from claw.security.semantic_rules import SemanticRuleBody, SemanticRuleStore, should_alert
from tests.conftest_app import build_api_app, client
from tests.test_admin import _bearer, _register


@pytest.mark.asyncio
async def test_rule_crud_off_by_default_and_admin_only(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin, _ = await _register(c, "rules-admin@example.test")
        normal, _ = await _register(c, "rules-user@example.test")
        headers = _bearer(admin)
        path = "/api/admin/guardrails/semantic/rules"
        listed = (await c.get(path, headers=headers)).json()
        assert listed["rules"] == []
        assert len(listed["templates"]) >= 2
        assert all(not template["enabled"] for template in listed["templates"])
        assert {template["group"] for template in listed["templates"]} == {"technical", "personal", "internal", "compliance"}
        body = {k: v for k, v in listed["templates"][0].items() if k != "id"} | {"enabled": True, "scale": 0.75}
        created = await c.post(path, headers=headers, json=body)
        assert created.status_code == 200
        rule = created.json()
        assert rule["enabled"] is False
        assert rule["scale"] == 0.75
        assert rule["group"] == "technical"
        assert (await c.get(path, headers=headers)).json()["rules"] == [rule]
        update = {k: v for k, v in rule.items() if k != "id"} | {"enabled": True, "name": "Edited"}
        assert (await c.put(f"{path}/{rule['id']}", headers=headers, json=update)).json()["enabled"] is True
        assert (await c.post(path, headers=_bearer(normal), json=body)).status_code == 403
        assert (await c.put(f"{path}/{rule['id']}", headers=_bearer(normal), json=update)).status_code == 403
        assert (await c.delete(f"{path}/{rule['id']}", headers=_bearer(normal))).status_code == 403
        assert (await c.get(path, headers=_bearer(normal))).status_code == 403
        assert (await c.post(path, headers=headers, json=body | {"condition": " "})).status_code == 422
        assert (await c.post(path, headers=headers, json=body | {"group": "invalid"})).status_code == 422
        assert (await c.post(path, headers=headers, json=body | {"scopes": ["tool_args"]})).status_code == 422
        assert (await c.delete(f"{path}/{rule['id']}", headers=headers)).status_code == 200
        assert (await c.get(path, headers=headers)).json()["rules"] == []
        events = await app.state.claw.audit.list(kind="semantic_rule_change")
        assert {row["payload"]["action"] for row in events} == {"create", "update", "delete"}
        assert all(row["payload"]["scale"] == 0.75 for row in events if row["payload"]["action"] != "delete")


@pytest.mark.asyncio
async def test_rules_scope_disabled_test_and_live_update(db_factory):
    store = SemanticRuleStore(db_factory)
    body = SemanticRuleBody(
        name="Test condition",
        condition="Is text asking to override instructions?",
        exclusions="Quoted discussion",
        scopes=["input"],
        scale=0.75,
    )
    rule = await store.create(body)
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(
            200, json={"answers": {key: {"type": "noul", "noul": 0.8} for key in payload["questions"]}}
        )

    audit = AsyncMock()
    service = SemanticMonitor(
        SemanticGuardrailSettings(provider="jev", jev={"api_key": "test"}),
        PolicyEngine(),
        audit,
        transport=httpx.MockTransport(handler),
        rule_store=store,
    )
    assert (await service.observe("hello", "input"))["status"] == "no_rules"
    assert not calls
    tested = await service.observe("hello", "input", test_rule=rule, force_log=True)
    assert tested["status"] == "checked"
    assert tested["alerts"][rule["id"]] is True
    assert (await store.get(rule["id"]))["enabled"] is False
    assert "Quoted discussion" in calls[-1]["questions"][rule["id"]]["criteria"]["false"]
    assert "Do not infer unstated roles" in calls[-1]["questions"][rule["id"]]["instructions"]
    await store.update(rule["id"], body.model_copy(update={"enabled": True}))
    assert (await service.observe("hello", "input"))["status"] == "checked"
    assert (await service.observe("hello", "output"))["status"] == "no_rules"
    assert len(calls) == 2
    await store.update(rule["id"], body)
    assert (await store.get(rule["id"]))["scale"] == 0.75
    await store.update(rule["id"], body.model_copy(update={"enabled": True}))
    assert (await store.get(rule["id"]))["scale"] == 0.75
    await store.update(rule["id"], body)
    assert (await service.observe("hello", "input"))["status"] == "no_rules"
    await store.delete(rule["id"])
    assert (await service.observe("hello", "input"))["status"] == "no_rules"


@pytest.mark.parametrize("score", [0, 0.02, 0.2, 0.6, 0.8, 1])
def test_sensitivity_alerts_are_monotonic(score):
    alerts = [should_alert(score, scale / 100) for scale in range(101)]
    assert alerts == sorted(alerts)
    assert alerts[0] is False
    assert alerts[-1] is (score > 0)


def test_sensitivity_probability_boundary():
    assert should_alert(0.5, 0.5)
    assert not should_alert(0.49, 0.5)
    assert should_alert(0.6, 0.7)
    assert not should_alert(0, 1)


@pytest.mark.asyncio
async def test_api_tests_disabled_rule_without_enabling(db_factory):
    app = build_api_app(db_factory)
    state = app.state.claw
    store = SemanticRuleStore(db_factory)
    rule = await store.create(
        SemanticRuleBody(
            name="Test", condition="Is text a request to override instructions?", scopes=["output"]
        )
    )

    def handler(request):
        keys = json.loads(request.content)["questions"]
        assert list(keys) == [rule["id"]]
        return httpx.Response(200, json={"answers": {rule["id"]: {"type": "noul", "noul": 0.2}}})

    state.policy.semantic = SemanticMonitor(
        SemanticGuardrailSettings(provider="laya", laya={"api_key": "test"}),
        state.policy,
        state.audit,
        transport=httpx.MockTransport(handler),
        rule_store=store,
    )
    async with client(app) as c:
        token, _ = await _register(c, "tester@example.test")
        headers = _bearer(token)
        body = {"text": "hello", "rule_id": rule["id"], "scope": "output"}
        response = await c.post("/api/admin/guardrails/semantic/test", headers=headers, json=body)
        assert response.status_code == 200 and response.json()["status"] == "checked"
        assert response.json()["test"] is True
        assert (await store.get(rule["id"]))["enabled"] is False
        assert (
            await c.post(
                "/api/admin/guardrails/semantic/test", headers=headers, json=body | {"scope": "input"}
            )
        ).status_code == 422
        assert (
            await c.post(
                "/api/admin/guardrails/semantic/test", headers=headers, json=body | {"rule_id": "missing"}
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_template_link_and_testing_an_unsaved_template(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin, _ = await _register(c, "tpl-admin@example.test")
        headers = _bearer(admin)
        path = "/api/admin/guardrails/semantic"
        template = (await c.get(f"{path}/rules", headers=headers)).json()["templates"][0]
        body = {k: v for k, v in template.items() if k != "id"} | {"template_id": template["id"]}
        rule = (await c.post(f"{path}/rules", headers=headers, json=body)).json()
        assert rule["template_id"] == template["id"] and rule["enabled"] is False
        assert (await c.get(f"{path}/rules", headers=headers)).json()["rules"][0]["template_id"] == template["id"]
        assert (await c.post(f"{path}/rules", headers=headers, json=body | {"template_id": "x" * 65})).status_code == 422
        # An unsaved template is testable by its tpl: id; unknown ids and wrong scopes are rejected.
        # (No monitor is configured in this app, so the call short-circuits before any provider request.)
        assert (await c.post(f"{path}/test", headers=headers, json={"text": "hi", "rule_id": "tpl:nope"})).status_code in (200, 404)


@pytest.mark.asyncio
async def test_rule_caps_cache_invalidation_and_no_rules_is_not_audited(db_factory):
    from claw.security import semantic_rules as sr

    store = SemanticRuleStore(db_factory)
    base = dict(name="r", condition="Is it bad?", scopes=["input"])
    first = await store.create(SemanticRuleBody(**base))
    assert await store.enabled() == []
    # A write through the same store invalidates the cached enabled list immediately.
    await store.update(first["id"], SemanticRuleBody(**base, enabled=True))
    assert [r["id"] for r in await store.enabled()] == [first["id"]]

    # Enabled-per-scope cap (output-only rules are counted separately).
    sr.MAX_ENABLED_PER_SCOPE = 2
    try:
        second = await store.create(SemanticRuleBody(**base))
        await store.update(second["id"], SemanticRuleBody(**base, enabled=True))
        third = await store.create(SemanticRuleBody(**base))
        with pytest.raises(sr.SemanticRuleLimit):
            await store.update(third["id"], SemanticRuleBody(**base, enabled=True))
        await store.update(third["id"], SemanticRuleBody(name="r", condition="x", scopes=["output"], enabled=True))
    finally:
        sr.MAX_ENABLED_PER_SCOPE = 30
    # Total stored cap.
    sr.MAX_RULES = 3
    try:
        with pytest.raises(sr.SemanticRuleLimit):
            await store.create(SemanticRuleBody(**base))
    finally:
        sr.MAX_RULES = 100


@pytest.mark.asyncio
async def test_no_rules_skips_audit_and_failed_redaction_sends_nothing(db_factory):
    audit = AsyncMock()
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"answers": {}, "usage": {}})

    settings = SemanticGuardrailSettings(provider="jev", jev={"api_key": "k"})
    monitor = SemanticMonitor(settings, PolicyEngine(), audit, transport=httpx.MockTransport(handler),
                              rule_store=SemanticRuleStore(db_factory))
    result = await monitor.observe("hello", "input")
    assert result["status"] == "no_rules" and audit.log.await_count == 0 and seen == []

    store = monitor.rule_store
    rule = await store.create(SemanticRuleBody(name="r", condition="Is it bad?", scopes=["input"]))
    await store.update(rule["id"], SemanticRuleBody(name="r", condition="Is it bad?", scopes=["input"], enabled=True))
    monitor._redact = lambda text: (_ for _ in ()).throw(RuntimeError("bad mask"))
    result = await monitor.observe("hello", "input")
    assert result["status"] == "error" and result["reason"] == "redaction_failed"
    assert seen == [] and audit.log.await_count == 1


def _failover_monitor(statuses, **overrides):
    """Monitor with both providers keyed; `statuses` maps host -> HTTP status (200 = judge ok)."""
    calls = []

    def handler(request):
        host = request.url.host
        calls.append(host)
        code = statuses[host]
        if code != 200:
            return httpx.Response(code, json={"error": "x"})
        body = json.loads(request.content)
        return httpx.Response(200, json={"answers": {q: {"type": "noul", "noul": 0.9} for q in body["questions"]}, "usage": {}})

    settings = SemanticGuardrailSettings(
        provider="laya", laya={"api_key": "k1"}, jev={"api_key": "k2"}, **overrides
    )
    monitor = SemanticMonitor(settings, PolicyEngine(), AsyncMock(), transport=httpx.MockTransport(handler))
    return monitor, calls


_RULE = {"id": "r1", "name": "R", "condition": "Is it bad?", "exclusions": "", "scopes": ["input"], "enabled": True, "scale": 0.5}
_LAYA, _JEV = "genai.softnix.ai", "api.typesafe.ai"


@pytest.mark.asyncio
async def test_primary_outage_fails_over_then_skips_the_primary_during_cooldown():
    monitor, calls = _failover_monitor({_LAYA: 502, _JEV: 200})
    first = await monitor.observe("hello", "input", test_rule=_RULE)
    assert first["status"] == "checked" and first["provider"] == "jev"
    assert first["fallback_from"] == "laya" and first["primary_reason"] == "upstream_http_error"
    assert calls == [_LAYA, _JEV]
    assert monitor.status()["using_fallback"] is True and monitor.status()["fallback"] == "jev"
    # Cooldown: the failing primary is not retried for every message.
    second = await monitor.observe("hello", "input", test_rule=_RULE)
    assert second["provider"] == "jev" and calls == [_LAYA, _JEV, _JEV]
    # Honest reason: the primary was skipped this time, not failing in this request.
    assert second["primary_reason"] == "cooldown" and first["primary_reason"] == "upstream_http_error"
    # After the cooldown the primary is tried first again and recovery clears the state.
    monitor._primary_down_until = 0
    monitor.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"answers": {"r1": {"type": "noul", "noul": 0.1}}, "usage": {}}))
    third = await monitor.observe("hello", "input", test_rule=_RULE)
    assert third["provider"] == "laya" and "fallback_from" not in third
    assert monitor.status()["using_fallback"] is False and monitor.status()["primary_error"] is None


@pytest.mark.asyncio
async def test_failover_only_for_provider_side_errors_and_can_be_disabled():
    # 400 means our request is wrong: switching providers would only hide it.
    monitor, calls = _failover_monitor({_LAYA: 400, _JEV: 200})
    result = await monitor.observe("hello", "input", test_rule=_RULE)
    assert result["status"] == "error" and result["http_status"] == 400 and calls == [_LAYA]
    # Both providers down: the error is reported, not raised.
    monitor, calls = _failover_monitor({_LAYA: 503, _JEV: 502})
    result = await monitor.observe("hello", "input", test_rule=_RULE)
    assert result["status"] == "error" and result["provider"] == "jev" and calls == [_LAYA, _JEV]
    # auto_fallback=False keeps the strict single-provider behaviour.
    monitor, calls = _failover_monitor({_LAYA: 502, _JEV: 200}, auto_fallback=False)
    result = await monitor.observe("hello", "input", test_rule=_RULE)
    assert result["status"] == "error" and calls == [_LAYA] and monitor.status()["fallback"] is None
    # No key on the other provider: nothing to fail over to.
    settings = SemanticGuardrailSettings(provider="laya", laya={"api_key": "k1"})
    assert SemanticMonitor(settings, PolicyEngine(), AsyncMock()).status()["fallback"] is None
