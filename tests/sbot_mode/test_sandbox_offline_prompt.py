"""sbot shares the sandbox, so its system prompt says the same thing when it has no network."""

import pytest

from claw.sandbox.capabilities import OFFLINE_PROMPT
from sbot.config import LLMSettings, SandboxSettings, Settings
from sbot.core.bus import EventBus
from sbot.core.memory import MemoryService
from sbot.core.runtime import AgentRuntime
from tests.sbot_mode.conftest import FakeProvider, text_turn


def system_text(call: list[dict]) -> str:
    return next(m["content"] for m in call if m["role"] == "system")


@pytest.mark.parametrize(("network", "offline"), [("none", True), ("bridge", False)])
async def test_sbot_prompt_says_when_the_sandbox_is_offline(network, offline, stores, tmp_path):
    provider = FakeProvider([text_turn("ok")])
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=SandboxSettings(enabled=True, network=network),
        llm=LLMSettings(),
    )
    runtime = AgentRuntime(
        settings=settings, provider=provider, bus=EventBus(), users=stores["users"], bots=stores["bots"],
        sessions=stores["sessions"], messages=stores["messages"],
        memory=MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider),
        audit=stores["audit"],
    )
    user = await stores["users"].get_or_create_by_email("offline-prompt@sbot.ai")
    bot = await stores["bots"].create(owner_id=user.id, name="Analyst", role_title="Analyst")
    session = await stores["sessions"].create(user.id, title="chat", bot_id=bot.id, kind="direct")
    await runtime.handle_message(user.id, session.id, "hello")
    assert (OFFLINE_PROMPT in system_text(provider.calls[-1])) is offline


def test_offline_rule_is_scoped_to_exec_not_project_containers():
    assert "with `exec`" in OFFLINE_PROMPT and "project" not in OFFLINE_PROMPT
