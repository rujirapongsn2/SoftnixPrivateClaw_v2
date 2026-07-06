"""Telegram channel via the Bot API (long polling).

Uses getUpdates with a long-poll timeout — no fixed-interval busy loop — and
sendMessage for replies. The HTTP transport is injectable so the message
mapping can be unit-tested without a live bot.
"""

import asyncio
from typing import Any, Protocol

from loguru import logger

from claw.channels.base import Channel
from claw.channels.link import LinkCodeService
from claw.core.runtime import AgentRuntime
from claw.db.stores import SessionStore, UserStore

_API = "https://api.telegram.org"

_LINK_PROMPT = (
    "👋 Your Telegram isn't linked to a Softnix PrivateClaw account yet.\n"
    "Open the web app → Settings → Telegram to get a link code, then send:\n"
    "/link YOURCODE"
)


class TelegramTransport(Protocol):
    async def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]: ...
    async def send_message(self, chat_id: int, text: str) -> None: ...
    async def get_me(self) -> dict[str, Any]: ...


class HttpTelegramTransport:
    def __init__(self, token: str):
        self._base = f"{_API}/bot{token}"

    async def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            resp = await client.get(
                f"{self._base}/getUpdates",
                params={"offset": offset, "timeout": timeout},
            )
        resp.raise_for_status()
        return resp.json().get("result", [])

    async def send_message(self, chat_id: int, text: str) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                f"{self._base}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )

    async def get_me(self) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self._base}/getMe")
        resp.raise_for_status()
        return resp.json().get("result", {})


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(
        self,
        runtime: AgentRuntime,
        users: UserStore,
        sessions: SessionStore,
        transport: TelegramTransport,
        links: LinkCodeService,
        poll_timeout: int = 25,
    ):
        super().__init__(runtime, users, sessions)
        self.transport = transport
        self.links = links
        self.poll_timeout = poll_timeout
        self.bot_username = ""
        self._offset = 0
        self._running = False
        self._task: asyncio.Task | None = None

    def start_task(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self.start())

    async def start(self) -> None:
        self._running = True
        try:
            me = await self.transport.get_me()
            self.bot_username = me.get("username", "")
        except Exception as exc:
            logger.warning("Telegram getMe failed: {}", exc)
        logger.info("Telegram channel polling started (bot=@{})", self.bot_username)
        while self._running:
            try:
                updates = await self.transport.get_updates(self._offset, self.poll_timeout)
                for update in updates:
                    self._offset = max(self._offset, update.get("update_id", 0) + 1)
                    await self.handle_update(update)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Telegram poll error: {}", exc)
                await asyncio.sleep(3)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def handle_update(self, update: dict[str, Any]) -> str | None:
        """Map one Telegram update to an agent turn for the LINKED Claw user.

        Telegram accounts must be linked to a real account first (privacy): an
        unlinked sender is prompted to link, and `/link CODE` performs the link.
        """
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        from_user = message.get("from") or {}
        tg_id = str(from_user.get("id") or chat_id or "")
        if not text or chat_id is None:
            return None

        if text.lower().startswith("/link"):
            reply = await self._handle_link_command(text, tg_id)
            await self.transport.send_message(chat_id, reply)
            return reply

        user = await self.users.get_by_telegram_id(tg_id)
        if user is None:
            await self.transport.send_message(chat_id, _LINK_PROMPT)
            return _LINK_PROMPT
        if not user.is_active:
            await self.transport.send_message(chat_id, "Your account is suspended.")
            return None

        title = f"Telegram · {chat.get('title') or from_user.get('username') or chat_id}"
        session_id = await self.resolve_session_id(user.id, str(chat_id), title)
        reply = await self.runtime.handle_message(
            user_id=user.id,
            session_id=session_id,
            content=text,
            channel=self.name,
            locale=from_user.get("language_code", "en")[:2],
        )
        if reply:
            await self.transport.send_message(chat_id, reply)
        return reply

    async def _handle_link_command(self, text: str, tg_id: str) -> str:
        parts = text.split()
        code = parts[1] if len(parts) > 1 else ""
        if not code:
            return "Send /link followed by the code from the web app, e.g. /link ABC123"
        user_id = self.links.consume(code)
        if user_id is None:
            return "❌ That link code is invalid or expired. Generate a new one in the web app."
        await self.users.set_telegram_id(user_id, tg_id)
        return "✅ Your Telegram is now linked. You can chat with Claw here."


async def validate_bot_token(token: str) -> dict[str, Any]:
    """Call Telegram's getMe to confirm a token is real before persisting it —
    an admin pasting a bad token gets immediate feedback instead of a bot that
    silently never connects. Raises if the token is invalid/unreachable."""
    return await HttpTelegramTransport(token).get_me()


class TelegramManager:
    """(Re)starts the Telegram channel from admin-set config, live — no process
    restart needed. Mirrors ConnectorManager's reconnect-on-change shape: calling
    ``ensure_running`` with the same token as what's already running is a no-op;
    a changed (or newly blank) token stops the old channel first.
    """

    def __init__(self, runtime: AgentRuntime, users: UserStore, sessions: SessionStore, links: LinkCodeService):
        self.runtime = runtime
        self.users = users
        self.sessions = sessions
        self.links = links
        self.channel: TelegramChannel | None = None
        self._token = ""

    async def ensure_running(self, token: str) -> TelegramChannel | None:
        token = (token or "").strip()
        if not token:
            await self.stop()
            return None
        if self.channel is not None and self._token == token:
            return self.channel  # already running with this token
        await self.stop()
        channel = TelegramChannel(self.runtime, self.users, self.sessions, HttpTelegramTransport(token), self.links)
        channel.start_task()
        self.channel = channel
        self._token = token
        return channel

    async def stop(self) -> None:
        if self.channel is not None:
            await self.channel.stop()
            self.channel = None
            self._token = ""
