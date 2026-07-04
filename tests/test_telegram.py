"""Telegram channel + account linking, with a fake transport (no live bot)."""

from typing import Any

from claw.channels.link import LinkCodeService
from claw.channels.telegram import TelegramChannel
from claw.config import SandboxSettings, Settings
from claw.core.bus import EventBus
from claw.core.memory import MemoryService
from claw.core.runtime import AgentRuntime
from tests.conftest import FakeProvider, text_turn


class FakeTransport:
    def __init__(self, updates: list[dict[str, Any]] | None = None):
        self._updates = updates or []
        self.sent: list[tuple[int, str]] = []

    async def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        batch = [u for u in self._updates if u["update_id"] >= offset]
        self._updates = []
        return batch

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def get_me(self) -> dict[str, Any]:
        return {"username": "ClawTestBot"}


def make_runtime(stores, provider, tmp_path) -> AgentRuntime:
    settings = Settings(_env_file=None, workspaces_root=tmp_path / "ws", sandbox=SandboxSettings(enabled=False))
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)
    return AgentRuntime(
        settings=settings, provider=provider, bus=EventBus(),
        users=stores["users"], sessions=stores["sessions"], messages=stores["messages"],
        memory=memory, audit=stores["audit"],
    )


def _channel(stores, provider, tmp_path, links=None):
    return TelegramChannel(
        make_runtime(stores, provider, tmp_path), stores["users"], stores["sessions"],
        FakeTransport(), links or LinkCodeService(),
    )


def _update(text: str, chat_id: int = 555, uid: int = 42) -> dict:
    return {
        "update_id": 1,
        "message": {"text": text, "chat": {"id": chat_id, "title": "DM"}, "from": {"id": uid}},
    }


# ---------------------------------------------------------------- linking

async def test_link_service_create_consume_and_unknown():
    svc = LinkCodeService()
    user_id = "u1"
    code = svc.create(user_id)
    assert len(code) == 6
    assert svc.consume(code.lower()) == user_id  # case-insensitive
    assert svc.consume(code) is None  # single-use
    assert svc.consume("ZZZZZZ") is None


async def test_link_service_expiry():
    import time

    svc = LinkCodeService(ttl_seconds=0)
    code = svc.create("u1")
    time.sleep(0.01)
    assert svc.consume(code) is None


async def test_link_command_links_telegram_to_user(stores, tmp_path):
    links = LinkCodeService()
    user = await stores["users"].get_or_create_by_email("web@x.y")
    code = links.create(user.id)
    channel = _channel(stores, FakeProvider([]), tmp_path, links)

    reply = await channel.handle_update(_update(f"/link {code}", uid=999))

    assert "linked" in reply.lower()
    linked = await stores["users"].get_by_telegram_id("999")
    assert linked is not None and linked.id == user.id


async def test_link_command_bad_code(stores, tmp_path):
    channel = _channel(stores, FakeProvider([]), tmp_path)
    reply = await channel.handle_update(_update("/link NOPE99", uid=999))
    assert "invalid or expired" in reply.lower()
    assert await stores["users"].get_by_telegram_id("999") is None


async def test_unlinked_user_is_prompted_to_link(stores, tmp_path):
    channel = _channel(stores, FakeProvider([text_turn("should not run")]), tmp_path)
    reply = await channel.handle_update(_update("hello", uid=777))
    assert "link" in reply.lower()
    # No session created for an unlinked sender.
    assert await stores["sessions"].list_for_user("777") == []


async def test_linked_user_message_routes_to_their_account(stores, tmp_path):
    links = LinkCodeService()
    user = await stores["users"].get_or_create_by_email("web@x.y")
    await stores["users"].set_telegram_id(user.id, "42")
    provider = FakeProvider([text_turn("สวัสดีจาก Telegram")])
    channel = _channel(stores, provider, tmp_path, links)

    reply = await channel.handle_update(_update("ping", uid=42))

    assert reply == "สวัสดีจาก Telegram"
    # The conversation is a session owned by the linked web user.
    sessions = await stores["sessions"].list_for_user(user.id)
    assert len(sessions) == 1 and sessions[0].channel == "telegram"


async def test_same_telegram_chat_reuses_session(stores, tmp_path):
    user = await stores["users"].get_or_create_by_email("web@x.y")
    await stores["users"].set_telegram_id(user.id, "42")
    provider = FakeProvider([text_turn("one"), text_turn("two")])
    channel = _channel(stores, provider, tmp_path)

    await channel.handle_update(_update("first", uid=42))
    await channel.handle_update(_update("second", uid=42))

    sessions = await stores["sessions"].list_for_user(user.id)
    assert len(sessions) == 1
    history = await stores["messages"].recent(sessions[0].id)
    assert [m["content"] for m in history if m["role"] == "user"] == ["first", "second"]


async def test_relinking_moves_telegram_id_to_new_user(stores, tmp_path):
    a = await stores["users"].get_or_create_by_email("a@x.y")
    b = await stores["users"].get_or_create_by_email("b@x.y")
    await stores["users"].set_telegram_id(a.id, "42")
    await stores["users"].set_telegram_id(b.id, "42")  # re-link same tg to b

    assert (await stores["users"].get_by_telegram_id("42")).id == b.id
    refreshed_a = await stores["users"].get(a.id)
    assert refreshed_a.telegram_user_id is None  # cleared from previous owner


async def test_empty_update_ignored(stores, tmp_path):
    channel = _channel(stores, FakeProvider([]), tmp_path)
    assert await channel.handle_update({"update_id": 2, "message": {"chat": {"id": 1}}}) is None
