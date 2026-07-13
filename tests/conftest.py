"""Shared fixtures: fake streaming provider and sqlite-backed stores."""

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from claw.db.engine import create_engine_and_factory, init_db
from claw.db.stores import AuditStore, MemoryStore, MessageStore, SessionStore, UserStore
from claw.providers.base import ChatResult, LLMProvider, ProviderEvent, TextDelta


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Strip ambient CLAW_*/QROQ_* before every test so the developer's real
    .env can never leak into a test's Settings(). Tests always pass their
    config explicitly (build_api_app / _settings(_env_file=None)); without
    this, importing litellm anywhere in the run calls load_dotenv() and
    injects .env into os.environ — which BaseSettings reads regardless of
    _env_file, making later tests (e.g. OIDC provider-enablement) depend on
    test ordering. monkeypatch restores the real environment afterwards."""
    for key in list(os.environ):
        if key.startswith("CLAW_") or key.startswith("QROQ_"):
            monkeypatch.delenv(key, raising=False)


class FakeProvider(LLMProvider):
    """Replays scripted turns: each turn is a list of ProviderEvents."""

    def __init__(self, turns: list[list[ProviderEvent]]):
        self.turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []

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
        "sessions": SessionStore(db_factory, is_postgres=False),
        "messages": MessageStore(db_factory, is_postgres=False),
        "memories": MemoryStore(db_factory),
        "audit": AuditStore(db_factory, is_postgres=False),
    }
