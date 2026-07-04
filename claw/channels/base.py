"""Channel abstraction. A channel maps an external messaging surface to the
runtime: inbound text becomes an agent turn, the final reply is sent back.

Channels resolve their external users to a Claw user (by a deterministic email
alias) and to a per-conversation session, so history and memory persist across
channels just like the web UI.
"""

from abc import ABC, abstractmethod

from claw.core.runtime import AgentRuntime
from claw.db.stores import SessionStore, UserStore


class Channel(ABC):
    name: str

    def __init__(self, runtime: AgentRuntime, users: UserStore, sessions: SessionStore):
        self.runtime = runtime
        self.users = users
        self.sessions = sessions
        self._session_by_chat: dict[str, str] = {}

    async def resolve_user_id(self, external_id: str) -> str:
        """Map an external account to a stable Claw user via an alias email."""
        user = await self.users.get_or_create_by_email(f"{self.name}:{external_id}@channels.claw")
        return user.id

    async def resolve_session_id(self, user_id: str, chat_key: str, title: str) -> str:
        """One persistent session per external conversation."""
        cached = self._session_by_chat.get(chat_key)
        if cached is not None:
            return cached
        for existing in await self.sessions.list_for_user(user_id):
            if existing.channel == self.name and existing.title == title:
                self._session_by_chat[chat_key] = existing.id
                return existing.id
        session = await self.sessions.create(user_id, title=title, channel=self.name)
        self._session_by_chat[chat_key] = session.id
        return session.id

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...
