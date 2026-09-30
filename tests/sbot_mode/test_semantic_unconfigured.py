"""Optional semantic connections must not change the existing Sbot chat path."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from claw.security.semantic import SemanticGuardrailSettings, SemanticMonitor
from sbot.security.policy import PolicyEngine
from tests.sbot_mode.conftest import FakeProvider, text_turn
from tests.sbot_mode.test_multi_bot_turn import make_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["off", "jev", "laya"])
async def test_unconfigured_sbot_chat_preserves_rules(stores, tmp_path, provider):
    def unexpected(_):
        raise AssertionError("Unconfigured provider must not be called")

    policy = PolicyEngine()
    audit = AsyncMock()
    service = SemanticMonitor(
        SemanticGuardrailSettings(provider=provider),
        policy,
        audit,
        transport=httpx.MockTransport(unexpected),
    )
    service._slots = asyncio.Semaphore(0)
    policy.semantic = service
    fake = FakeProvider([text_turn("reply to jane@example.com")])
    runtime = make_runtime(stores, fake, tmp_path)
    runtime.policy = policy
    user = await stores["users"].get_or_create_by_email("no-config@example.test")
    session = await stores["sessions"].create(user.id)
    result = await asyncio.wait_for(
        runtime.handle_message(user.id, session.id, "contact jane@example.com"),
        timeout=10,
    )
    await runtime.drain()
    assert "[REDACTED_EMAIL]" in result
    assert "jane@example.com" not in result
    assert all("jane@example.com" not in str(call) for call in fake.calls)
    audit.log.assert_not_awaited()


async def _sbot_runtime_with_rule(stores, db_factory, tmp_path, action, replies):
    import json

    from claw.security.semantic_rules import SemanticRuleBody, SemanticRuleStore

    store = SemanticRuleStore(db_factory)
    body = dict(name="R", condition="Is it bad?", scopes=["input", "output"], scale=0.5, action=action, dry_run=False)
    created = await store.create(SemanticRuleBody(**body))
    await store.update(created["id"], SemanticRuleBody(**body, enabled=True))

    def judge(request):
        questions = json.loads(request.content)["questions"]
        return httpx.Response(200, json={"answers": {q: {"type": "noul", "noul": 0.97} for q in questions}, "usage": {}})

    policy = PolicyEngine()
    policy.semantic = SemanticMonitor(
        SemanticGuardrailSettings(provider="jev", jev={"api_key": "k"}), policy, stores["audit"],
        transport=httpx.MockTransport(judge), rule_store=store,
    )
    fake = FakeProvider([text_turn(r) for r in replies])
    runtime = make_runtime(stores, fake, tmp_path)
    runtime.policy = policy
    user = await stores["users"].get_or_create_by_email("sbot-sem@example.test")
    session = await stores["sessions"].create(user.id)
    return runtime, fake, user, session


@pytest.mark.asyncio
async def test_sbot_block_stops_before_the_model(stores, db_factory, tmp_path):
    from sbot.i18n import t

    runtime, fake, user, session = await _sbot_runtime_with_rule(stores, db_factory, tmp_path, "block", ["ok"])
    result = await runtime.handle_message(user.id, session.id, "do the bad thing")
    assert result == t("policy.semantic.block", "en") and fake.calls == []


@pytest.mark.asyncio
async def test_sbot_confirm_waits_for_the_user_and_non_web_is_refused(stores, db_factory, tmp_path):
    from sbot.core.events import ToolConfirmRequest
    from sbot.i18n import t

    runtime, fake, user, session = await _sbot_runtime_with_rule(stores, db_factory, tmp_path, "confirm", ["ok"])
    async with runtime.bus.subscribe(session.id) as queue:
        turn = asyncio.create_task(runtime.handle_message(user.id, session.id, "risky"))
        request = None
        while request is None:
            event = await asyncio.wait_for(queue.get(), 5)
            request = event if isinstance(event, ToolConfirmRequest) else None
        assert fake.calls == [] and request.tool == "policy_review"
        runtime.resolve_confirmation(request.request_id, True)
        assert await asyncio.wait_for(turn, 5) == "ok"
    refused = await runtime.handle_message(user.id, session.id, "scheduled", channel="schedule")
    assert refused == t("policy.semantic.needs_web", "en")
