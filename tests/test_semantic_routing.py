"""Auto routing: the purpose score question, Local AI routing rules, and the guarantee that
routing rules never leak into the block/confirm check."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from claw.security.policy import PolicyEngine
from claw.security.semantic import SemanticGuardrailSettings, SemanticMonitor
from claw.security.semantic_routing import route_message
from claw.security.semantic_rules import TEMPLATES, SemanticRuleBody, routing_rules


class Rules:
    def __init__(self, rules):
        self._rules = rules

    async def enabled(self):
        return self._rules


def rule(**kw):
    base = {"id": "r1", "name": "PII", "condition": "has pii", "exclusions": "", "scopes": ["input"],
            "enabled": True, "scale": 0.5, "group": "routing", "action": "local", "dry_run": False,
            "act_threshold": 0.9, "message": ""}
    return {**base, **kw}


def purpose_answer(index=3, confidence=1.0):
    probabilities = {str(i): 0.0 for i in range(6)} | {str(index): 1.0}
    return {"type": "score", "score": float(index), "confidence": confidence, "probabilities": probabilities}


def service(rules, *, noul=0.0, answer=None, status=200):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if status != 200:
            return httpx.Response(status, json={})
        answers = {k: {"type": "noul", "noul": noul} for k, q in body["questions"].items() if q["type"] == "noul"}
        answers["purpose"] = answer or purpose_answer()
        return httpx.Response(200, json={"answers": answers, "usage": {}})

    config = SemanticGuardrailSettings(
        provider="jev", jev={"endpoint": "https://example.test/decide", "model": "m", "api_key": "k"}
    )
    monitor = SemanticMonitor(config, PolicyEngine(), AsyncMock(), transport=httpx.MockTransport(handler),
                              rule_store=Rules(rules))
    return monitor, seen


async def test_purpose_judgment_comes_back_by_name():
    monitor, seen = service([])
    decision = await route_message(monitor, "write me a sort function")
    assert decision.checked and decision.judgment["coding"] == 1.0
    assert seen[0]["questions"]["purpose"]["type"] == "score"
    assert len(seen[0]["questions"]["purpose"]["criteria"]) == 6
    assert not decision.local_required


async def test_a_firing_routing_rule_requires_local():
    monitor, _ = service([rule()], noul=0.9)
    decision = await route_message(monitor, "my customer is ...")
    assert decision.local_required and decision.local_reasons == ["PII"]


async def test_a_dry_run_rule_is_recorded_but_does_not_force_local():
    monitor, _ = service([rule(dry_run=True)], noul=0.9)
    decision = await route_message(monitor, "my customer is ...")
    assert not decision.local_required and decision.rule_scores == {"PII": 0.9}


async def test_failed_check_with_live_routing_rules_fails_safe_to_local():
    monitor, _ = service([rule()], status=500)
    decision = await route_message(monitor, "anything")
    assert not decision.checked and decision.local_required and "check_failed" in decision.local_reasons


async def test_failed_check_without_routing_rules_just_has_no_judgment():
    monitor, _ = service([], status=500)
    decision = await route_message(monitor, "anything")
    assert not decision.checked and not decision.local_required and decision.judgment is None


async def test_a_secret_pattern_forces_local_even_when_the_provider_is_down():
    monitor, _ = service([], status=500)
    decision = await route_message(monitor, "my key is sk-abcdefghijklmnopqrstuvwxyz0123456789")
    assert decision.local_required and "sensitive_pattern" in decision.local_reasons


async def test_routing_rules_never_reach_the_block_confirm_check():
    monitor, seen = service([rule()], noul=0.99)
    verdict = await monitor.evaluate("hello", "input")
    assert verdict.action is None
    assert seen == []  # no acting rule remains, so no request was made at all


def test_local_action_and_routing_group_must_go_together():
    base = {"name": "n", "condition": "c", "scopes": ["input"]}
    with pytest.raises(ValidationError):
        SemanticRuleBody(**base, action="local", group="custom")
    with pytest.raises(ValidationError):
        SemanticRuleBody(**base, action="block", group="routing")
    with pytest.raises(ValidationError):
        SemanticRuleBody(name="n", condition="c", scopes=["input", "output"], action="local", group="routing")
    assert SemanticRuleBody(**base, action="local", group="routing")


def test_routing_templates_are_valid_and_start_in_dry_run():
    templates = [t for t in TEMPLATES if t["group"] == "routing"]
    assert len(templates) == 5
    for t in templates:
        body = SemanticRuleBody(**{k: v for k, v in t.items() if k != "id"})
        assert body.action == "local" and body.dry_run and not body.enabled
    assert routing_rules([rule(), rule(id="r2", scopes=["output"])]) == [rule()]
