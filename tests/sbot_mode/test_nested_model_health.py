"""Nested turns update the health of the selected route, even after fallback."""

from types import SimpleNamespace

import pytest

from claw.core.model_health import ModelHealthService
from sbot.core.bus import EventBus
from sbot.core.specialist import SpecialistRunner
from sbot.core.subagent import SubagentManager
from sbot.db.stores import LLMConfigStore
from sbot.providers.base import ChatResult, ProviderError, TextDelta
from tests.sbot_mode.conftest import FakeProvider
from tests.sbot_mode.test_missions import make_service
from tests.sbot_mode.test_multi_bot_turn import make_runtime


class RoutedProvider(FakeProvider):
    def __init__(self, errors):
        super().__init__([])
        self.errors = errors
        self.keys = []

    async def stream_chat(self, messages, **kwargs):
        key = kwargs["api_key"]
        self.keys.append(key)
        if key in self.errors:
            raise ProviderError("upstream rejected request", status_code=self.errors[key])
        yield TextDelta(text="backup answer")
        yield ChatResult(content="backup answer")


async def configured_routes(stores, db_factory):
    user = await stores["users"].get_or_create_by_email("nested-health@sbot.ai")
    config = LLMConfigStore(db_factory)
    upstream = await config.create_provider("primary", "primary-key", "https://example.test/v1", auto_disable_models=True)
    primary = await config.create_model(upstream.id, "openai/primary", "Primary")
    backup_upstream = await config.create_provider("backup", "backup-key", "https://backup.test/v1", auto_disable_models=True)
    backup = await config.create_model(backup_upstream.id, "openai/backup", "Backup")
    await config.update_model(primary.id, is_default=True)
    await config.update_model(backup.id, is_fallback=True)
    return user, config, primary, backup


async def run_nested(kind, provider, config, health, user, tmp_path):
    if kind == "specialist":
        runner = SpecialistRunner(provider, None, tmp_path, llm_config=config,
                                  owner_id=user.id, model_health=health)
        bot = SimpleNamespace(model="openai/primary", tool_allowlist=[])
        return (await runner.run(bot, "Answer briefly", "hello", lambda event: None, "nested")).text
    manager = SubagentManager(provider, None, tmp_path, model="openai/primary",
                              llm_config=config, owner_id=user.id, model_health=health)
    return await manager.run("hello")


@pytest.mark.parametrize("kind", ["specialist", "subagent"])
@pytest.mark.parametrize("status", [401, 429])
async def test_nested_failure_is_reported_even_when_fallback_answers(kind, status, stores, db_factory, tmp_path):
    user, config, primary, backup = await configured_routes(stores, db_factory)
    provider = RoutedProvider({"primary-key": status})
    health = ModelHealthService(config, provider)

    assert await run_nested(kind, provider, config, health, user, tmp_path) == "backup answer"
    current = {m.id: m for m in await config.list_models()}
    assert provider.keys == ["primary-key", "backup-key"]
    assert current[primary.id].enabled is (status == 429)
    assert current[primary.id].health_auto_disabled is (status == 401)
    assert current[backup.id].enabled is True


@pytest.mark.parametrize("kind", ["specialist", "subagent"])
async def test_both_nested_routes_are_disabled_when_both_fail(kind, stores, db_factory, tmp_path):
    user, config, primary, backup = await configured_routes(stores, db_factory)
    provider = RoutedProvider({"primary-key": 401, "backup-key": 401})
    health = ModelHealthService(config, provider)

    if kind == "specialist":
        with pytest.raises(ProviderError):
            await run_nested(kind, provider, config, health, user, tmp_path)
    else:
        assert "Subagent error" in await run_nested(kind, provider, config, health, user, tmp_path)
    current = {m.id: m for m in await config.list_models()}
    assert all(not current[mid].enabled and current[mid].health_auto_disabled for mid in (primary.id, backup.id))


@pytest.mark.parametrize("kind", ["specialist", "subagent"])
async def test_private_nested_failure_does_not_disable_global_namesake(kind, stores, db_factory, tmp_path):
    user, config, primary, backup = await configured_routes(stores, db_factory)
    own_provider = await config.create_provider("private", "private-key", "https://private.test/v1",
                                                owner_id=user.id, auto_disable_models=True)
    private = await config.create_model(own_provider.id, "openai/primary", "Private", owner_id=user.id)
    provider = RoutedProvider({"private-key": 401})
    health = ModelHealthService(config, provider)

    assert await run_nested(kind, provider, config, health, user, tmp_path) == "backup answer"
    current = {m.id: m for m in await config.list_models()}
    assert provider.keys == ["private-key", "backup-key"]
    assert all(current[mid].enabled for mid in (primary.id, backup.id))
    assert next(m for m in await config.list_models(user.id) if m.id == private.id).enabled
    assert current[primary.id].health_status == "unchecked"


@pytest.mark.parametrize("tool_name", ["spawn", "delegate"])
async def test_runtime_nested_tools_receive_health_service(tool_name, stores, db_factory, tmp_path):
    user, config, primary, _backup = await configured_routes(stores, db_factory)
    provider = RoutedProvider({"primary-key": 401})
    runtime = make_runtime(stores, provider, tmp_path, llm_config=config,
                           model_health=ModelHealthService(config, provider))
    bot = await stores["bots"].create(owner_id=user.id, name="Researcher", model="openai/primary")
    agent = runtime.get_agent(user.id, is_cos=True)
    async with runtime.bus.subscribe("caller") as caller, runtime.bus.subscribe("other-chat") as other:
        result = await agent.tools.get(tool_name).execute(task="hello", **({"bot_id": bot.id} if tool_name == "delegate" else {}))
        for queue in (caller, other):
            assert queue.get_nowait().type == "model_availability_changed"
            assert queue.empty()
    assert "backup answer" in str(result)
    assert next(m for m in await config.list_models() if m.id == primary.id).health_auto_disabled is True


async def test_mission_node_reports_specialist_failure(stores, db_factory, tmp_path):
    user, config, primary, _backup = await configured_routes(stores, db_factory)
    provider = RoutedProvider({"primary-key": 401})
    service = make_service(stores, provider, tmp_path)
    service.llm_config = config
    service.model_health = ModelHealthService(config, provider)
    service.bus = EventBus()
    bot = await stores["bots"].create(owner_id=user.id, name="Researcher", model="openai/primary")
    mission = await service.plan(user.id, "Research", [
        {"id": "research", "title": "Research", "instruction": "hello", "bot_id": bot.id},
    ])
    async with service.bus.subscribe("open-chat") as queue:
        await service.run_to_completion(mission.id, user.id)
        assert queue.get_nowait().type == "model_availability_changed"
    assert next(m for m in await config.list_models() if m.id == primary.id).health_auto_disabled is True
