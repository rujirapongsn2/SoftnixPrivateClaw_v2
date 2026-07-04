"""Multi-tenant agent runtime: many users, many agents, one process.

Each user gets a lightweight ClawAgent (workspace + tools + persona), not an
OS process. Turns are serialized per session — never globally — so hundreds
of concurrent users share the event loop.
"""

import asyncio
import platform
import uuid
from collections import OrderedDict
from pathlib import Path

from loguru import logger

from claw.config import Settings
from claw.core.bus import EventBus
from claw.core.context import ContextAssembler, build_runtime_context, build_user_content
from claw.core.events import TurnCompleted, TurnError, TurnStarted
from claw.core.limits import RateLimiter
from claw.core.loop import AgentLoop
from claw.core.memory import MemoryService
from claw.browser.manager import BrowserManager
from claw.core.connectors import ConnectorManager
from claw.core.subagent import SubagentManager
from claw.core.scheduler import SchedulerService
from claw.db.stores import (
    AuditStore,
    MessageStore,
    ScheduleStore,
    SessionStore,
    SkillStore,
    UsageStore,
    UserStore,
)
from claw.i18n import classify_error_reason, t
from claw.providers.base import LLMProvider, ProviderError
from claw.sandbox.ephemeral import EphemeralSandbox
from claw.security.policy import Action, PolicyEngine
from claw.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from claw.tools.registry import ToolRegistry
from claw.tools.browser import BrowserTool
from claw.tools.documents import build_document_tools
from claw.tools.shell import ExecTool
from claw.tools.skills import ReadSkillTool, build_skills_summary
from claw.tools.spawn import SpawnTool
from claw.tools.web import WebFetchTool, WebSearchTool
from claw.tools.workflow import WorkflowTool
from claw.workflows.service import WorkflowService

_STORED_TOOL_RESULT_CAP = 4000


class ClawAgent:
    """Per-user agent: workspace, tools, and loop. Cheap to keep resident."""

    def __init__(
        self,
        user_id: str,
        workspace: Path,
        provider: LLMProvider,
        sandbox: EphemeralSandbox,
        settings: Settings,
        audit: AuditStore,
        skills: SkillStore | None = None,
        policy: PolicyEngine | None = None,
        browser: BrowserManager | None = None,
        schedules: "ScheduleStore | None" = None,
        scheduler: "SchedulerService | None" = None,
    ):
        self.user_id = user_id
        self.workspace = workspace
        self.policy = policy
        self.tools = ToolRegistry(on_execute=self._audit_tool)
        self._audit_store = audit
        for tool_cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(tool_cls(workspace))
        self.tools.register(ExecTool(sandbox, workspace))
        self.tools.register(WebFetchTool())
        self.tools.register(WebSearchTool())
        subagents = SubagentManager(
            provider=provider,
            sandbox=sandbox,
            workspace=workspace,
            model=settings.llm.model,
            max_tokens=settings.llm.max_tokens,
        )
        self.tools.register(SpawnTool(subagents))
        self.tools.register(
            WorkflowTool(WorkflowService(provider, subagents, model=settings.llm.model))
        )
        if browser is not None:
            self.tools.register(BrowserTool(browser, user_id))
        if skills is not None:
            self.tools.register(ReadSkillTool(skills, user_id))
        if schedules is not None:
            from claw.tools.schedule import ScheduleTool

            self.tools.register(ScheduleTool(schedules, scheduler, user_id))
        for doc_tool in build_document_tools(workspace):
            self.tools.register(doc_tool)
        self.loop = AgentLoop(
            provider=provider,
            tools=self.tools,
            model=settings.llm.model,
            max_iterations=settings.llm.max_iterations,
            max_tokens=settings.llm.max_tokens,
            temperature=settings.llm.temperature,
            arg_guard=self._guard_tool_args if policy is not None else None,
        )

    def _guard_tool_args(self, tool_name: str, args: dict) -> tuple[dict, str | None]:
        """Apply the control policy to each string argument before a tool runs."""
        if self.policy is None:
            return args, None
        guarded = dict(args)
        for key, value in args.items():
            if not isinstance(value, str) or not value:
                continue
            decision = self.policy.enforce(value, scope="tool_args")
            if decision.blocked:
                return args, decision.message
            if decision.masked:
                guarded[key] = decision.text
        return guarded, None

    def _audit_tool(self, name: str, params: dict, result: str) -> None:
        payload = {"tool": name, "params_preview": str(params)[:500], "result_preview": result[:500]}
        try:
            asyncio.get_running_loop().create_task(
                self._audit_store.log("tool_call", payload, user_id=self.user_id)
            )
        except RuntimeError:
            pass

    def system_prompt(self, memory_context: str, persona: str = "", skills_summary: str = "") -> str:
        runtime = f"{platform.system()} {platform.machine()}, Python {platform.python_version()}"
        parts = [
            "# Claw Agent\n\n"
            "You are Claw, the user's personal AI agent — a careful, reliable partner "
            "that finishes what it starts.\n\n"
            f"## Runtime\n{runtime}\n\n"
            f"## Workspace\nYour workspace is mounted for file tools; shell commands run "
            f"in an isolated sandbox with the same workspace at /workspace.\n\n"
            "## Guidelines\n"
            "- State intent before tool calls; never claim results before receiving them.\n"
            "- Read a file before modifying it. Analyze tool errors before retrying.\n"
            "- Ask for clarification when the request is ambiguous.\n"
            "- Match the user's language in your replies."
        ]
        if persona:
            parts.append(f"# Persona\n\n{persona}")
        if memory_context:
            parts.append(memory_context)
        if skills_summary:
            parts.append(skills_summary)
        return "\n\n---\n\n".join(parts)


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider,
        bus: EventBus,
        users: UserStore,
        sessions: SessionStore,
        messages: MessageStore,
        memory: MemoryService,
        audit: AuditStore,
        skills: SkillStore | None = None,
        connectors: ConnectorManager | None = None,
        policy: PolicyEngine | None = None,
        browser: BrowserManager | None = None,
        usage: "UsageStore | None" = None,
        schedules: ScheduleStore | None = None,
        scheduler: SchedulerService | None = None,
    ):
        self.settings = settings
        self.provider = provider
        self.bus = bus
        self.users = users
        self.sessions = sessions
        self.messages = messages
        self.memory = memory
        self.audit = audit
        self.skills = skills
        self.connectors = connectors
        self.policy = policy
        self.browser = browser
        self.usage = usage
        self.schedules = schedules
        self.scheduler = scheduler
        self.sandbox = EphemeralSandbox(settings.sandbox)
        self.assembler = ContextAssembler(
            token_counter=provider.count_tokens,
            max_context_tokens=settings.llm.max_context_tokens,
        )
        # LRU-bounded so a gateway serving many distinct users cannot grow without
        # limit; an evicted agent is just a reloadable in-memory object.
        self._agents: "OrderedDict[str, ClawAgent]" = OrderedDict()
        self._session_locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()
        self._rate_limiter = RateLimiter(settings.turns_per_minute)
        self._background: set[asyncio.Task] = set()
        self._inflight = 0

    def get_agent(self, user_id: str) -> ClawAgent:
        agent = self._agents.get(user_id)
        if agent is not None:
            self._agents.move_to_end(user_id)
            return agent
        workspace = self.settings.workspaces_root / user_id
        agent = ClawAgent(
            user_id=user_id,
            workspace=workspace,
            provider=self.provider,
            sandbox=self.sandbox,
            settings=self.settings,
            audit=self.audit,
            skills=self.skills,
            policy=self.policy,
            browser=self.browser,
            schedules=self.schedules,
            scheduler=self.scheduler,
        )
        self._agents[user_id] = agent
        self._agents.move_to_end(user_id)
        while len(self._agents) > self.settings.max_resident_agents:
            self._agents.popitem(last=False)
        return agent

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        self._session_locks.move_to_end(session_id)
        # Prune oldest locks that aren't currently held, keeping the map bounded.
        if len(self._session_locks) > self.settings.max_session_locks:
            for key in list(self._session_locks.keys()):
                if len(self._session_locks) <= self.settings.max_session_locks:
                    break
                if key != session_id and not self._session_locks[key].locked():
                    del self._session_locks[key]
        return lock

    async def handle_message(
        self,
        user_id: str,
        session_id: str,
        content: str,
        channel: str = "web",
        locale: str = "en",
        media: list[str] | None = None,
    ) -> str | None:
        """Process one user message; tracks in-flight count for graceful shutdown."""
        self._inflight += 1
        try:
            return await self._process_turn(user_id, session_id, content, channel, locale, media)
        finally:
            self._inflight -= 1

    async def drain(self, timeout: float = 20.0) -> None:
        """Wait for in-flight turns and background tasks to finish (graceful shutdown)."""
        import time as _time

        deadline = _time.monotonic() + timeout
        while (self._inflight > 0 or self._background) and _time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def _process_turn(
        self,
        user_id: str,
        session_id: str,
        content: str,
        channel: str = "web",
        locale: str = "en",
        media: list[str] | None = None,
    ) -> str | None:
        """Process one user message; events stream to the bus, messages persist to DB.

        Returns the final assistant content (for non-streaming callers/tests).
        """
        turn_id = uuid.uuid4().hex[:12]

        # Per-user rate limit — reject before doing any work or calling the model.
        if not self._rate_limiter.allow(user_id):
            msg = t("error.rate_limited", locale)
            self.bus.publish(session_id, TurnStarted(turn_id=turn_id))
            self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
            return msg

        agent = self.get_agent(user_id)

        # Enforce the control policy on the way in. Blocked input never reaches
        # the model; masked input is what we send AND store (raw PII is not persisted).
        stored_content = content
        if self.policy is not None:
            decision = self.policy.enforce(content, scope="input")
            if decision.matched_rules:
                await self.audit.log(
                    "policy",
                    {"scope": "input", "action": decision.action, "rules": decision.matched_rules},
                    user_id=user_id,
                    session_id=session_id,
                )
            if decision.blocked:
                msg = decision.message or "Request blocked by the control policy."
                self.bus.publish(session_id, TurnStarted(turn_id=turn_id))
                self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
                await self.messages.append(session_id, [{"role": "user", "content": stored_content}])
                return msg
            if decision.masked:
                content = decision.text
                stored_content = decision.text

        async with self._session_lock(session_id):
            self.bus.publish(session_id, TurnStarted(turn_id=turn_id))
            session = await self.sessions.get(session_id)
            after_seq = session.last_consolidated_seq if session else 0
            history = await self.messages.recent(session_id, after_seq=after_seq)
            memory_context = await self.memory.build_context(user_id)
            skills_summary = ""
            if self.skills is not None:
                skills_summary = build_skills_summary(await self.skills.enabled_for_user(user_id))
            if self.connectors is not None:
                try:
                    await self.connectors.sync_tools(user_id, agent.tools)
                except Exception:
                    logger.exception("Connector sync failed for {}", user_id)
            runtime_ctx = build_runtime_context(channel, locale)
            model_content, storage_text = build_user_content(content, media, agent.workspace)
            if isinstance(model_content, str):
                user_message = {"role": "user", "content": f"{runtime_ctx}\n\n{model_content}"}
            else:
                # Multimodal: prepend the runtime context as a leading text block.
                user_message = {
                    "role": "user",
                    "content": [{"type": "text", "text": runtime_ctx}, *model_content],
                }
            stored_content = storage_text  # text + attachment names (never base64)
            prompt_messages = self.assembler.assemble(
                agent.system_prompt(memory_context, skills_summary=skills_summary),
                history,
                user_message,
            )

            try:
                outcome = await agent.loop.run_turn(
                    turn_id, prompt_messages, lambda ev: self.bus.publish(session_id, ev)
                )
            except ProviderError as exc:
                reason = t(classify_error_reason(str(exc)), locale)
                message = t("error.llm", locale, reason=reason)
                self.bus.publish(session_id, TurnError(turn_id=turn_id, message=message))
                # Persist only the user message — never error text — so a bad
                # provider response cannot poison future context (legacy #1303).
                await self.messages.append(session_id, [{"role": "user", "content": stored_content}])
                return message

            final = outcome.final_content
            if outcome.reached_max_iterations and not final:
                final = t("error.max_iterations", locale)

            # Enforce policy on the model's final output before it leaves the system.
            if self.policy is not None and final:
                out_decision = self.policy.enforce(final, scope="output")
                if out_decision.blocked:
                    final = out_decision.message or "Response withheld by the control policy."
                elif out_decision.masked:
                    final = out_decision.text

            to_store = [{"role": "user", "content": stored_content}]
            for msg in outcome.new_messages:
                entry = dict(msg)
                if entry.get("role") == "tool" and len(entry.get("content") or "") > _STORED_TOOL_RESULT_CAP:
                    entry["content"] = entry["content"][:_STORED_TOOL_RESULT_CAP] + "\n... (truncated)"
                to_store.append(entry)
            await self.messages.append(session_id, to_store)

            self.bus.publish(
                session_id,
                TurnCompleted(turn_id=turn_id, content=final or "", usage=outcome.usage),
            )

        if self.usage is not None:
            self._spawn_background(
                self.usage.record(user_id, session_id, self.settings.llm.model, outcome.usage)
            )
        self._spawn_background(self.memory.maybe_consolidate(user_id, session_id))
        return final

    def _spawn_background(self, coro) -> None:
        task = asyncio.create_task(self._guard(coro))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    async def _guard(coro) -> None:
        try:
            await coro
        except Exception:
            logger.exception("Background task failed")
