"""Run one specialist bot's turn.

Both paths that put a specialist to work — Chief of Staff's `delegate` tool and
a mission node — need the same four things: the bot's allowlisted tools, its
resolved model/credentials, a wall-clock budget, and its token cost back. Kept
here so the mission scheduler does not grow a second, drifting copy of the
model-resolution logic that a bot's behaviour depends on.
"""

import asyncio
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from sbot.core.builtin_skills import builtin_skills
from sbot.core.events import DelegatedTask, TurnCompleted, TurnError, TurnStarted
from sbot.core.keyed_locks import KeyedLocks
from sbot.core.loop import AgentLoop
from sbot.core.memory import memory_scope
from sbot.core.turn_context import current_turn_deadline
from sbot.tools.documents import build_document_tools
from sbot.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from sbot.tools.memory import MemoryTool, RecallMemoryTool
from sbot.tools.project import ProjectTool
from sbot.tools.registry import ToolRegistry
from sbot.tools.shell import ExecTool
from sbot.tools.skills import ReadSkillTool, build_skills_summary, scope_skills
from sbot.tools.web import WebFetchTool, WebSearchTool

# The capabilities a specialist's `tool_allowlist` chooses from. Chief of Staff
# is an LLM, so an allowlist it proposes is untrusted input: anything outside
# this set is rejected when the bot is created rather than silently dropped
# later. Deliberately not the whole tool set — see `build_tools`.
DELEGATABLE_TOOLS = frozenset(
    {"read_file", "write_file", "edit_file", "list_dir", "exec", "project", "web_fetch", "web_search"}
)
SPECIALIST_ALWAYS_TOOLS = frozenset({'publish_artifact', 'read_docx', 'read_excel', 'read_csv', 'read_pdf'})

_DEFAULT_BUDGET_SECONDS = 300.0

# How much of a specialist's reply accumulates before it is forwarded to its
# assignment card. Per-token forwarding would put thousands of events through a
# session queue that is bounded and shared — with `delegate_many` four streams
# compete for it, and the loser's tool events are what get evicted.
_DELTA_FLUSH_CHARS = 200


def _budget_notice(seconds: float, iterations: int, structured: bool = False) -> str:
    """Tell the specialist what it is spending.

    Without this a specialist plans as if time were free: one run spent its
    whole budget on two dozen searches and a large file write, was cut off
    mid-stream, and returned nothing at all. Both limits are enforced only by a
    hard stop, so the bot has to know the numbers to spend them well.
    """
    # `iterations` is the loop's provider round-trip count, and the round that
    # delivers the reply spends one of them — so the rounds that can carry tool
    # calls are one fewer. Advertising the raw number told the model it could
    # plan that many tool steps, which left it no round in which to answer:
    # the loop falls through with no content, which is the failure this notice
    # exists to prevent.
    tool_rounds = max(1, iterations - 1)
    notice = (
        "# Budget\n"
        f"You have about {int(seconds)} seconds of wall-clock time, and at most "
        f"{tool_rounds} rounds of tool calls before you must give your reply "
        f"({iterations} model turns in total). When either limit runs out you are "
        "cut off and whatever you have not yet said is lost.\n"
        "- Always finish with your findings written out in your reply. A partial "
        "answer that arrives beats a complete one that gets cut off.\n"
        "- Gather only what you need. Stop researching once you can answer.\n"
        "- Writing large files is expensive and its content comes back into your "
        "context. Only write a file if the task asked for one, and put the findings "
        "in your reply too."
    )
    if structured:
        notice = notice[:notice.index("- Always finish")]
        notice += (
            "Save requested deliverables incrementally and reserve time to verify them. "
            "Call finish_step with evidence and file paths. For file tasks, keep its summary "
            "brief rather than copying the deliverable into the reply. For text tasks, "
            "include the actual result in summary. Report unfinished work honestly."
        )
    return notice



@dataclass(slots=True)
class _SpecialistSink:
    """A progress sink that can also be told the run is over.

    Text is buffered before it is forwarded, so somebody has to say when there
    will be no more of it. Without that, a specialist whose whole reply is
    shorter than one flush and that reached for no tool along the way emitted
    nothing at all, and its assignment card showed a spinner and then an answer
    with no working-out in between.
    """

    emit: Callable[[Any], None]
    flush: Callable[[], None]

    def __call__(self, event: Any) -> None:
        self.emit(event)


def specialist_progress(
    progress: Any,
    bot_name: str,
    next_index: Any = None,
    delegation: dict[str, str] | None = None,
) -> _SpecialistSink:
    """Bridge a nested specialist's loop events onto the caller's progress sink.

    Two sinks, from one stream of events. The Execution panel gets tool-shaped
    substeps, as it always has. When `delegation` names the assignment this run
    belongs to (`delegation_id` + `bot_id`), the specialist's own activity is
    forwarded under that card too — the tools it runs with their arguments and
    results, and its reply as it writes it.

    Text still never reaches the panel's substeps, and reaches the card only
    tagged with the delegation it came from. That tag is the whole reason it can
    be forwarded at all: untagged, a second bot's stream lands in the delegating
    bot's own reply and the two voices interleave, which is why this used to
    drop text outright.

    `next_index` hands out substep numbers on behalf of several specialists that
    share one sink (`delegate_many`). They all report into the same tool row,
    and the panel keys a substep by its index alone — so with each of them
    counting from 1 privately, their rows land on top of each other and one
    bot's finished step marks another's as done.
    """
    if progress is None:
        return _SpecialistSink(lambda _event: None, lambda: None)

    # Substeps are keyed by index downstream, so without one every tool the
    # specialist runs collapses onto step 0 and the panel shows a single row
    # rewriting itself. 1-based: index 0 reads as "not a numbered step".
    # `total` stays 0 — a specialist plans as it goes and has no step count to
    # promise. A specialist's own loop runs its tool calls in sequence, so
    # `tool_finished` always belongs to the step this bot started last.
    counter = itertools.count(1)
    allocate = next_index if callable(next_index) else lambda: next(counter)
    step = 0
    # Numbered per assignment, unlike `step`: the card lists only this bot's own
    # work, so it counts from 1 no matter who else is running.
    own_steps = itertools.count(1)
    own_step = 0
    pending: list[str] = []

    def card(payload: dict[str, Any]) -> None:
        if delegation is not None:
            progress({**payload, **delegation})

    def flush_text() -> None:
        if not pending:
            return
        text = "".join(pending)
        pending.clear()
        card({"kind": "delegation_delta", "text": text})

    def emit(event: Any) -> None:
        nonlocal step, own_step
        kind = getattr(event, "type", "")
        if kind == "tool_started":
            # Whatever the specialist had written before reaching for a tool is
            # its reasoning about why — worth showing, and it would otherwise
            # sit in the buffer until enough more arrived to trip the flush.
            flush_text()
            step = allocate()
            own_step = next(own_steps)
            progress({
                "label": f"{bot_name}: {event.tool}",
                "stage": "step",
                "index": step,
                "status": "running",
            })
            card({
                "kind": "delegation_step",
                "index": own_step,
                "tool": event.tool,
                "detail": event.args_preview,
                "status": "running",
            })
        elif kind == "tool_finished":
            progress({
                "label": f"{bot_name}: {event.tool}",
                "stage": "step",
                "index": step,
                "status": "error" if event.is_error else "done",
            })
            card({
                "kind": "delegation_step",
                "index": own_step,
                "tool": event.tool,
                "detail": event.result_preview,
                "status": "error" if event.is_error else "done",
            })
        elif kind == "text_delta":
            pending.append(event.text)
            if sum(len(chunk) for chunk in pending) >= _DELTA_FLUSH_CHARS:
                flush_text()

    return _SpecialistSink(emit, flush_text)


class DelegationMirror:
    """Show a delegated run in the specialist's own chat thread, as it happens.

    A delegated specialist has no session: it is a nested loop inside the
    leader's turn, and the event bus is keyed by session, so everything the bot
    did was only ever visible on the leader's screen. Clicking that bot in the
    sidebar opened its thread and found nothing — which is the one place a user
    looks to see what it is doing.

    Shaped as an ordinary turn (the instruction, then the loop's own events,
    then the reply) rather than as a new kind of transcript row: the chat then
    renders it with no special case, and what the user watches is the same view
    they would get by asking the bot directly.

    Every method swallows its own failures. This is a second view of work that
    is already being delivered elsewhere — a thread that cannot be written must
    not take the delegation down with it.
    """

    def __init__(
        self,
        bus: Any,
        sessions: Any,
        messages: Any,
        user_id: str,
        leader_bot_id: str | None = None,
        lock_for: Callable[[str], asyncio.Lock] | None = None,
    ):
        self.bus = bus
        self.sessions = sessions
        self.messages = messages
        self.user_id = user_id
        self.leader_bot_id = leader_bot_id
        # The runtime's per-session turn lock, so a mirrored run and a message
        # the user types into the same thread cannot be in progress at once.
        # A private one when there is no runtime to borrow from (missions,
        # tests): the guard is then only against this mirror's own runs, which
        # is all there is to collide with.
        self._own_locks = KeyedLocks()
        self._lock_for = lock_for or self._own_locks.get
        # Locks acquired by a run still in progress, so `close` can let go of
        # the one it took without re-deriving it.
        self._held: dict[str, asyncio.Lock] = {}

    async def open(self, bot: Any, task: str, turn_id: str) -> str | None:
        """Claim the bot's thread for this run and record what it was asked.

        None means there is no mirror for this run: either the thread could not
        be reached, or it is busy with another one. The work is still fully
        visible on the leader's side, which is where it was asked for.

        The instruction is stored before the turn opens, not with the reply at
        the end, because the moment a user comes looking is while the bot is
        still working — a thread that shows tool activity under no visible
        instruction is the wrong half to have.
        """
        try:
            session = await self.sessions.thread_for_bot(self.user_id, bot.id, title=bot.name)
        except Exception:
            logger.exception("Could not open a thread to mirror {} into", bot.name)
            return None
        lock = self._lock_for(session.id)
        # Tried, not waited for. A busy thread gets no mirror rather than a
        # second conversation shuffled into the first, and waiting would hold
        # this delegation behind whatever else that bot is doing. `locked()`
        # and an uncontended `acquire()` are one step: neither yields, so
        # nothing can take the lock in between.
        if lock.locked():
            return None
        await lock.acquire()
        self._held[session.id] = lock
        try:
            await self.messages.append(
                session.id,
                [{
                    "role": "user",
                    "content": task,
                    "meta": {"delegated_by": self.leader_bot_id},
                }],
            )
        except Exception:
            logger.exception("Could not record the instruction given to {}", bot.name)
            self._release(session.id)
            return None
        self.bus.publish(session.id, TurnStarted(turn_id=turn_id))
        self.bus.publish(
            session.id,
            DelegatedTask(turn_id=turn_id, content=task, delegated_by=self.leader_bot_id or ""),
        )
        return session.id

    def relay(self, session_id: str, event: Any) -> None:
        self.bus.publish(session_id, event)

    async def close(
        self,
        session_id: str,
        turn_id: str,
        text: str,
        is_error: bool,
        artifacts: list[str] | None = None,
        result: dict | None = None,
    ) -> None:
        """End the mirrored turn, whatever happened to the run.

        Reachable from a cancellation path, so the reply is stored behind a
        shield and the turn is ended from a `finally`: a cancelled write must
        not cost the client its `turn_completed`, which is the only thing that
        stops the spinner, or leave the thread claimed for good.
        """
        try:
            try:
                await asyncio.shield(
                    self.messages.append(
                        session_id,
                        [{
                            "role": "assistant",
                            "content": text,
                            "meta": {"delegated": True, "artifacts": artifacts or [],
                                     "speaker_error": is_error,
                                     **({"task_result": result} if result is not None else {})},
                        }],
                    )
                )
            except Exception:
                logger.exception("Could not record a delegated reply in its own thread")
        finally:
            if is_error:
                self.bus.publish(session_id, TurnError(turn_id=turn_id, message=text))
            else:
                self.bus.publish(
                    session_id,
                    TurnCompleted(turn_id=turn_id, content=text, artifacts=artifacts or []),
                )
            self._release(session_id)

    def _release(self, session_id: str) -> None:
        lock = self._held.pop(session_id, None)
        if lock is not None and lock.locked():
            lock.release()


@dataclass(slots=True)
class SpecialistOutcome:
    text: str
    # {"tokens": int} — what the mission budget is charged.
    cost: dict[str, float] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    timed_out: bool = False
    # The other way the nested loop stops without an answer: it used every
    # provider round it was given. Carried separately from `timed_out` because
    # the caller has to be able to tell a cut-off run from a finished one, and
    # dropping this flag made an exhausted specialist indistinguishable from a
    # successful one — the caller then reported success and redid the work.
    reached_max_iterations: bool = False
    output_truncated: bool = False

    @property
    def cut_off(self) -> bool:
        """True when the run stopped without the specialist getting to answer."""
        return self.timed_out or self.reached_max_iterations or self.output_truncated


class SpecialistRunner:
    def __init__(
        self,
        provider: Any,
        sandbox: Any,
        workspace: Any,
        model: str | None = None,
        llm_config: Any = None,
        owner_id: str | None = None,
        skills: Any = None,
        memory: Any = None,
        connectors: Any = None,
        project_access: Any = None,
        arg_guard: Any = None,
        llm_settings: Any = None,
    ):
        from sbot.config import LLMSettings
        self.llm_settings = llm_settings or LLMSettings()
        self.provider = provider
        self.sandbox = sandbox
        self.workspace = workspace
        self.model = model
        self.llm_config = llm_config
        self.owner_id = owner_id
        self.skills = skills
        self.memory = memory
        self.connectors = connectors
        self.project_access = project_access
        self.arg_guard = arg_guard

    def build_tools(
        self, allowlist: list[str] | None, extra: list[Any] | None = None, bot: Any = None
    ) -> ToolRegistry:
        all_tools = {
            "read_file": ReadFileTool(self.workspace),
            "write_file": WriteFileTool(self.workspace),
            "edit_file": EditFileTool(self.workspace),
            "list_dir": ListDirTool(self.workspace),
            "exec": ExecTool(self.sandbox, self.workspace),
            "project": ProjectTool(self.sandbox, self.workspace, self.owner_id, self.project_access),
            "web_fetch": WebFetchTool(),
            "web_search": WebSearchTool(),
        }
        assert set(all_tools) == DELEGATABLE_TOOLS, "delegatable tool set drifted"
        registry = ToolRegistry()
        from sbot.tools.artifacts import PublishArtifactTool
        registry.register(PublishArtifactTool(self.workspace))
        for tool in build_document_tools(self.workspace):
            registry.register(tool)
        for name, tool in all_tools.items():
            if allowlist is None or name in allowlist:
                registry.register(tool)
        # The bot's own machinery, granted regardless of its allowlist for the
        # same reason `ALWAYS_AVAILABLE_TOOLS` protects them in direct chat: a
        # specialist's memory is not a capability it can be configured out of.
        # Registering them only there was a guarantee about nothing — the
        # specialist path never built them, so a mission node could neither read
        # the skills it was assigned nor record what it learned, which is the
        # substrate per-bot learning needs (PRD §4.5/§7).
        #
        # `update_plan` is deliberately still absent: it writes the *session's*
        # plan, and a delegated specialist runs inside the caller's turn, so it
        # would overwrite Chief of Staff's plan. A mission node has the graph
        # instead, and no session to attach one to at all.
        if self.memory is not None and self.owner_id:
            own_doc = memory_scope(getattr(bot, "id", None), getattr(bot, "kind", "") == "chief_of_staff")
            registry.register(MemoryTool(self.memory, self.owner_id, bot_id=own_doc))
            registry.register(RecallMemoryTool(self.memory, self.owner_id))
        if self.skills is not None and self.owner_id:
            registry.register(ReadSkillTool(self.skills, self.owner_id))
        # Not allowlist-gated either: these are granted by the caller (the
        # mission scheduler), not proposed by the bot's own configuration.
        for tool in extra or []:
            registry.register(tool)
        return registry

    async def context_block(self, bot: Any) -> str:
        """The skills and long-term memory a specialist starts its turn with.

        A specialist used to be handed only its charter and the task, so an
        assigned skill was invisible to it and its own memory document was
        write-only — it could save a lesson and never see it again.
        """
        parts: list[str] = []
        if self.skills is not None and self.owner_id:
            own = await self.skills.enabled_for_user(self.owner_id)
            # Built-ins are merged the same way the chat turn merges them, and a
            # user skill of the same name shadows rather than joins the built-in
            # — `read_skill` checks the store first, so advertising both would
            # describe content the tool can never return.
            own_names = {s.name for s in own}
            scoped = scope_skills(
                [*(b for b in builtin_skills() if b.name not in own_names), *own],
                getattr(bot, "skill_ids", None),
            )
            summary = build_skills_summary(scoped)
            if summary:
                parts.append(summary)
        if self.memory is not None and self.owner_id:
            block = await self.memory.build_context(
                self.owner_id,
                bot_id=memory_scope(
                    getattr(bot, "id", None), getattr(bot, "kind", "") == "chief_of_staff"
                ),
            )
            if block.strip():
                parts.append(block)
        return "\n\n".join(parts)

    async def _resolve_model(self, preferred: str | None) -> dict[str, Any]:
        effective = preferred or self.model
        resolved = {"model": effective, "api_key": None, "api_base": None, "context_window": None}
        if self.llm_config is None:
            return resolved
        found = await self.llm_config.resolve(effective, self.owner_id) if effective else None
        if found is None:
            default_id = await self.llm_config.default_model_for(None)
            if default_id:
                found = await self.llm_config.resolve(default_id, self.owner_id)
        if found is not None:
            resolved = {
                "model": found["model_id"],
                "api_key": found["api_key"] or None,
                "api_base": found["api_base"] or None,
                "context_window": found["context_window"],
            }
        return resolved

    def _budget(self, max_seconds: float) -> float:
        """Never outlive the enclosing turn's deadline, if there is one — a
        delegated specialist runs inside the caller's turn."""
        deadline = current_turn_deadline.get()
        if deadline is None:
            return max_seconds
        return max(0.001, min(max_seconds, deadline - time.monotonic()))

    async def run(
        self,
        bot: Any,
        system_prompt: str,
        user_prompt: str,
        emit: Any,
        turn_id: str,
        max_seconds: float = _DEFAULT_BUDGET_SECONDS,
        max_iterations: int | None = None,
        extra_tools: list[Any] | None = None,
        budget_notice: bool = True,
    ) -> SpecialistOutcome:
        max_iterations = max_iterations or self.llm_settings.max_iterations
        model = await self._resolve_model(getattr(bot, "model", None))
        budget = self._budget(max_seconds)
        tools = self.build_tools(bot.tool_allowlist, extra_tools, bot=bot)
        if self.connectors is not None and self.owner_id:
            await self.connectors.sync_tools(self.owner_id, tools)
            # Apply after sync too: newly discovered connector tools must never
            # widen an explicit bot allowlist. Mission tools are caller grants.
            if bot.tool_allowlist is not None:
                tools.restrict_to([*bot.tool_allowlist, *(t.name for t in extra_tools or [])])
        budget = self._budget(max_seconds)
        if current_turn_deadline.get() is not None and current_turn_deadline.get() <= time.monotonic():
            return SpecialistOutcome(text="Parent turn deadline expired.", timed_out=True)
        loop = AgentLoop(
            provider=self.provider,
            tools=tools,
            model=model["model"],
            max_iterations=max_iterations,
            max_tokens=self.llm_settings.max_tokens,
            max_context_chars=self.llm_settings.max_context_tokens,
            # Delegated bots use the same user workspace as their leader. Pass
            # it into the loop as well as their tools so files created by an
            # `exec` or `project` command are detected and returned as
            # downloadable artifacts, rather than remaining invisible on disk.
            workspace=self.workspace,
            max_turn_seconds=budget,
            arg_guard=self.arg_guard,
        )
        # Opt-out, not unconditional: a caller whose prompt reserves "answer in
        # text instead of calling the tool" as a distinct signal cannot also
        # carry the notice's "always finish with your findings in your reply"
        # (see the mission replan prompt) — the two instructions contradict.
        if budget_notice:
            structured = getattr(tools.get("finish_step"), "require_record", False)
            system_prompt = f"{system_prompt}\n\n{_budget_notice(budget, max_iterations, structured)}"
        outcome = await loop.run_turn(
            turn_id,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            emit,
            model=model["model"],
            api_key=model["api_key"],
            api_base=model["api_base"],
            context_window=model["context_window"],
        )
        usage = outcome.usage or {}
        return SpecialistOutcome(
            text=outcome.final_content or "",
            cost={
                "tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
            },
            artifacts=list(outcome.artifacts or []),
            timed_out=outcome.timed_out,
            reached_max_iterations=outcome.reached_max_iterations,
            output_truncated=getattr(outcome, 'finish_reason', 'stop') in ('length', 'max_tokens', 'plan_incomplete'),
        )
