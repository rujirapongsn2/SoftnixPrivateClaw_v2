"""Agent Channel: an external agent (e.g. a Grok bot) chats with PrivateClaw
as the user who issued its API key.

Unlike the web UI and Telegram, the caller is a program, not a person. So a
turn may use only the tools of the connectors the owner picked for this key,
plus a few harmless built-ins; nothing that runs code, browses, writes files
or starts other agents. Each conversation id the agent sends maps to one chat
session, so it can keep talking in context, and the owner sees that chat in
their own history.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from loguru import logger

from claw.core.events import TurnError
from claw.core.runtime import AgentRuntime, TurnFailed
from claw.db.models import AgentClient, AgentTurn
from claw.db.stores import AgentChannelStore, AuditStore, ConnectorStore, SessionStore, UserStore

# Built-ins safe for an untrusted caller: keeping a plan, and reading the
# procedures of skills the owner enabled.
SAFE_TOOLS = frozenset({"update_plan", "read_skill"})
# How long a request waits for the reply before answering 202 with a turn to poll.
WAIT_SECONDS = 55.0
TURN_RETENTION = timedelta(days=7)
# A turn still `working` after this long belonged to a process that is gone.
TURN_STALE_AFTER = timedelta(minutes=30)
# Each new conversation id opens a chat in the owner's list; this bounds it.
MAX_CONVERSATIONS = 50
_PRUNE_EVERY = timedelta(hours=1)
# How long a client's resolved tool names are reused before resolving again.
TOOL_CACHE_SECONDS = 60.0


class AgentBusy(Exception):
    """The client already has a turn running; one at a time per client."""


class ConversationLimit(Exception):
    """The client already has MAX_CONVERSATIONS conversations; reuse one."""


class AgentChannelService:
    name = "agent"

    def __init__(
        self,
        runtime: AgentRuntime,
        store: AgentChannelStore,
        sessions: SessionStore,
        users: UserStore,
        connectors: ConnectorStore,
        audit: AuditStore,
    ):
        self.runtime = runtime
        self.store = store
        self.sessions = sessions
        self.users = users
        self.connectors = connectors
        self.audit = audit
        self._busy: set[str] = set()
        # asyncio keeps only a weak reference to a task; a turn that outlives its
        # request would otherwise be collectable mid-run.
        self._tasks: set[asyncio.Task] = set()
        self._pruned_at: datetime | None = None
        self._tool_cache: dict[tuple, tuple[float, frozenset[str]]] = {}

    async def allowed_tools(self, client: AgentClient) -> frozenset[str]:
        """Tool names this client may call now: its connectors' current tools plus SAFE_TOOLS.

        Cached briefly per client: resolving means a connector sync and two
        connector-store reads, and the runtime syncs again inside the turn.
        """
        # Tool names are built from connector names, so the key carries the
        # current names: a deleted or renamed connector, or a new one reusing a
        # name, changes the key and forces a fresh resolve.
        current = await self.connectors.names_by_id(list(client.connector_ids or ()))
        key = (client.id, tuple(sorted(current.items())))
        cached = self._tool_cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < TOOL_CACHE_SECONDS:
            return cached[1]
        names = set(SAFE_TOOLS)
        manager = self.runtime.connectors
        if manager is not None and current:
            # Registers the owner's connector tools so their current names resolve.
            await self.runtime.warm_connectors(client.owner_id)
            owned = await self.connectors.list_for_user(client.owner_id)
            global_connectors = await self.connectors.list_for_global()
            for connector_id in current:
                names.update(
                    await manager.resolve_tool_names(
                        client.owner_id, connector_id, owned=owned, global_connectors=global_connectors
                    )
                    or []
                )
        allowed = frozenset(names)
        self._tool_cache = {k: v for k, v in self._tool_cache.items() if k[0] != client.id}
        self._tool_cache[key] = (now, allowed)
        return allowed

    async def send(
        self,
        client: AgentClient,
        conversation_id: str,
        message: str,
        locale: str = "en",
        idempotency_key: str | None = None,
    ) -> AgentTurn:
        """Run one message; returns the finished turn, or a `working` one to poll after WAIT_SECONDS."""
        await self._maybe_prune()
        await self.store.fail_stale_turns(datetime.now(timezone.utc) - TURN_STALE_AFTER, client.id)
        # The in-process set closes the race between two requests here; the
        # database check covers a turn another process is running.
        if client.id in self._busy or await self.store.has_running_turn(client.id):
            # A retry of the running message gets that turn back; anything else waits.
            existing = await self.store.find_turn(client.id, idempotency_key) if idempotency_key else None
            if existing is not None:
                return existing
            raise AgentBusy()
        if client.id in self._busy:
            raise AgentBusy()
        # Claimed before the next await, so two requests arriving together
        # cannot both get past the check above.
        self._busy.add(client.id)
        try:
            if (
                await self.store.session_for(client.id, conversation_id) is None
                and await self.store.count_conversations(client.id) >= MAX_CONVERSATIONS
            ):
                raise ConversationLimit()
            turn, created = await self.store.start_turn(client.id, conversation_id, idempotency_key)
        except BaseException:
            self._busy.discard(client.id)
            raise
        if not created:
            self._busy.discard(client.id)
            return turn
        task = asyncio.create_task(self._run(client, turn.id, conversation_id, message, locale))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=WAIT_SECONDS)
        except asyncio.TimeoutError:
            pass
        return await self.store.get_turn(client.id, turn.id) or turn

    async def _session(self, client: AgentClient, conversation_id: str) -> str:
        session_id = await self.store.session_for(client.id, conversation_id)
        if session_id:
            existing = await self.sessions.get(session_id)
            if existing is not None and existing.user_id == client.owner_id:
                return session_id
        created = await self.sessions.create(
            client.owner_id, title=f"{client.name} · {conversation_id}"[:255], channel=self.name
        )
        await self.store.bind_session(client.id, conversation_id, created.id)
        return created.id

    async def _run(self, client: AgentClient, turn_id: str, conversation_id: str, message: str, locale: str) -> None:
        session_id: str | None = None
        status, reply = "failed", "The message could not be processed."
        try:
            session_id = await self._session(client, conversation_id)
            tools = await self.allowed_tools(client)
            # A provider failure is not raised: the runtime publishes TurnError and
            # returns its message as the reply, so the bus is what tells failure
            # apart. The queue is read throughout, so streamed deltas never pile up.
            errors: list[TurnError] = []
            async with self.runtime.bus.subscribe(session_id) as events:
                while not events.empty():
                    events.get_nowait()  # replay of an earlier turn
                watcher = asyncio.create_task(_collect_errors(events, errors))
                try:
                    answer = await self.runtime.handle_message(
                        user_id=client.owner_id,
                        session_id=session_id,
                        content=message,
                        channel=self.name,
                        locale=locale,
                        allowed_tools=tools,
                    )
                    await asyncio.sleep(0)  # let the watcher read the last events
                finally:
                    watcher.cancel()
            if errors:
                reply = errors[-1].message
            else:
                status, reply = "completed", answer or ""
        except TurnFailed as exc:
            reply = exc.message
        except Exception:
            logger.exception("Agent channel turn {} failed", turn_id)
        finally:
            self._busy.discard(client.id)
            await self.store.finish_turn(turn_id, status, reply)
            await self.audit.log(
                "agent_channel.message",
                {"client_id": client.id, "client": client.name, "conversation_id": conversation_id,
                 "turn_id": turn_id, "status": status},
                user_id=client.owner_id,
                session_id=session_id,
            )

    async def _maybe_prune(self) -> None:
        now = datetime.now(timezone.utc)
        if self._pruned_at is not None and now - self._pruned_at < _PRUNE_EVERY:
            return
        self._pruned_at = now
        try:
            await self.store.prune_turns(now - TURN_RETENTION)
        except Exception:
            logger.exception("Agent channel could not prune old turns")




async def _collect_errors(events: "asyncio.Queue", errors: list[TurnError]) -> None:
    while True:
        event = await events.get()
        if isinstance(event, TurnError):
            errors.append(event)
