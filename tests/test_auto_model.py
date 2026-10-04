"""Auto model option end to end through the runtime: the guardrail model's judgment picks the
chat model, Local AI rules narrow the pool, and nothing is guessed when the check fails."""

import json

import httpx

from claw.core.auto_model import AUTO_MODEL, auto_available
from claw.db.stores import LLMConfigStore
from claw.i18n import t
from claw.providers.base import ChatResult, TextDelta
from claw.security.policy import PolicyEngine
from claw.security.semantic import SemanticGuardrailSettings, SemanticMonitor
from claw.security.semantic_rules import SemanticRuleBody, SemanticRuleStore
from tests.conftest import FakeProvider
from tests.test_runtime import make_runtime


class Recorder(FakeProvider):
    def __init__(self):
        super().__init__([])
        self.models: list[str | None] = []

    async def stream_chat(self, messages, tools=None, model=None, **kwargs):
        self.models.append(model)
        yield TextDelta(text="answer")
        yield ChatResult(content="answer", usage={"prompt_tokens": 7, "completion_tokens": 3})


class Judge:
    """The guardrail model: a fixed purpose index, and `noul` for every rule question."""

    def __init__(self, purpose=3, noul=0.0, status=200):
        self.purpose, self.noul, self.status, self.calls = purpose, noul, status, 0

    def __call__(self, request):
        self.calls += 1
        if self.status != 200:
            return httpx.Response(self.status, json={})
        questions = json.loads(request.content)["questions"]
        answers = {k: {"type": "noul", "noul": self.noul} for k, q in questions.items() if q["type"] == "noul"}
        answers["purpose"] = {
            "type": "score", "score": float(self.purpose), "confidence": 1.0,
            "probabilities": {str(i): 1.0 if i == self.purpose else 0.0 for i in range(6)},
        }
        return httpx.Response(200, json={"answers": answers, "usage": {}})


async def setup(stores, db_factory, tmp_path, *, judge, local=False, fallback=True, routing_rule=False, vendor="vendor"):
    config = LLMConfigStore(db_factory)
    prov = await config.create_provider(vendor, "key", "https://example.test/v1")
    default = await config.create_model(prov.id, "m/general", "General", purposes=["general"])
    await config.create_model(prov.id, "m/coder", "Coder", purposes=["coding"])
    await config.update_model(default.id, is_default=True)
    if fallback:
        fb = await config.create_model(prov.id, "m/fallback", "Fallback", purposes=["general"])
        await config.update_model(fb.id, is_fallback=True)
    if local:
        await config.create_model(prov.id, "m/local", "Local", purposes=["general", "coding"], data_locality="local")
    rules = SemanticRuleStore(db_factory)
    if routing_rule:
        body = dict(name="PII", condition="has pii", scopes=["input"], scale=0.5, action="local",
                    group="routing", dry_run=False)
        created = await rules.create(SemanticRuleBody(**body))
        await rules.update(created["id"], SemanticRuleBody(**body, enabled=True))
    policy = PolicyEngine()
    policy.semantic = SemanticMonitor(
        SemanticGuardrailSettings(provider="jev", jev={"api_key": "k"}), policy, stores["audit"],
        transport=httpx.MockTransport(judge), rule_store=rules,
    )
    provider = Recorder()
    runtime = make_runtime(stores, provider, tmp_path, policy=policy, llm_config=config)
    user = await stores["users"].get_or_create_by_email("auto@x.y")
    session = await stores["sessions"].create(user.id)
    return runtime, provider, config, policy, user, session


async def completed_info(runtime, user, session, text="hi", model=AUTO_MODEL):
    events = []
    async with runtime.bus.subscribe(session.id) as queue:
        result = await runtime.handle_message(user.id, session.id, text, model=model)
        await runtime.drain()
        while not queue.empty():
            events.append(queue.get_nowait().to_dict())
    return result, events


async def test_auto_sends_a_coding_message_to_the_coding_model(stores, db_factory, tmp_path):
    runtime, provider, _, _, user, session = await setup(stores, db_factory, tmp_path, judge=Judge(purpose=3))
    _, events = await completed_info(runtime, user, session, "write a sort function")
    assert provider.models == ["m/coder"]
    done = next(e for e in events if e["type"] == "turn_completed")
    assert done["info"]["model"] == "m/coder" and done["info"]["auto"] is True
    audit = await stores["audit"].list(kind="auto_model_route")
    assert audit[0]["payload"]["model"] == "m/coder" and audit[0]["payload"]["reason"] == "purpose"


async def test_auto_stays_selected_for_the_next_message_of_the_chat(stores, db_factory, tmp_path):
    runtime, provider, _, _, user, session = await setup(stores, db_factory, tmp_path, judge=Judge(purpose=3))
    await completed_info(runtime, user, session)
    await completed_info(runtime, user, session, model=None)
    assert provider.models == ["m/coder", "m/coder"]


async def test_a_failed_check_without_routing_rules_uses_the_default_model(stores, db_factory, tmp_path):
    runtime, provider, *_, user, session = await setup(stores, db_factory, tmp_path, judge=Judge(status=500))
    await completed_info(runtime, user, session)
    assert provider.models == ["m/general"]


async def test_a_matching_routing_rule_sends_the_message_to_a_local_model(stores, db_factory, tmp_path):
    runtime, provider, *_, user, session = await setup(
        stores, db_factory, tmp_path, judge=Judge(purpose=3, noul=0.95), local=True, routing_rule=True
    )
    await completed_info(runtime, user, session, "my customer's national id is ...")
    assert provider.models == ["m/local"]


async def test_sensitive_data_with_no_local_model_is_stopped_not_sent_outside(stores, db_factory, tmp_path):
    runtime, provider, *_, user, session = await setup(
        stores, db_factory, tmp_path, judge=Judge(noul=0.95), routing_rule=True
    )
    result, events = await completed_info(runtime, user, session, "my customer's national id is ...")
    assert result == t("error.no_local_model", "en")
    assert provider.models == []
    assert any(e["type"] == "turn_error" and e.get("code") == "no_local_model" for e in events)


async def test_a_failed_check_with_live_routing_rules_stays_local(stores, db_factory, tmp_path):
    runtime, provider, *_, user, session = await setup(
        stores, db_factory, tmp_path, judge=Judge(status=500), local=True, routing_rule=True
    )
    await completed_info(runtime, user, session)
    assert provider.models == ["m/local"]


async def test_choosing_a_model_by_hand_never_runs_the_routing_check(stores, db_factory, tmp_path):
    judge = Judge(purpose=3)
    runtime, provider, *_, user, session = await setup(stores, db_factory, tmp_path, judge=judge)
    await completed_info(runtime, user, session, model="m/general")
    assert provider.models == ["m/general"] and judge.calls == 0


async def test_auto_needs_a_ready_check(stores, db_factory, tmp_path):
    _, _, config, policy, *_ = await setup(stores, db_factory, tmp_path, judge=Judge())
    assert await auto_available(config, policy.semantic, None) is True
    assert await auto_available(config, None, None) is False


async def test_auto_is_not_offered_without_a_fallback_model(stores, db_factory, tmp_path):
    _, _, config, policy, *_ = await setup(stores, db_factory, tmp_path, judge=Judge(), fallback=False)
    assert await auto_available(config, policy.semantic, None) is False
