"""Shared fixtures: fake streaming provider and sqlite-backed stores."""

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from sbot.db.engine import create_engine_and_factory, init_db
from sbot.db.stores import AuditStore, BotStore, MemoryStore, MessageStore, MissionStore, SessionStore, UserStore
from sbot.providers.base import ChatResult, LLMProvider, ProviderEvent, TextDelta


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Strip the app's ambient env vars before every test so the developer's
    real .env can never leak into a test's Settings(). Tests always pass their
    config explicitly (build_api_app / _settings(_env_file=None)); without
    this, importing litellm anywhere in the run calls load_dotenv() and
    injects .env into os.environ — which BaseSettings reads regardless of
    _env_file, making later tests (e.g. OIDC provider-enablement) depend on
    test ordering. monkeypatch restores the real environment afterwards.

    SBOT_ is the live prefix (see Settings.model_config); CLAW_ is the old one,
    kept because a long-lived .env may still carry it. Missing SBOT_ here meant
    a real SBOT_DATABASE_URL could point a test at the developer's database."""
    for key in list(os.environ):
        if key.startswith(("SBOT_", "CLAW_", "QROQ_")):
            monkeypatch.delenv(key, raising=False)


class FakeProvider(LLMProvider):
    """Replays scripted turns: each turn is a list of ProviderEvents."""

    def __init__(self, turns: list[list[ProviderEvent]]):
        self.turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []
        # Tool names offered on each call. What the model was allowed to see is
        # the observable half of a per-bot allowlist — the other half being
        # whether the registry refuses a call it was never offered.
        self.offered_tools: list[list[str]] = []
        # The model each call actually ran on — the observable half of a bot's
        # configured model, since resolution happens before the provider is hit.
        self.models: list[str | None] = []

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append(list(messages))
        self.offered_tools.append([t["function"]["name"] for t in tools or []])
        self.models.append(model)
        if not self.turns:
            yield ChatResult(content="(exhausted)")
            return
        for event in self.turns.pop(0):
            yield event

    def count_tokens(self, messages: list[dict[str, Any]], model: str | None = None) -> int:
        import json

        return len(json.dumps(messages, ensure_ascii=False, default=str)) // 4


def text_turn(text: str) -> list[ProviderEvent]:
    return [TextDelta(text=text), ChatResult(content=text, usage={"prompt_tokens": 10, "completion_tokens": 5})]


@pytest.fixture
async def db_factory(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    yield factory
    await engine.dispose()


@pytest.fixture
async def stores(db_factory):
    return {
        "users": UserStore(db_factory),
        "bots": BotStore(db_factory),
        "missions": MissionStore(db_factory),
        "sessions": SessionStore(db_factory, is_postgres=False),
        "messages": MessageStore(db_factory, is_postgres=False),
        "memories": MemoryStore(db_factory),
        "audit": AuditStore(db_factory, is_postgres=False),
    }
