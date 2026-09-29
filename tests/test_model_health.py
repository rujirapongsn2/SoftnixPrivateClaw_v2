from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncio
import pytest

from sqlalchemy import update

from claw.core.model_health import ModelHealthService, classify_error
from claw.api.manage import list_models as claw_list_models
from claw.config import LLMSettings
from claw.db.engine import create_engine_and_factory, init_db
from claw.db.models import LLMModel, User
from claw.db.stores import LLMConfigStore
from claw.providers.base import ChatResult, ProviderError
from sbot.db.stores import LLMConfigStore as SbotLLMConfigStore
from sbot.api.manage import list_models as sbot_list_models
from tests.conftest_app import build_api_app, client


class FakeProvider:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return ChatResult(content="OK")


@pytest.mark.asyncio
async def test_empty_probe_response_does_not_restore_auto_disabled_model(tmp_path):
    class EmptyProvider(FakeProvider):
        async def chat(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return ChatResult(content=None, tool_calls=[])

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    fake = EmptyProvider()
    service = ModelHealthService(store, fake)

    assert await service.check_model(model.id)
    assert [call[1]["max_tokens"] for call in fake.calls] == [8, 64]
    assert (await store.list_models())[0].health_status == "warning"
    assert await service.check_model(model.id, force=True)
    current = (await store.list_models())[0]
    assert current.health_status == "quarantined"
    assert current.enabled is False

    # An empty completion is not evidence that the model recovered.
    assert await service.check_model(model.id, force=True)
    current = (await store.list_models())[0]
    assert current.enabled is False
    assert current.health_auto_disabled is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_daily_probe_disables_only_when_option_is_on_and_failure_is_permanent(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1")
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    fake = FakeProvider(ProviderError("secret upstream body", status_code=401))
    service = ModelHealthService(store, fake)

    assert await service.check_model(model.id)
    current = (await store.list_models())[0]
    assert current.enabled is True
    assert current.health_status == "unavailable"
    assert current.health_reason == "auth_failed"
    assert await service.check_model(model.id) is False  # due only once a day
    assert len(fake.calls) == 1
    assert fake.calls[0][1]["max_tokens"] == 8
    assert fake.calls[0][1]["tools"][0]["function"]["name"] == "health_ping"

    await store.update_provider(provider.id, auto_disable_models=True)
    assert await service.check_model(model.id, force=True)
    current = (await store.list_models())[0]
    assert current.enabled is False
    assert current.health_auto_disabled is True
    assert current.is_fallback is False
    assert await store.has_configured_global_chat_models() is True
    assert await store.default_model_for(None) is None
    assert await store.resolve("openai/demo") is None
    assert "secret" not in current.health_reason

    # Repaired credentials trigger a fresh probe and restore only the model
    # that the health service disabled.
    fake.error = None
    await store.update_provider(provider.id, api_key="repaired-key")
    assert await service.check_model(model.id)
    current = (await store.list_models())[0]
    assert current.health_status == "recovering"
    assert current.enabled is False
    assert await service.check_model(model.id, force=True)
    current = (await store.list_models())[0]
    assert current.health_status == "healthy"
    assert current.enabled is True
    assert current.health_auto_disabled is False
    assert [m["model_id"] for m in await store.enabled_models()] == ["openai/demo"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_default_requires_manual_selection_even_with_other_model(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    default = await store.create_model(provider.id, "openai/default", "Default")
    await store.create_model(provider.id, "openai/other", "Other")
    await store.update_model(default.id, is_default=True)
    fake = FakeProvider(ProviderError("unauthorized", status_code=401))
    assert await ModelHealthService(store, fake).check_model(default.id)
    assert await store.default_model_for(None) is None
    assert [m["model_id"] for m in await store.enabled_models()] == ["openai/other"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_auto_disabled_fallback_remains_marked_for_admin_warning(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    await store.create_model(provider.id, "openai/default", "Default")
    fallback = await store.create_model(provider.id, "openai/fallback", "Fallback")
    await store.update_model(fallback.id, is_fallback=True)
    assert await ModelHealthService(store, FakeProvider(ProviderError("unauthorized", status_code=401))).check_model(fallback.id)
    current = next(m for m in await store.list_models() if m.id == fallback.id)
    assert current.is_fallback is True
    assert current.enabled is False
    assert await store.fallback_model_for(None) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_transient_errors_never_auto_disable_and_private_models_are_not_probed(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    fake = FakeProvider(ProviderError("rate limit", status_code=429))
    service = ModelHealthService(store, fake)
    assert await service.check_model(model.id)
    current = (await store.list_models())[0]
    assert current.enabled is True
    assert current.health_status == "warning"
    assert current.health_reason == "rate_limited"
    assert [m["model_id"] for m in await store.enabled_models()] == ["openai/demo"]
    assert await service.check_due() == 0
    # A first uncertain failure stays selectable. A quick successful
    # confirmation restores healthy status without changing Enabled.
    fake.error = None
    async with factory() as db:
        await db.execute(update(LLMModel).where(LLMModel.id == model.id).values(
            health_checked_at=datetime.now(timezone.utc) - timedelta(minutes=6)
        ))
        await db.commit()
    assert await service.check_due() == 1
    assert [m["model_id"] for m in await store.enabled_models()] == ["openai/demo"]
    async with factory() as db:
        db.add(User(id="u" * 32, email="owner@example.test"))
        await db.commit()
    private = await store.create_provider("private", "key", "https://example.test/v1", owner_id="u" * 32)
    private_model = await store.create_model(private.id, "openai/private", "Private", owner_id="u" * 32)
    assert await service.check_model(private_model.id, force=True) is False
    assert len(fake.calls) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_default_and_fallback_require_manual_selection(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    default = await store.create_model(provider.id, "openai/default", "Default")
    fallback = await store.create_model(provider.id, "openai/fallback", "Fallback")
    other = await store.create_model(provider.id, "openai/other", "Other")
    await store.update_model(default.id, is_default=True)
    await store.update_model(fallback.id, is_fallback=True)
    service = ModelHealthService(store, FakeProvider(ProviderError("upstream down", status_code=503)))
    assert await service.check_model(default.id)
    assert await service.check_model(fallback.id)
    assert await store.default_model_for() == "openai/default"
    assert await service.check_model(default.id, force=True)
    assert await service.check_model(fallback.id, force=True)
    assert await store.default_model_for() is None
    assert await store.fallback_model_for() is None
    assert await store.resolve("openai/default") is None
    assert [m["model_id"] for m in await store.enabled_models()] == ["openai/other"]
    assert all(not model.enabled and model.health_auto_disabled for model in await store.list_models() if model.id != other.id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_auto_disable_off_keeps_warning_model_selectable(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1")
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    assert await ModelHealthService(store, FakeProvider(ProviderError("busy", status_code=429))).check_model(model.id)
    assert [m["model_id"] for m in await store.enabled_models()] == ["openai/demo"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_bot_mode_uses_same_health_gate(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = SbotLLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    service = ModelHealthService(store, FakeProvider(ProviderError("upstream down", status_code=503)))
    assert await service.check_model(model.id)
    assert await service.check_model(model.id, force=True)
    assert await store.enabled_models() == []
    assert await store.resolve("openai/demo") is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_confirmed_transient_failure_is_quarantined_then_recovers(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    fake = FakeProvider(ProviderError("upstream down", status_code=503))
    service = ModelHealthService(store, fake)
    assert await service.check_model(model.id)
    assert (await store.list_models())[0].health_status == "warning"
    async with factory() as db:
        await db.execute(update(LLMModel).where(LLMModel.id == model.id).values(
            health_checked_at=datetime.now(timezone.utc) - timedelta(minutes=2)
        ))
        await db.commit()
    assert await service.check_due() == 1
    current = (await store.list_models())[0]
    assert current.health_status == "quarantined"
    assert current.enabled is False
    assert current.health_auto_disabled is True
    assert await store.enabled_models() == []
    assert await store.resolve("openai/demo") is None
    fake.error = None
    async with factory() as db:
        await db.execute(update(LLMModel).where(LLMModel.id == model.id).values(
            health_checked_at=datetime.now(timezone.utc) - timedelta(minutes=6)
        ))
        await db.commit()
    assert await service.check_due() == 1
    assert (await store.list_models())[0].health_status == "recovering"
    assert (await store.list_models())[0].enabled is False
    assert await service.check_model(model.id, force=True)
    assert (await store.list_models())[0].health_status == "healthy"
    assert (await store.list_models())[0].enabled is True
    assert (await store.list_models())[0].health_auto_disabled is False
    assert [m["model_id"] for m in await store.enabled_models()] == ["openai/demo"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_turn_failure_disables_after_confirmation_and_recovers(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    fake = FakeProvider(ProviderError("busy", status_code=503))
    service = ModelHealthService(store, fake)

    await service.report_failure(model.id, ProviderError("busy", status_code=503))
    current = (await store.list_models())[0]
    assert current.health_status == "warning" and current.enabled is True
    assert fake.calls == []  # no probe inside the blip; the scheduler confirms later
    async with factory() as db:
        await db.execute(update(LLMModel).where(LLMModel.id == model.id).values(
            health_checked_at=datetime.now(timezone.utc) - timedelta(minutes=2)
        ))
        await db.commit()
    assert await service.check_due() == 1
    current = (await store.list_models())[0]
    assert current.health_status == "quarantined"
    assert current.enabled is False
    assert current.health_auto_disabled is True
    assert await store.enabled_models() == []

    fake.error = None
    assert await service.check_model(model.id, force=True)
    assert (await store.list_models())[0].health_status == "recovering"
    assert await service.check_model(model.id, force=True)
    current = (await store.list_models())[0]
    assert current.health_status == "healthy"
    assert current.enabled is True
    assert current.health_auto_disabled is False

    await store.update_model(model.id, enabled=False)
    assert await service.check_model(model.id, force=True) is False
    assert (await store.list_models())[0].enabled is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_manual_off_overrides_pending_auto_recovery(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    fake = FakeProvider(ProviderError("bad key", status_code=401))
    service = ModelHealthService(store, fake)
    assert await service.check_model(model.id)
    assert (await store.list_models())[0].health_auto_disabled is True

    await store.update_model(model.id, enabled=False)
    fake.error = None
    assert await service.check_model(model.id, force=True) is False
    current = (await store.list_models())[0]
    assert current.enabled is False
    assert current.health_auto_disabled is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_bad_user_request_does_not_disable_shared_model(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    fake = FakeProvider()
    service = ModelHealthService(store, fake)
    await service.report_failure(model.id, ProviderError("bad user request", status_code=400))
    current = (await store.list_models())[0]
    assert current.health_status == "unchecked"
    assert current.enabled is True
    assert fake.calls == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_failure_from_old_provider_route_cannot_disable_updated_model(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "old-key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    snapshot = (await store.resolve("openai/demo"))["health_route"]
    await store.update_provider(provider.id, api_key="new-key")

    await ModelHealthService(store, FakeProvider()).report_failure(
        model.id, ProviderError("old key rejected", status_code=401), snapshot,
    )
    current = (await store.list_models())[0]
    assert current.enabled is True
    assert current.health_status == "unchecked"
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_type, list_endpoint", [
    (LLMConfigStore, claw_list_models),
    (SbotLLMConfigStore, sbot_list_models),
])
async def test_picker_never_substitutes_default_for_quarantined_lineup(tmp_path, store_type, list_endpoint):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = store_type(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    default = await store.create_model(provider.id, "openai/default", "Default")
    other = await store.create_model(provider.id, "openai/other", "Other")
    await store.update_model(default.id, is_default=True)
    service = ModelHealthService(store, FakeProvider(ProviderError("upstream down", status_code=503)))
    state = SimpleNamespace(
        plans=None, llm_config=store,
        settings=SimpleNamespace(llm=LLMSettings(model="openai/env", api_key="env-key")),
    )
    user = SimpleNamespace(id="user-1")
    assert await service.check_model(default.id)
    assert await service.check_model(default.id, force=True)
    picker = await list_endpoint(user, state)
    assert [m["model_id"] for m in picker["models"]] == ["openai/other"]
    assert picker["default"] is None
    assert await service.check_model(other.id)
    assert await service.check_model(other.id, force=True)
    assert await list_endpoint(user, state) == {"models": [], "default": None}
    await engine.dispose()


@pytest.mark.asyncio
async def test_turning_option_off_during_probe_prevents_auto_disable(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")

    class SwitchingProvider(FakeProvider):
        async def chat(self, messages, **kwargs):
            await store.update_provider(provider.id, auto_disable_models=False)
            raise ProviderError("unauthorized", status_code=401)

    assert await ModelHealthService(store, SwitchingProvider()).check_model(model.id)
    current = (await store.list_models())[0]
    assert current.enabled is True
    assert current.health_reason == "auth_failed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_disabling_provider_during_probe_discards_late_failure(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")

    class SwitchingProvider(FakeProvider):
        async def chat(self, messages, **kwargs):
            await store.update_provider(provider.id, enabled=False)
            raise ProviderError("unauthorized", status_code=401)

    assert await ModelHealthService(store, SwitchingProvider()).check_model(model.id) is False
    current = (await store.list_models())[0]
    assert current.enabled is True
    assert current.health_status == "unchecked"
    await engine.dispose()


def test_classifier_requires_explicit_quota_or_model_error():
    assert classify_error(ProviderError("busy", status_code=429)) == ("rate_limited", False)
    assert classify_error(ProviderError("credits", status_code=429, error_type="insufficient_quota")) == ("quota_exhausted", True)
    assert classify_error(ProviderError('{"error":{"code":"insufficient_quota"}}', status_code=429)) == ("quota_exhausted", True)
    assert classify_error(ProviderError("route", status_code=404)) == ("probe_failed", False)
    assert classify_error(ProviderError("missing", status_code=404, error_type="model_not_found")) == ("model_not_found", True)


@pytest.mark.asyncio
async def test_invalid_base_is_reported_without_spending_tokens(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "not-a-url", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    fake = FakeProvider()
    assert await ModelHealthService(store, fake).check_model(model.id)
    current = (await store.list_models())[0]
    assert fake.calls == []
    assert current.enabled is True
    assert current.health_reason == "config_error"
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_now_api_forces_check_and_is_admin_only(db_factory):
    app = build_api_app(db_factory)
    store = app.state.claw.llm_config
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    fake = FakeProvider(ProviderError("credits exhausted", status_code=429, error_type="insufficient_quota"))
    app.state.claw.model_health = ModelHealthService(store, fake)

    async with client(app) as http:
        admin = (await http.post("/api/auth/register", json={"email": "admin@example.test", "password": "password123"})).json()["access_token"]
        user = (await http.post("/api/auth/register", json={"email": "user@example.test", "password": "password123"})).json()["access_token"]
        url = f"/api/admin/models/{model.id}/health/check"
        assert (await http.post(url, headers={"Authorization": f"Bearer {user}"})).status_code == 403
        first = await http.post(url, headers={"Authorization": f"Bearer {admin}"})
        assert first.status_code == 200 and first.json() == {"checked": True}
        assert (await store.list_models())[0].enabled is False
        # The manual check bypasses the 23-hour cooldown, including for a model
        # the health checker previously disabled. It does not re-enable it.
        second = await http.post(url, headers={"Authorization": f"Bearer {admin}"})
        assert second.status_code == 200 and second.json() == {"checked": True}
        assert len(fake.calls) == 2
        await store.update_provider(provider.id, enabled=False)
        assert (await http.post(url, headers={"Authorization": f"Bearer {admin}"})).status_code == 409


@pytest.mark.asyncio
async def test_concurrent_or_rate_limited_turn_failures_do_not_disable_a_healthy_model(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    fake = FakeProvider()  # the confirmation probe succeeds: the blip is over
    service = ModelHealthService(store, fake)

    # A 429 burst is capacity, not an outage: no state change, no probe.
    await service.report_failure(model.id, ProviderError("busy", status_code=429))
    assert (await store.list_models())[0].health_status != "warning"
    assert fake.calls == []

    # Several failed turns racing through a brief 503 blip: only a probe may
    # quarantine, and it succeeded, so the model stays enabled and healthy.
    await asyncio.gather(*(service.report_failure(model.id, ProviderError("blip", status_code=503)) for _ in range(3)))
    current = (await store.list_models())[0]
    assert current.enabled is True
    assert current.health_status == "warning"
    assert fake.calls == []
    # The delayed scheduled probe succeeds once the blip is over.
    async with factory() as db:
        await db.execute(update(LLMModel).where(LLMModel.id == model.id).values(
            health_checked_at=datetime.now(timezone.utc) - timedelta(minutes=2)
        ))
        await db.commit()
    assert await service.check_due() == 1
    current = (await store.list_models())[0]
    assert current.enabled is True and current.health_status == "healthy"


@pytest.mark.asyncio
async def test_sustained_rate_limiting_never_quarantines(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    await init_db(engine)
    store = LLMConfigStore(factory)
    provider = await store.create_provider("vendor", "key", "https://example.test/v1", auto_disable_models=True)
    model = await store.create_model(provider.id, "openai/demo", "Demo")
    service = ModelHealthService(store, FakeProvider(ProviderError("busy", status_code=429)))
    for _ in range(3):
        assert await service.check_model(model.id, force=True)
    current = (await store.list_models())[0]
    assert current.enabled is True and current.health_auto_disabled is False
    assert current.health_status == "warning" and current.health_reason == "rate_limited"
    assert [m["model_id"] for m in await store.enabled_models()] == ["openai/demo"]
    # Re-checked gently (5 min), not every minute.
    async with factory() as db:
        await db.execute(update(LLMModel).where(LLMModel.id == model.id).values(
            health_checked_at=datetime.now(timezone.utc) - timedelta(minutes=2)
        ))
        await db.commit()
    assert await service.check_due() == 0
    await engine.dispose()
