"""Multi-tenant agent runtime: many users, many agents, one process.

Each user gets a lightweight ClawAgent (workspace + tools + persona), not an
OS process. Turns are serialized per session — never globally — so hundreds
of concurrent users share the event loop.
"""

import asyncio
import platform
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from sbot.browser.broker import BrowserBrokerStore
from sbot.browser.manager import BrowserManager
from sbot.config import Settings
from sbot.core.builtin_skills import builtin_skills
from sbot.core.bus import EventBus
from sbot.core.connector_presets import list_presets
from sbot.core.connectors import ConnectorManager
from sbot.core.context import (
    ContextAssembler,
    PromptSection,
    build_runtime_context,
    build_user_content,
    render_plan,
    swap_images_for_description,
    vision_note,
)
from sbot.core.events import (
    DelegationFinished,
    DelegationStarted,
    ToolConfirmRequest,
    ToolConfirmResolved,
    TurnCompleted,
    TurnError,
    TurnStarted,
)
from sbot.core.limits import RateLimiter
from sbot.core.loop import AgentLoop
from sbot.core.memory import MemoryService, memory_scope
from sbot.core.scheduler import SchedulerService
from sbot.core.specialist import DelegationMirror
from sbot.core.subagent import SubagentManager
from sbot.core.turn_context import current_session_id, current_turn_id, current_turn_deadline, current_turn_locale
from sbot.db.stores import (
    AuditStore,
    BlueprintStore,
    KnowledgeStore,
    MessageStore,
    PolicyPlanStore,
    ScheduleStore,
    SessionStore,
    SkillStore,
    UsageStore,
    UserStore,
)
from sbot.i18n import classify_error_reason, is_no_tool_support_error, is_no_vision_support_error, t
from sbot.providers.base import LLMProvider, ProviderError
from sbot.providers.registry import supports_vision as model_supports_vision
from sbot.sandbox.ephemeral import EphemeralSandbox
from sbot.security.policy import PolicyEngine
from sbot.tools.blueprint import SaveBlueprintTool
from sbot.tools.browser import BrowserTool
from sbot.tools.documents import build_document_tools
from sbot.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from sbot.tools.knowledge import SearchKnowledgeTool
from sbot.tools.memory import MemoryTool, RecallMemoryTool
from sbot.tools.plan import PlanTool
from sbot.tools.project import ProjectTool
from sbot.tools.registry import ToolRegistry
from sbot.tools.shell import ExecTool
from sbot.tools.skills import ManageSkillTool, ReadSkillTool, build_skills_summary, scope_skills
from sbot.tools.spawn import SpawnTool
from sbot.tools.web import WebFetchTool, WebSearchTool
from sbot.tools.workflow import WorkflowTool
from sbot.workflows.service import WorkflowService

if TYPE_CHECKING:
    from sbot.core.missions import MissionService
    from sbot.db.stores import BotStore, LLMConfigStore

_STORED_TOOL_RESULT_CAP = 4000

# How many specialists the Chief of Staff's prompt names before the rest are
# left to `list_bots`. A user may own up to MAX_BOTS_PER_OWNER of them, and a
# roster that long would cost more of the window than knowing about the tail is
# worth — the leader picks from the ones it can see.
_TEAM_ROSTER_LIMIT = 20

# How much of a teammate's charter the roster shows. Enough to route a request
# to the right specialist; the whole charter is one `list_bots` call away.
_CHARTER_GIST_CHARS = 160

# How many filenames a "no answer, but files were produced" message names before
# collapsing to a "+N" count — a long run can touch dozens, and an unbounded list
# would bury the explanation it is appended to.
_FALLBACK_ARTIFACTS_SHOWN = 8

# How long an "ask"-mode tool waits for the user's approve/deny before it is
# auto-declined, so a turn can never hang forever on an unanswered card.
_CONFIRM_TIMEOUT_SECONDS = 600

# Vision delegation: when the chat model can't accept images, the operator's
# kind="vision" model reads them once and the chat model answers from that
# description. These bounds keep a delegated turn from costing more than the
# turn it stands in for — the images are already capped at ~8MB each by
# build_user_content, but nothing capped how many of them, how long the reader
# may run, or how much it may write. The image cap matches the upload cap
# (_MAX_ATTACHMENTS in claw/api/routes.py) so a normal upload is never silently
# half-read; when a cap does bite, vision_note() tells the chat model so.
_VISION_MAX_IMAGES = 8
_VISION_MAX_TOKENS = 1500
_VISION_TIMEOUT_SECONDS = 90.0
_VISION_PROMPT = (
    "Describe the attached image(s) in full, factual detail for a colleague who "
    "cannot see them. Transcribe any text, numbers, table values, labels, code, or "
    "error messages exactly as they appear. Describe charts by their data, not just "
    "their shape. Do not interpret, advise, or answer any question in the image — "
    "only report what is visible. If something is unreadable, say so."
)


def _charter_gist(charter: str) -> str:
    """A teammate's charter shortened to one roster line."""
    lines = charter.strip().splitlines()
    first = lines[0].strip() if lines else ""
    if len(first) > _CHARTER_GIST_CHARS:
        return first[:_CHARTER_GIST_CHARS].rstrip() + "…"
    return first


class _VisionBlocked(Exception):
    """The control policy blocked what the vision model read out of an attached
    image. Carried as an exception so the turn refuses with the guardrail's own
    message rather than the generic "this model can't see images" — the two are
    different problems and only one is fixed by switching models."""


class TurnFailed(Exception):
    """Raised when a turn hits an unexpected (non-ProviderError) exception.

    A TurnError event has already been published to the bus by the time this
    is raised, so WebSocket/Telegram callers don't need it — it exists so
    callers that branch on success/failure by return value (SchedulerService,
    HeartbeatService) see a real failure instead of a truthy "ok" string.
    Carries the already-localized, user-facing message.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _TranscriptSaveError(Exception):
    """Marks a SQLAlchemyError as specifically coming from a transcript
    write, not from some other DB access nested inside the turn (audit log,
    a tool querying the DB, …) — those get the generic error.llm message
    instead of a misleading "your message/answer wasn't saved".

    `stage` distinguishes the two save points: "request" is the user's own
    message, persisted before the model is ever called, vs. "answer" (the
    default) for the assistant/tool messages persisted after generation —
    only the latter can truthfully say "the answer was generated"."""

    def __init__(self, stage: str = "answer") -> None:
        super().__init__(stage)
        self.stage = stage


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
        browser_broker: "BrowserBrokerStore | None" = None,
        schedules: "ScheduleStore | None" = None,
        scheduler: "SchedulerService | None" = None,
        memory: MemoryService | None = None,
        knowledge: "KnowledgeStore | None" = None,
        sessions: SessionStore | None = None,
        bot_store: "BotStore | None" = None,
        bot_id: str | None = None,
        is_cos: bool = False,
        llm_config: "LLMConfigStore | None" = None,
        missions: "MissionService | None" = None,
        mirror: "DelegationMirror | None" = None,
        group_members: frozenset[str] | None = None,
        connectors: Any = None,
        project_access: Any = None,
        blueprints: "BlueprintStore | None" = None,
    ):
        self.user_id = user_id
        self.workspace = workspace
        self.policy = policy
        self.is_cos = is_cos or group_members is not None
        self.is_group = group_members is not None
        self.tools = ToolRegistry(on_execute=self._audit_tool)
        self._audit_store = audit
        # Network mode of the sandbox exec runs, recorded in the security audit
        # trail so admins can see when a command had internet access.
        self._sandbox_network = settings.sandbox.network
        for tool_cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(tool_cls(workspace))
        self.tools.register(ExecTool(sandbox, workspace))
        self.tools.register(ProjectTool(sandbox, workspace, user_id, project_access))
        if blueprints is not None:
            self.tools.register(SaveBlueprintTool(blueprints, settings.blueprints_root, workspace, user_id))
        self.tools.register(WebFetchTool())
        self.tools.register(WebSearchTool())
        if memory is not None:
            self.tools.register(MemoryTool(memory, user_id, bot_id=memory_scope(bot_id, is_cos)))
            self.tools.register(RecallMemoryTool(memory, user_id))
        if sessions is not None:
            self.tools.register(PlanTool(sessions))
        subagents = SubagentManager(
            arg_guard=self._guard_tool_args if policy is not None else None,
            provider=provider,
            sandbox=sandbox,
            workspace=workspace,
            model=settings.llm.model,
            max_tokens=settings.llm.max_tokens,
            max_turn_seconds=settings.llm.max_turn_seconds,
            owner_id=user_id,
            project_access=project_access,
        )
        self.tools.register(SpawnTool(subagents))
        self.tools.register(WorkflowTool(WorkflowService(provider, subagents, model=settings.llm.model)))
        # One tool named "browser": when client-extension pairing is enabled, the
        # unified tool prefers the user's paired Chrome and falls back to the
        # server-side browser; otherwise keep the server-side-only tool.
        if browser_broker is not None and settings.browser.client_extension_enabled:
            from sbot.tools.client_browser import ClientBrowserTool

            self.tools.register(
                ClientBrowserTool(
                    broker=browser_broker,
                    user_id=user_id,
                    settings=settings.browser,
                    server_manager=browser,
                )
            )
        elif browser is not None:
            self.tools.register(BrowserTool(browser, user_id))
        if skills is not None:
            self.tools.register(ReadSkillTool(skills, user_id))
            self.tools.register(ManageSkillTool(skills, user_id))
        if knowledge is not None:
            self.tools.register(SearchKnowledgeTool(knowledge, user_id))
        if schedules is not None:
            from sbot.tools.schedule import ScheduleTool

            self.tools.register(ScheduleTool(schedules, scheduler, user_id))
        if (is_cos or group_members is not None) and bot_store is not None:
            from sbot.tools.cos import CreateBotTool, CreateBotsTool, DelegateManyTool, DelegateTool, ListBotsTool

            self.tools.register(ListBotsTool(bot_store, user_id, member_ids=group_members))
            if group_members is None:
                self.tools.register(CreateBotTool(bot_store, user_id, creator_bot_id=bot_id or "cos", connectors=connectors))
                self.tools.register(CreateBotsTool(bot_store, user_id, creator_bot_id=bot_id or "cos", connectors=connectors))
            delegate = DelegateTool(
                bot_store=bot_store,
                owner_id=user_id,
                provider=provider,
                sandbox=sandbox,
                workspace=workspace,
                model=settings.llm.model,
                llm_config=llm_config,
                skills=skills,
                memory=memory,
                mirror=mirror,
                leader_bot_id=bot_id,
                member_ids=group_members,
                connectors=connectors,
                project_access=project_access,
                arg_guard=self._guard_tool_args if policy is not None else None,
                reliability=settings.reliability,
                llm_settings=settings.llm,
            )
            self.tools.register(delegate)
            self.tools.register(DelegateManyTool(delegate))
            if missions is not None:
                from sbot.tools.missions import (
                    GroupMissionTool,
                    MissionGateTool,
                    MissionPlanTool,
                    MissionReplanTool,
                    MissionStartTool,
                    MissionStatusTool,
                )

                for tool in [MissionPlanTool(missions, user_id, member_ids=group_members),
                             MissionStartTool(missions, user_id), MissionStatusTool(missions, user_id),
                             MissionGateTool(missions, user_id), MissionReplanTool(missions, user_id)]:
                    self.tools.register(GroupMissionTool(tool, missions, user_id) if group_members is not None else tool)
                if settings.team_work.enabled:
                    from sbot.tools.team_work import TeamSubmitTool, BackgroundDelegateTool, TeamStatusTool, TeamCancelTool
                    submit = TeamSubmitTool(missions, user_id, bot_id, group_members)
                    self.tools.register(submit)
                    self.tools.register(BackgroundDelegateTool(submit, delegate))
                    self.tools.register(BackgroundDelegateTool(submit, delegate, many=True))
                    self.tools.register(TeamStatusTool(missions, user_id, group_members is not None))
                    self.tools.register(TeamCancelTool(missions, user_id, group_members is not None))
        if group_members is not None:
            # Group work is delegated only to selected named members.
            self.tools.unregister("spawn")
            self.tools.unregister("workflow")
        for doc_tool in build_document_tools(workspace):
            self.tools.register(doc_tool)
        from sbot.tools.artifacts import PublishArtifactTool
        self.tools.register(PublishArtifactTool(workspace))
        self.loop = AgentLoop(
            provider=provider,
            tools=self.tools,
            model=settings.llm.model,
            max_iterations=settings.llm.max_iterations,
            max_tokens=settings.llm.max_tokens,
            temperature=settings.llm.temperature,
            arg_guard=self._guard_tool_args if policy is not None else None,
            workspace=workspace,
            max_turn_seconds=settings.llm.max_turn_seconds,
        )

    def _guard_tool_args(self, tool_name: str, args: dict) -> tuple[dict, str | None]:
        """Apply the control policy to each string argument before a tool runs.

        Trusted tools (e.g. email/calendar connectors, per the admin exemption
        list) are "log-but-allow": their arguments are still checked so a match
        is recorded in the audit trail, but never masked or blocked — otherwise a
        legitimate recipient email would be redacted and the action would break.
        """
        if self.policy is None:
            return args, None
        exempt = self.policy.is_tool_exempt(tool_name)
        guarded = dict(args)
        for key, value in args.items():
            if not isinstance(value, str) or not value:
                continue
            decision = self.policy.enforce(value, scope="tool_args")
            if decision.matched_rules:
                self._log_policy_hit("tool_args", decision, tool=tool_name, exempt=exempt)
            if exempt:
                continue  # detected + logged, but pass the original value through
            if decision.blocked:
                return args, decision.message
            if decision.masked:
                guarded[key] = decision.text
        return guarded, None

    def _log_policy_hit(self, scope: str, decision, **extra) -> None:
        """Fire-and-forget audit trail for a guardrail match, for the admin
        overview's "guardrail hits over time" chart. Mirrors `_audit_tool`'s
        pattern since this is called from sync code inside the async loop."""
        # An exempt tool detected PII but was allowed through — record it as
        # observed (monitor), not enforced, so the trail is honest.
        if self._audit_store is None:
            return
        action = "monitor" if extra.get("exempt") else decision.action
        payload = {"scope": scope, "action": action, "rules": decision.matched_rules, **extra}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._audit_store.log("policy", payload, user_id=self.user_id))

    def _audit_tool(self, name: str, params: dict, result: str) -> None:
        if self._audit_store is None:
            return
        payload = {"tool": name, "params_preview": str(params)[:500], "result_preview": result[:500]}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._audit_store.log("tool_call", payload, user_id=self.user_id))
        # Dedicated security trail for sandbox shell runs — records the command
        # and whether it had network access, so an admin can review every
        # potentially unsafe action (esp. when NETWORK=bridge gives internet).
        if name in {"exec", "project"}:
            loop.create_task(
                self._audit_store.log(
                    "sandbox_exec",
                    {
                        "command": str(params.get("command", ""))[:500],
                        "network": self._sandbox_network,
                        "error": result.startswith("Error"),
                    },
                    user_id=self.user_id,
                )
            )

    def _leadership_brief(self, team_summary: str) -> str:
        """How a Chief of Staff is expected to handle a request. Empty for anyone else.

        Nothing in the prompt used to say a CoS should delegate — it was given
        the orchestration tools and left to infer the rest, so whether the team
        was used at all came down to the model's mood on the day. It mostly did
        the work itself, which is both the slower answer and the one that leaves
        the specialists' memory and skills unused.
        """
        if not self.is_cos:
            return ""
        if self.tools.has("team_submit"):
            return (
                "# Leading your team\n\n"
                "Keep the conversation available while your team works. For specialist work use "
                "team_submit to save and start a separate background job. delegate and delegate_many "
                "also hand off in the background here; their receipt is NOT a completed result. "
                "Submit all steps of one request together. Use depends_on whenever a step needs another's "
                "verified result (e.g. research before a travel post); never invent missing upstream results. "
                "Independent requests are separate jobs. Each job with multiple steps gets a final "
                "coordinator synthesis step; results will be delivered to this conversation. "
                "Provide self-contained instructions, input_files and required_files; chat history is not "
                "worker context. Workers use separate file folders, and inputs are copied under inputs/. "
                "Use team_status when asked about progress or pending work; read real status before "
                "claiming there is no pending work. Use its mission id with mission_status for full results. "
                "A follow-up changes an existing job only when explicitly requested; otherwise create a new one. "
                "Use team_cancel only when asked to stop that job. Failed work stays failed: explain the "
                "blocker and do not resubmit repeatedly or take over a long assignment inside this chat. "
                "For greetings, team questions and brief answers, respond directly. "
                "If nobody fits, you may assign a background step to yourself. Do not create recursive "
                "delegations. Do not schedule external actions beyond the user's authorization. "
                "File creation, publication and local delivery are distinct; report only confirmed outcomes."
                "\n\nCreating a team member is a bounded administrative request. When the user asks "
                "to create one bot, call create_bot once and stop. If the user explicitly requests two or more, "
                "call create_bots once with the complete roster. Do not create a plan, update memory, "
                "delegate a smoke test, message the new bot, create files, or begin its work unless the "
                "user explicitly asks for that additional action."
                + (f"\n{team_summary}" if team_summary else "")
            )
        return (
            "# Leading your team\n\n"
            f"You are the user's {'group coordinator' if self.is_group else 'Chief of Staff'}. "
            "Every request is yours to see through — "
            "which is not the same as yours to do. Before you start work, decide who should "
            "do it:\n"
            "- A specialist on your team covers the request → delegate it to them. This "
            "outranks '## Answering directly' above: when the work is a teammate's job, "
            "handing it over IS the direct answer, even for writing, planning or analysis "
            "you could have done yourself.\n"
            "- It splits into parts that do not depend on each other → send them together "
            "with `delegate_many` so they run at the same time. Two `delegate` calls in one "
            "turn run one after the other and take twice as long.\n"
            "- Parts that must happen in order, or work that will outlast this conversation "
            "→ plan a mission (`mission_plan` / `mission_start`).\n"
            "- Nobody on the team fits, or you have no team → do it yourself, and say so.\n"
            "- The user is asking about you, the team, or the conversation, or is just "
            "chatting → answer. Never delegate a greeting.\n\n"
            "Creating a member is an administrative request, not a project. When the user asks "
            "to create one bot, call create_bot once; when they explicitly ask for several, call create_bots once "
            "with every requested member. Then stop; do not plan, test, delegate to, "
            "or otherwise use it unless the user explicitly asks for that extra work.\n\n"
            "Handing work out does not end your turn. You still own the report and coordination:\n"
            "- Every specialist can publish_artifact and read documents in addition to its configured "
            "tools. Use the runtime attachment manifest in delegate results as delivery evidence. "
            "Those files already appear in the specialist's reply; do not republish them.\n"
            "- Check what comes back against what the user actually asked for. If it is incomplete, "
            "report the recorded status and blocker. Do not repeat the full delegation or take over "
            "a long specialist assignment inside this chat unless the user explicitly asks you to.\n"
            "- Report in your own voice: what was done, by whom, what it means, what is next. "
            "A specialist's reply pasted verbatim is not a report.\n"
            "- Decide what is yours to decide, and say that you decided it.\n"
            "- When the call is genuinely the user's — money, risk, direction, anything hard "
            "to undo — put it to them with the options, the trade-off and your own "
            "recommendation. Do not stall on a decision you could have made, and do not "
            "quietly make one that was theirs.\n"
            + (f"\n{team_summary}" if team_summary else "")
        )

    def prompt_sections(
        self,
        memory_context: str,
        persona: str = "",
        skills_summary: str = "",
        knowledge_summary: str = "",
        connectors_summary: str = "",
        plan_context: str = "",
        bot_charter: str = "",
        bot_name: str = "",
        bot_role: str = "",
        team_summary: str = "",
    ) -> list[PromptSection]:
        """The system prompt as ordered sections, most important first.

        Returned unjoined so the assembler can drop from the bottom when the
        window is tight (PRD §4.5) instead of the whole prompt being sent whole
        and pushing the conversation out of context.
        """
        runtime = f"{platform.system()} {platform.machine()}, Python {platform.python_version()}"
        agent_identity = (
            f"You are {bot_name} ({bot_role}), part of the user's multi-bot AI team."
            if bot_name
            else "You are sbot, the user's AI agent — a careful, reliable partner that finishes what it starts."
        )
        core = (
            f"# {bot_name or 'sbot Agent'}\n\n"
            f"{agent_identity}\n\n"
            f"## Runtime\n{runtime}\n\n"
            f"## Workspace\nYour workspace is mounted for file tools; shell commands run "
            f"in an isolated sandbox with the same workspace at /workspace.\n"
            "For software development use project, which keeps packages and services across calls. "
            "Its /workspace maps to projects/<slug>/ in file tools. Give all teammates the same slug. "
            "Use mission dependencies or separate git worktrees to avoid concurrent edits. "
            "Deliver code, test results and service health evidence; a prose answer is not a working project.\n\n"
            "## Answering directly\n"
            "Most requests are best answered straight from your own knowledge, in the chat, "
            "with no tool call at all — writing a PRD, a marketing plan, an email, a strategy, "
            "an explanation, a comparison, or a review of text the user pasted. For these, "
            "just write the answer. Every tool call the user didn't need is pure waiting.\n"
            "Reach for a tool only when the request genuinely needs one:\n"
            "- facts you don't have, or that must be current (`web_search`, `web_fetch`)\n"
            "- the user's own files, data, or knowledge bases\n"
            "- an action in the world: running code, browsing, sending, scheduling\n"
            "Do not produce a .docx/.xlsx/.pptx/.pdf file unless the user actually asked for a "
            "file. Write the content in the chat as markdown and offer to export it.\n"
            "For a requested file, create it in the workspace and state its filename; Sbot attaches "
            "created files automatically.\n"
            "Never search the filesystem for your own instructions, skills, or capabilities — "
            "everything you have is already in this prompt or one `read_skill` call away.\n\n"
            "## Guidelines\n"
            "- State intent before tool calls; never claim results before receiving them.\n"
            "- Read a file before modifying it. Analyze tool errors before retrying.\n"
            "- Ask for clarification when the request is ambiguous.\n"
            "- Complete all authorized steps before ending the turn. 'Do A first, then B' "
            "authorizes both: do not stop after A to ask optional approval. Choose reasonable "
            "defaults for reversible file creation. Skill suggestions to preview/review do not "
            "require user approval unless the user or a mandatory permission rule requires it. "
            "Before your final answer, update the plan to match actual progress. If essential "
            "input or permission is missing, mark the affected step waiting_for_user with a "
            "specific reason; if an external failure prevents progress, mark it blocked. "
            "Never claim completion while required steps remain unfinished.\n"
            "- Match the user's language in your replies.\n"
            "- When a task will genuinely take several tool calls, call `update_plan` to record "
            "the goal and steps, and keep it updated as you progress. The plan stays pinned in "
            "your context, so you keep the thread even after earlier messages scroll away — lean "
            "on it instead of losing track of the original request. A request you can answer in "
            "one written reply needs no plan; writing one just delays the answer.\n"
            "- You have long-term memory: when the user shares something worth keeping "
            "(their name, preferences, ongoing work, or asks you to remember something), "
            "call the `remember` tool to save it. It persists across conversations, so "
            "never tell the user you cannot remember things. What is saved is usually "
            "shown under Long-term Memory below; when that section is missing or does not "
            "cover what was asked, call `recall_memory` rather than assuming you have "
            "nothing stored."
        )
        charter = ""
        if bot_charter:
            charter = f"# Bot Charter & Persona\n\n{bot_charter}"
        elif persona:
            charter = f"# Persona\n\n{persona}"
        return [
            PromptSection("core", core, pinned=True),
            PromptSection("charter", charter, pinned=True),
            # Pinned for the same reason the charter is: for a Chief of Staff,
            # leading the team is not a capability it has, it is what it is. It
            # also has to outrank "## Answering directly" above, which is written
            # for a bot working alone and on its own reads as "just write the
            # marketing plan yourself" — which is exactly what a CoS with a
            # strategist on the team did.
            PromptSection("leadership", self._leadership_brief(team_summary), pinned=True),
            PromptSection("memory", memory_context),
            # Pinned because it is the one section nothing can fetch back:
            # `update_plan` is write-only and replaces the stored plan, so a
            # dropped plan leaves the model no way to read its own goal and
            # steps — its only recourse is to invent a new plan over the real
            # one. The tool description and this prompt both tell the model the
            # plan is pinned; PlanTool bounds what it can store so that promise
            # cannot be used to grow the prompt without limit.
            PromptSection("plan", plan_context, pinned=True),
            PromptSection("skills", skills_summary),
            PromptSection("knowledge", knowledge_summary),
            PromptSection("connectors", connectors_summary),
        ]


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
        bots: "BotStore | None" = None,
        skills: SkillStore | None = None,
        connectors: ConnectorManager | None = None,
        policy: PolicyEngine | None = None,
        browser: BrowserManager | None = None,
        usage: "UsageStore | None" = None,
        schedules: ScheduleStore | None = None,
        scheduler: SchedulerService | None = None,
        llm_config: "LLMConfigStore | None" = None,
        plans: "PolicyPlanStore | None" = None,
        browser_broker: BrowserBrokerStore | None = None,
        knowledge: "KnowledgeStore | None" = None,
        missions: "MissionService | None" = None,
        project_access: Any = None,
        blueprints: "BlueprintStore | None" = None,
    ):
        self.settings = settings
        self.provider = provider
        self.llm_config = llm_config
        self.plans = plans
        self.knowledge = knowledge
        self.bus = bus
        self.users = users
        self.bots = bots
        self.sessions = sessions
        self.messages = messages
        self.memory = memory
        self.audit = audit
        self.skills = skills
        self.connectors = connectors
        self.policy = policy
        self.browser = browser
        self.browser_broker = browser_broker
        self.usage = usage
        self.schedules = schedules
        self.scheduler = scheduler
        self.missions = missions
        self.project_access = project_access
        self.blueprints = blueprints
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
        # session_id -> in-flight turn count, so the UI can show "processing"
        # status in the sidebar even when the client isn't viewing that session.
        self._active_turns: dict[str, int] = {}
        # request_id -> pending confirmation (Ask-mode gate). The awaiting turn
        # holds the future; the WS handler resolves it when the user answers.
        self._confirmations: dict[str, dict] = {}

    def active_sessions(self) -> set[str]:
        """Sessions with at least one turn currently processing."""
        return {sid for sid, n in self._active_turns.items() if n > 0}

    # ------------------------------------------------------------------ Ask-mode
    async def request_confirmation(self, session_id: str, turn_id: str, tool: str, args_preview: str) -> bool:
        """Publish a confirm-request and block until the user answers (or timeout)."""
        request_id = uuid.uuid4().hex[:12]
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._confirmations[request_id] = {
            "future": future,
            "session_id": session_id,
            "turn_id": turn_id,
            "tool": tool,
            "args_preview": args_preview,
        }
        self.bus.publish(
            session_id,
            ToolConfirmRequest(turn_id=turn_id, request_id=request_id, tool=tool, args_preview=args_preview),
        )
        try:
            approved = await asyncio.wait_for(future, timeout=_CONFIRM_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            approved = False
            self.bus.publish(
                session_id,
                ToolConfirmResolved(turn_id=turn_id, request_id=request_id, approved=False),
            )
            logger.info("Confirmation {} timed out — auto-declined", request_id)
        finally:
            self._confirmations.pop(request_id, None)
        return approved

    def resolve_confirmation(self, request_id: str, approved: bool) -> bool:
        """Answer a pending confirmation from the user's decision. Returns True if it existed."""
        entry = self._confirmations.get(request_id)
        if entry is None:
            return False
        future: asyncio.Future = entry["future"]
        if not future.done():
            future.set_result(approved)
        self.bus.publish(
            entry["session_id"],
            ToolConfirmResolved(turn_id=entry["turn_id"], request_id=request_id, approved=approved),
        )
        return True

    def pending_confirmations(self, session_id: str) -> list[ToolConfirmRequest]:
        """Open confirmations for a session, so a (re)connecting client can re-render them."""
        return [
            ToolConfirmRequest(
                turn_id=e["turn_id"],
                request_id=rid,
                tool=e["tool"],
                args_preview=e["args_preview"],
            )
            for rid, e in self._confirmations.items()
            if e["session_id"] == session_id and not e["future"].done()
        ]

    def get_agent(
        self, user_id: str, bot_id: str | None = None, is_cos: bool = False,
        group_members: frozenset[str] | None = None,
    ) -> ClawAgent:
        cache_key = f"{user_id}:{bot_id or 'default'}"
        agent = self._agents.get(cache_key) if group_members is None else None
        if agent is not None:
            self._agents.move_to_end(cache_key)
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
            browser_broker=self.browser_broker,
            schedules=self.schedules,
            scheduler=self.scheduler,
            memory=self.memory,
            knowledge=self.knowledge,
            sessions=self.sessions,
            bot_store=self.bots,
            bot_id=bot_id,
            is_cos=is_cos,
            group_members=group_members,
            llm_config=self.llm_config,
            missions=self.missions,
            connectors=self.connectors,
            project_access=self.project_access,
            blueprints=self.blueprints,
            # Built here rather than inside the agent: mirroring needs the bus
            # and the message store, which an agent otherwise has no reason to
            # know about — it publishes nothing, the runtime does that for it.
            # `lock_for` is the same per-session turn lock `_process_turn`
            # takes, and it has to come from here: this agent is cached, and an
            # eviction mid-delegation used to leave the rebuilt one with a fresh
            # guard that knew nothing about the run still in flight.
            mirror=(
                DelegationMirror(
                    self.bus,
                    self.sessions,
                    self.messages,
                    user_id,
                    leader_bot_id=bot_id,
                    lock_for=self._session_lock,
                )
                if is_cos and group_members is None and self.sessions is not None
                else None
            ),
        )
        if getattr(self, 'local_workspaces', None) is not None:
            from sbot.tools.local_workspace import LocalWorkspaceTool
            agent.tools.register(LocalWorkspaceTool(workspace, self.local_workspaces, user_id))
            delegate = agent.tools.get("delegate")
            if delegate is not None:
                delegate.local_broker = self.local_workspaces
        if group_members is not None:
            return agent  # group membership is a per-turn snapshot; never contaminate a direct chat
        self._agents[cache_key] = agent
        self._agents.move_to_end(cache_key)
        while len(self._agents) > self.settings.max_resident_agents:
            self._agents.popitem(last=False)
        return agent

    async def warm_connectors(self, user_id: str) -> None:
        """Connect the user's MCP connectors outside a chat turn so their live
        status is accurate for the composer's connector menu. Cheap when already
        up to date.

        The registry is a throwaway: what the warm is for is the live sessions
        and statuses, which live in the ConnectorManager's per-user state, and a
        turn's own registry is filled from that state whichever bot it belongs
        to. Warming into an agent instead would build one nobody chats with —
        the turn resolves a real bot, so its agent is a different cache entry —
        and cost a resident-agent slot to hold it."""
        if self.connectors is None:
            return
        try:
            await self.connectors.sync_tools(user_id, ToolRegistry())
        except Exception:
            logger.exception("Connector warm failed for {}", user_id)

    # -------------------------------------------------- Prompt-context fetches
    # Each returns an empty result when its subsystem is disabled, so the turn
    # can gather them unconditionally instead of branching per optional store.
    async def _sync_connectors(self, user_id: str, tools: ToolRegistry) -> None:
        if self.connectors is None:
            return
        try:
            await self.connectors.sync_tools(user_id, tools)
        except Exception:
            logger.exception("Connector sync failed for {}", user_id)

    async def _user_skills(self, user_id: str) -> list:
        if self.skills is None:
            return []
        return await self.skills.enabled_for_user(user_id)

    async def _knowledge_bases(self, user_id: str) -> list[dict]:
        if self.knowledge is None:
            return []
        try:
            return await self.knowledge.list_accessible(user_id)
        except Exception:
            return []

    async def _connected_connectors(self, user_id: str) -> list:
        if self.connectors is None:
            return []
        try:
            return await self.connectors.store.enabled_accessible(user_id)
        except Exception:
            return []

    def _assembler_for(self, model_window: int | None) -> ContextAssembler:
        """Pack the prompt to the window of the model this turn actually runs on.

        `settings.llm.max_context_tokens` is the operator's prompt budget for the
        env-configured default, but the model is re-resolved every turn from the
        session picker and the admin's per-model rows. A turn on a smaller model
        was still packed to that budget, so nothing was trimmed here and the
        request was rejected by the provider instead — the same conversation
        failing on one model and working on another, with no trimming in between.

        The lower of the two: the model's window is a hard limit, the operator's
        budget a deliberate one, and neither is safe to exceed.
        """
        if not model_window or model_window >= self.assembler.max_context_tokens:
            return self.assembler
        return ContextAssembler(
            token_counter=self.assembler.count_tokens, max_context_tokens=model_window
        )

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
        model: str | None = None,
        permission_mode: str = "auto",
        blueprints: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Process one user message; tracks in-flight count for graceful shutdown."""
        self._inflight += 1
        self._active_turns[session_id] = self._active_turns.get(session_id, 0) + 1
        try:
            return await self._process_turn(
                user_id, session_id, content, channel, locale, media, model, permission_mode, blueprints
            )
        finally:
            self._inflight -= 1
            remaining = self._active_turns.get(session_id, 1) - 1
            if remaining <= 0:
                self._active_turns.pop(session_id, None)
            else:
                self._active_turns[session_id] = remaining

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
        model: str | None = None,
        permission_mode: str = "auto",
        blueprints: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Process one user message; events stream to the bus, messages persist to DB.

        Returns the final assistant content (for non-streaming callers/tests).
        """
        turn_id = uuid.uuid4().hex[:12]

        # Resolve the caller's usage-tier plan once (None = no plan / unlimited).
        # It governs the per-minute cap, the daily message quota, and the chat
        # model cost ceiling used further down.
        plan = await self.plans.resolve_for_user(user_id) if self.plans is not None else None

        # Daily message quota (plan.messages_per_day; 0 = unlimited). Soft cap:
        # UsageDaily is written in a background task after the turn, so a burst
        # can overshoot slightly — acceptable for tiering. Checked BEFORE the
        # rate limiter below so a user who's already over their daily cap
        # doesn't also burn a per-minute slot on every rejected retry.
        if plan and plan["messages_per_day"] > 0 and self.usage is not None:
            used_today = (await self.usage.usage_today(user_id))["turns"]
            if used_today >= plan["messages_per_day"]:
                await self.audit.log(
                    "quota",
                    {
                        "event": "messages_per_day",
                        "plan": plan["name"],
                        "limit": plan["messages_per_day"],
                    },
                    user_id=user_id,
                    session_id=session_id,
                )
                msg = t("error.daily_limit", locale)
                self.bus.publish(session_id, TurnStarted(turn_id=turn_id))
                self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
                return msg

        # Per-user rate limit — reject before doing any work or calling the
        # model. A plan can only TIGHTEN the global per-minute cap, never exceed
        # it: Settings.turns_per_minute is the operator's hard safety backstop
        # against overloading shared infrastructure, and applies even to a plan
        # whose own turns_per_minute is 0. Plan 0 = "no plan-specific throttle,
        # inherit the global backstop" (NOT "ignore the global backstop") —
        # global 0 = the backstop itself is off. So the effective cap is the
        # stricter of the two, treating 0 as "no limit on that side."
        global_rpm = self._rate_limiter.per_minute
        plan_rpm = plan["turns_per_minute"] if plan else 0
        if plan_rpm and global_rpm:
            effective_rpm = min(plan_rpm, global_rpm)
        else:
            # One side unlimited: plan_rpm (if set) applies, else fall to global.
            effective_rpm = plan_rpm or global_rpm
        if not self._rate_limiter.allow(user_id, per_minute=effective_rpm):
            msg = t("error.rate_limited", locale)
            self.bus.publish(session_id, TurnStarted(turn_id=turn_id))
            self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
            return msg

        session = await self.sessions.get(session_id)
        # Resolved once and carried through the whole turn: the bot decides its
        # identity in the prompt, which tools it may call, and which skills it
        # sees, so a second fetch further down could disagree with this one.
        # A session pointing at a bot that is gone (or was never the caller's)
        # falls back to the caller's own Chief of Staff, matching what the
        # prompt has always claimed in that case.
        group = None
        group_members = None
        bot = None
        if session and session.kind == "group":
            from sbot.db.bot_groups import BotGroupStore
            group = await BotGroupStore(self.sessions.factory).get(session.group_id, user_id)
            if group is not None and self.bots is not None:
                group_members = frozenset(group.member_ids)
                bot = await self.bots.get(group.leader_id, user_id)
            if group is None or bot is None or bot.id not in group_members:
                msg = "Group unavailable. Choose an active leader in group settings."
                self.bus.publish(session_id, TurnStarted(turn_id=turn_id))
                self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
                return msg
        elif self.bots is not None:
            if session and session.bot_id:
                bot = await self.bots.get(session.bot_id, user_id)
            if bot is None:
                bot = await self.bots.get_or_create_cos(user_id)
        bot_id = bot.id if bot else None
        is_cos = bot.kind == "chief_of_staff" if bot else False
        agent = self.get_agent(user_id, bot_id=bot_id, is_cos=is_cos, group_members=group_members)
        # The bot's capability boundary, re-applied every turn so an allowlist
        # edited in Settings takes effect on the next message instead of
        # whenever the agent cache happens to evict. Without this the boundary
        # only existed on the delegate/mission path (SpecialistRunner), so a
        # bot restricted to research could still run shell commands the moment
        # the user opened its chat directly.
        agent.tools.restrict_to(bot.tool_allowlist if bot else None)
        # Which memory document this turn reads and writes (None = the shared one).
        mem_bot_id = memory_scope(bot_id, is_cos)

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
            try:
                session = await self.sessions.get(session_id)
                after_seq = session.last_consolidated_seq if session else 0

                # Resolve the effective model for this turn: explicit request → sticky
                # per-chat choice → admin default → env default. Persist an explicit
                # choice onto the session so it sticks for the whole conversation.
                # The plan's chat cost ceiling gates which admin-global model this
                # turn may use; resolve() returns None for a disallowed one, so a
                # user can't reach a pricier model than their tier by passing its
                # id. Default resolution picks the best model the plan allows.
                plan_chat_cost = plan["max_chat_cost"] if plan else None
                effective_model: str | None = None
                model_key: str | None = None
                model_base: str | None = None
                model_window: int | None = None
                fallback_model: str | None = None
                fallback_key: str | None = None
                fallback_base: str | None = None
                fallback_window: int | None = None
                requested_unavailable = False
                if self.llm_config is not None:
                    # The bot's own model sits behind the session's sticky choice
                    # but ahead of the defaults: it is part of the bot's profile,
                    # so a specialist configured onto a cheaper or stronger model
                    # ran on it when delegated to and silently ignored it the
                    # moment the user opened its chat directly. A model the plan
                    # disallows still resolves to None and falls through, so this
                    # cannot be used to reach a pricier tier.
                    requested = (
                        model
                        or (session.model if session else None)
                        or (bot.model if bot else None)
                    )
                    if requested:
                        resolved = await self.llm_config.resolve(requested, user_id, max_cost=plan_chat_cost)
                        if resolved is not None:
                            effective_model = resolved["model_id"]
                            model_key = resolved["api_key"] or None
                            model_base = resolved["api_base"] or None
                            model_window = resolved["context_window"]
                        else:
                            requested_unavailable = True
                    if effective_model is None:
                        effective_model = await self.llm_config.default_model_for(plan_chat_cost)
                        if effective_model is not None:
                            # Scoped to admin-global rows (user_id=None) to match
                            # default_model_for, which picked this model from that
                            # scope: resolve() otherwise prefers the caller's own
                            # row on a model_id tie, so a user with a same-named
                            # BYOK model would set the window for a turn running on
                            # the admin's credentials.
                            default_resolved = await self.llm_config.resolve(
                                effective_model, None, max_cost=plan_chat_cost
                            )
                            if default_resolved:
                                model_key = default_resolved["api_key"] or None
                                model_base = default_resolved["api_base"] or None
                                model_window = default_resolved["context_window"]
                    # A plan cost ceiling is in effect but no admin-global model
                    # satisfies it. Distinguish two cases before rejecting:
                    #   1. Admin-global models DO exist but the plan allows none of
                    #      them → genuine cost-ceiling gating; reject so the user
                    #      can't reach a pricier tier than their plan permits.
                    #   2. No admin-global model is configured at all → the
                    #      operator's env-configured default is the sole out-of-box
                    #      model and their deliberate baseline. Let it through
                    #      (model=None → claw/core/loop.py resolves the env default),
                    #      the same spirit as BYOK models bypassing the ceiling —
                    #      there is no lineup to gate. Otherwise a fresh install with
                    #      only SBOT_LLM__MODEL set would be unusable for every
                    #      non-admin on the default plan.
                    # (No ceiling in effect, i.e. plan_chat_cost is None, always kept
                    # the env-default fallback and is unchanged.)
                    if effective_model is None and plan_chat_cost is not None:
                        any_global = await self.llm_config.default_model_for(None)
                        if any_global is not None:
                            await self.audit.log(
                                "quota",
                                {
                                    "event": "no_model_for_plan",
                                    "plan": plan["name"] if plan else None,
                                    "max_chat_cost": plan_chat_cost,
                                },
                                user_id=user_id,
                                session_id=session_id,
                            )
                            msg = t("error.no_model_for_plan", locale)
                            self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
                            return msg
                    fallback_resolver = getattr(self.llm_config, "fallback_model_for", None)
                    fallback_model = (
                        await fallback_resolver(plan_chat_cost) if fallback_resolver is not None else None
                    )
                    if fallback_model is not None:
                        fallback_resolved = await self.llm_config.resolve(
                            fallback_model, None, max_cost=plan_chat_cost
                        )
                        if fallback_resolved is not None:
                            fallback_key = fallback_resolved["api_key"] or None
                            fallback_base = fallback_resolved["api_base"] or None
                            fallback_window = fallback_resolved["context_window"]
                            if requested_unavailable:
                                effective_model = fallback_model
                                model_key = fallback_key
                                model_base = fallback_base
                                model_window = fallback_window
                                fallback_model = None
                    # About to fall through to the operator's env-configured default
                    # (effective_model is None, no DB model available). If that env
                    # default has no usable credentials either (no api_key and no
                    # api_base — a local keyless endpoint would still set api_base),
                    # there is genuinely no model to call: surface a clear setup
                    # message to the operator instead of letting loop.py hit a raw,
                    # confusing provider auth error.
                    if effective_model is None and not (
                        self.settings.llm.api_key or self.settings.llm.api_base
                    ):
                        await self.audit.log(
                            "quota",
                            {"event": "no_model_configured"},
                            user_id=user_id,
                            session_id=session_id,
                        )
                        msg = t("error.no_model_configured", locale)
                        self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
                        return msg
                    if model and session is not None and session.model != model:
                        self._spawn_background(self.sessions.set_model(session_id, model))
                # Everything the prompt needs, fetched concurrently. These reads
                # are independent, and the user waits for all of them before the
                # first token — serializing them just adds their latencies up.
                # Connector sync is the only network-bound step, so it overlaps
                # the DB reads rather than blocking them; it is still awaited
                # before the skills summary and the turn itself, because a
                # connector-linked skill needs the tool names it registers.
                # Tracked in _background (like _spawn_background) so a failure in
                # one of the gathered reads below can't leave it orphaned outside
                # drain()'s view — the `finally` guarantees it's awaited even if
                # gather raises, instead of being abandoned mid-flight.
                sync_task = asyncio.create_task(self._sync_connectors(user_id, agent.tools))
                self._background.add(sync_task)
                sync_task.add_done_callback(self._background.discard)
                try:
                    history, memory_context, user_skills, bases, connected_rows = await asyncio.gather(
                        self.messages.recent(session_id, after_seq=after_seq),
                        self.memory.build_context(user_id, bot_id=mem_bot_id),
                        self._user_skills(user_id),
                        self._knowledge_bases(user_id),
                        self._connected_connectors(user_id),
                    )
                finally:
                    await sync_task
                # Built-in skills are always offered; user skills are merged in,
                # and one of the same name SHADOWS the built-in instead of
                # joining it. Both other readers already resolve it that way —
                # read_skill checks the store first, and Settings hides the
                # built-in behind `shadows_builtin` — so listing both here would
                # advertise a built-in's description alongside content the tool
                # can never return. A user can hold such a name from before it
                # was reserved: the reserved-name check only guards new writes.
                user_names = {s.name for s in user_skills}
                enabled_skills = scope_skills(
                    [
                        *(b for b in builtin_skills() if b.name not in user_names),
                        *user_skills,
                    ],
                    bot.skill_ids if bot else None,
                )
                # For any user skill linked to a connector, resolve that
                # connector's CURRENT registered tool names (mcp_{name}_{tool})
                # live — never hardcoded in the skill's own text, so renaming or
                # recreating the connector never leaves the skill's instructions
                # pointing at a stale/nonexistent tool name.
                tool_names_by_skill: dict[str, list[str]] = {}
                if self.connectors is not None and any(
                    getattr(s, "connector_id", None) for s in enabled_skills
                ):
                    # Fetch each list once for the whole turn instead of once per
                    # linked skill — resolve_tool_names would otherwise re-query
                    # both the user's own connectors and the global ones on every
                    # call, which scales with skill count, not with anything that
                    # actually changed.
                    owned = await self.connectors.store.list_for_user(user_id)
                    global_connectors = await self.connectors.store.list_for_global()
                    for s in enabled_skills:
                        connector_id = getattr(s, "connector_id", None)
                        if connector_id:
                            names = await self.connectors.resolve_tool_names(
                                user_id, connector_id, owned=owned, global_connectors=global_connectors
                            )
                            # Filtered by what this bot can actually call: the
                            # names come from the connector's state, not the
                            # registry, so a restricted bot would otherwise be
                            # handed exact instructions to call a tool its own
                            # allowlist has already removed from the turn.
                            # `or []` because resolve_tool_names returns None for a
                            # connector that is not currently connected, and a skill
                            # keeps its connector_id while the connector is merely
                            # disabled — an ordinary turn, not an error.
                            names = [n for n in (names or []) if agent.tools.has(n)]
                            if names:
                                tool_names_by_skill[s.name] = names
                skills_summary = build_skills_summary(enabled_skills, tool_names_by_skill)
                # Tell the agent which knowledge bases exist so it knows to reach for
                # the search_knowledge tool when a question may be answered by them.
                # Withheld when the bot's allowlist excludes that tool — naming a
                # tool the model cannot call buys a refused call and an apology,
                # not an answer.
                knowledge_summary = ""
                usable = [b for b in bases if b["docs"] > 0] if agent.tools.has("search_knowledge") else []
                if usable:
                    lines = "\n".join(
                        f"- {b['name']}" + (f": {b['description']}" if b["description"] else "")
                        for b in usable
                    )
                    knowledge_summary = (
                        "# Knowledge bases\n\n"
                        "The user has uploaded documents into these knowledge bases. When a "
                        "question may be answered by them, call the `search_knowledge` tool "
                        "(optionally with `knowledge_base` to target one) and answer from the "
                        "returned passages, citing the source.\n\n" + lines
                    )
                # Tell the agent which integrations exist but aren't connected yet,
                # so it points the user at Settings -> Connectors instead of
                # improvising a workaround with the generic browser/web-fetch tools
                # (e.g. asking them to make a private Google Sheet public).
                connectors_summary = ""
                if self.connectors is not None:
                    connected_names = {c.name for c in connected_rows}
                    not_connected = [p for p in list_presets() if p["name"] not in connected_names]
                    if not_connected:
                        lines = "\n".join(f"- {p['label']}: {p['description']}" for p in not_connected)
                        connectors_summary = (
                            "# Available but not-yet-connected integrations\n\n"
                            "These integrations exist but the user has not connected them yet, so "
                            "no tool for them is available this turn. If a request needs one (e.g. a "
                            "Google Sheets/Drive URL needs the Google Sheets connector, a repo needs "
                            "GitHub), tell the user to connect it under Settings -> Connectors — do "
                            "not try to work around it with the browser or web-fetch tools, and do "
                            "not ask the user to make private content public or export/download it "
                            "instead.\n\n" + lines
                        )
                # Who the Chief of Staff can hand work to, named in the prompt
                # rather than left behind a `list_bots` call. A leader that has
                # to look up its own team first mostly does not bother, and then
                # reasons as if it had none; naming the ids here also spares the
                # model from retyping a 32-character uuid into `delegate`.
                team_summary = ""
                if (is_cos or group is not None) and self.bots is not None:
                    team = [
                        b
                        for b in await self.bots.list_for_user(user_id)
                        if b.id != bot_id and (b.id in group_members if group_members is not None
                                                  else b.kind != "chief_of_staff")
                    ]
                    if team:
                        shown = team[:_TEAM_ROSTER_LIMIT]
                        lines = "\n".join(
                            f"- **{b.name}** — {b.role_title} (bot_id: `{b.id}`)"
                            + (f"\n  {_charter_gist(b.charter)}" if b.charter else "")
                            for b in shown
                        )
                        # Said out loud when the list is cut. The roster is
                        # oldest-first, so what gets dropped is whatever the
                        # user created most recently — and a leader told only
                        # that `list_bots` returns fuller charters reads a
                        # partial list as the whole team and concludes nobody
                        # fits.
                        rest = (
                            f"Showing {len(shown)} of {len(team)} — call `list_bots` for the "
                            f"{len(team) - len(shown)} not listed here.\n"
                            if len(team) > len(shown)
                            else ""
                        )
                        team_summary = (
                            "## Your team\n\n"
                            "Delegate by `bot_name`. Call `list_bots` if you need a charter in "
                            "full.\n" + rest + "\n" + lines
                        )
                    else:
                        team_summary = (
                            "## Your team\n\n"
                            "You have no specialists yet, so this work is yours to do. If the "
                            "user will need the same kind of work again, offer to create a "
                            "specialist for it (`create_bot`) — do not create one unasked."
                        )
                if group is not None:
                    team_summary = (
                        f"## Group chat: {group.name}\n"
                        "You are this group's coordinator. Address the user's message in this shared conversation. "
                        "Use delegate/delegate_many to involve the selected members when their expertise helps; "
                        "pass the relevant conversation context and synthesize their results. If the user asks "
                        "a named member to work, delegate to that member. Only the members listed below belong "
                        "to this group. Do not create bots, start missions outside this group, or invent replies "
                        "on behalf of other members. For long or multi-step work use mission_plan then "
                        "mission_start with the selected members. Missions persist after this chat turn "
                        "and report results and deliverables into this conversation.\n\n" + team_summary
                    )
                runtime_ctx = build_runtime_context(channel, locale)
                if getattr(self, 'local_workspaces', None) is not None:
                    selected_workspace = self.local_workspaces.selected(user_id, session_id)
                    if selected_workspace:
                        runtime_ctx += (
                            '\nSelected LOCAL workspace_id: ' + selected_workspace +
                            '\nUse local_workspace to list/read/import files from this folder. '
                            'Host file tools and exec cannot access the local folder. Import a copy to '
                            'the cloud workspace to edit Office documents; publish the final cloud file '
                            'and export to a NEW local filename when requested. Never overwrite originals. '
                            'Before delegating, import required files yourself and pass their cloud paths '
                            'to teammates. Specialist and mission tools do not access local folders in '
                            'this release. Perform any final local export yourself after their work. '
                            'If the device is offline or revoked, report the blocker; do not substitute '
                            'a cloud folder or claim delivery. Local shell execution is unavailable.'
                        )
                model_content, storage_text = build_user_content(content, media, agent.workspace)
                if blueprints:
                    blueprint_lines = "\n".join(
                        f"- {item['name']} version {item['version']} at `{item['path']}`"
                        for item in blueprints[:8]
                    )
                    blueprint_note = (
                        "\n\n[Blueprint templates selected by the user]\n"
                        "Use the selected file's existing layout, styles, formulas, and structure as the "
                        "default template for this request. The library source is immutable and the attached "
                        "file is a working copy. Create the finished deliverable with a clear new filename; "
                        "do not replace or delete the working copy. Ask only for information that is actually "
                        "missing from the user's request.\n" + blueprint_lines
                    )
                    if isinstance(model_content, str):
                        model_content += blueprint_note
                    else:
                        for block in reversed(model_content):
                            if block.get("type") == "text":
                                block["text"] = str(block.get("text") or "") + blueprint_note
                                break
                # model_content is only ever a list when build_user_content produced at
                # least one image_url block (see its docstring) — never send that to a
                # text-only model, which would fail identically on every retry.
                # effective_model is None when no Control Plane model resolved and the
                # provider falls back to its own default, so mirror that fallback here or
                # the check silently skips on env-configured deployments.
                turn_model = effective_model or self.settings.llm.model
                stored_content = storage_text  # text + attachment names (never base64)
                # Persist the user message NOW (not at turn end) so it's durable the
                # instant the turn starts. Otherwise switching away mid-turn and back
                # would show an empty transcript — listMessages had nothing yet.
                # history was already loaded above, so this doesn't duplicate the
                # prompt's user turn. This has to stay ahead of the vision read
                # below: that read can block for a minute and a half, and anything
                # it raises would otherwise take the user's message down with it.
                try:
                    user_seq = await self.messages.append(
                        session_id, [{"role": "user", "content": stored_content}]
                    )
                except SQLAlchemyError as exc:
                    raise _TranscriptSaveError("request") from exc
                vision_delegate: str | None = None
                if (
                    isinstance(model_content, list)
                    and turn_model
                    and not model_supports_vision(turn_model)
                ):
                    # Hand the images to the operator's kind="vision" model once and
                    # let the chat model answer from its description, so a turn that
                    # could only ever be refused gets an answer instead. Falls back to
                    # the refusal when no vision model is configured or the read fails.
                    refusal: str | None = None
                    try:
                        delegated = await self._delegate_vision(user_id, session_id, model_content)
                    except _VisionBlocked as blocked:
                        delegated, refusal = None, str(blocked)
                    if delegated is None:
                        msg = refusal or t("error.llm_no_vision_support", locale)
                        self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
                        return msg
                    vision_delegate, model_content, description_note = delegated
                    # Fold the description into the stored message. The images are
                    # never persisted, so without this every follow-up turn is blind
                    # to what the user attached while the transcript still claims it
                    # was read. Backgrounded: losing the enrichment degrades the next
                    # turn, failing the current one over it would be worse.
                    stored_content = f"{stored_content}\n\n{description_note}"
                    self._spawn_background(
                        self.messages.set_content(session_id, user_seq, stored_content)
                    )
                if isinstance(model_content, str):
                    user_message = {"role": "user", "content": f"{runtime_ctx}\n\n{model_content}"}
                else:
                    # Multimodal: prepend the runtime context as a leading text block.
                    user_message = {
                        "role": "user",
                        "content": [{"type": "text", "text": runtime_ctx}, *model_content],
                    }
                bot_name = bot.name if bot else ""
                bot_role = bot.role_title if bot else ""
                bot_charter = bot.charter if bot else ""

                prompt_messages = self._assembler_for(model_window).assemble(
                    agent.prompt_sections(
                        memory_context,
                        skills_summary=skills_summary,
                        knowledge_summary=knowledge_summary,
                        connectors_summary=connectors_summary,
                        # Pin the session's working plan into the (never-trimmed)
                        # system prompt so the agent keeps the thread across a long
                        # conversation and updates it via the update_plan tool.
                        plan_context=render_plan(session.plan if session else None),
                        bot_charter=bot_charter,
                        bot_name=bot_name,
                        bot_role=bot_role,
                        team_summary=team_summary,
                    ),
                    history,
                    user_message,
                )

                async def _confirm(t_id: str, tool: str, args_preview: str) -> bool:
                    return await self.request_confirmation(session_id, t_id, tool, args_preview)

                # Specialist replies reach the client live as delegation events, but
                # they are only stored as the `delegate` tool's result — a row the
                # transcript endpoint drops. Captured off the event stream so a
                # reloaded conversation still shows who answered what.
                delegations: list[dict[str, Any]] = []

                def _relay(ev: Any) -> None:
                    if isinstance(ev, DelegationStarted):
                        delegations.append(
                            {
                                "role": "assistant",
                                "content": "",
                                "speaker_bot_id": ev.bot_id,
                                "meta": {
                                    "delegation_id": ev.delegation_id,
                                    "delegated_task": ev.task,
                                    "speaker_name": ev.bot_name,
                                    "speaker_role": ev.role_title,
                                },
                            }
                        )
                    elif isinstance(ev, DelegationFinished):
                        pending = next(
                            (
                                d
                                for d in delegations
                                if d["meta"]["delegation_id"] == ev.delegation_id
                            ),
                            None,
                        )
                        if pending is not None:
                            text = ev.text
                            if len(text) > _STORED_TOOL_RESULT_CAP:
                                # Unmarked, a cut-off report reads as a specialist
                                # that gave up mid-sentence — and the `delegate`
                                # tool row holding the rest is dropped by the
                                # transcript endpoint, so this row is all there is.
                                text = text[:_STORED_TOOL_RESULT_CAP] + "\n... (truncated)"
                            pending["content"] = text
                            if ev.artifacts:
                                pending["meta"]["artifacts"] = ev.artifacts
                            if ev.result is not None:
                                pending["meta"]["task_result"] = ev.result
                            if ev.is_error:
                                pending["meta"]["speaker_error"] = True
                    self.bus.publish(session_id, ev)

                # Expose the active session to session-scoped tools (update_plan) for
                # the duration of the turn; reset after so it never leaks to another.
                _session_token = current_session_id.set(session_id)
                _turn_token = current_turn_id.set(turn_id)
                # Same idea for the wall-clock deadline, read by tools that start a
                # nested agent so they can't hand themselves a fresh budget on top
                # of this turn's. Set from the same value the loop enforces.
                budget = agent.loop.max_turn_seconds
                _deadline_token = current_turn_deadline.set(
                    time.monotonic() + budget if budget > 0 else None
                )
                # And for tools whose result the user reads rather than only the
                # model — `delegate` turns a specialist's reply into a message
                # row, so its fallback text has to be in the user's language.
                _locale_token = current_turn_locale.set(locale)
                try:
                    model_used = effective_model or self.settings.llm.model

                    def _on_fallback(model_id: str) -> None:
                        nonlocal model_used
                        model_used = model_id

                    outcome = await agent.loop.run_turn(
                        turn_id,
                        prompt_messages,
                        _relay,
                        model=effective_model,
                        api_key=model_key,
                        api_base=model_base,
                        context_window=model_window,
                        fallback_model=fallback_model,
                        fallback_api_key=fallback_key,
                        fallback_api_base=fallback_base,
                        fallback_context_window=fallback_window,
                        on_fallback=_on_fallback,
                        permission_mode=permission_mode,
                        confirm=_confirm,
                    )
                except ProviderError as exc:
                    detail = str(exc)
                    if is_no_tool_support_error(detail):
                        # Retrying gets the identical rejection every time (the
                        # selected model can never handle a tool-calling
                        # request) — tell the user to switch models instead of
                        # error.llm's generic "please try again".
                        message = t("error.llm_no_tool_support", locale)
                    elif is_no_vision_support_error(detail):
                        # Safety net for a text-only model the registry's proactive
                        # supports_vision() check above doesn't yet know about —
                        # same non-retryable reasoning as the tool-support branch.
                        message = t("error.llm_no_vision_support", locale)
                    else:
                        reason = t(classify_error_reason(detail), locale)
                        message = t("error.llm", locale, reason=reason)
                    self.bus.publish(session_id, TurnError(turn_id=turn_id, message=message))
                    # The user message is already persisted (above); never persist the
                    # error text, so a bad provider response can't poison future
                    # context (legacy #1303).
                    #
                    # Specialist replies are the exception: the user already watched
                    # them arrive, and a provider failure on a later iteration would
                    # otherwise make work that did complete vanish on reload. They
                    # carry speaker_bot_id, so recent() keeps them out of context.
                    answered = [d for d in delegations if d["content"]]
                    if answered:
                        with suppress(SQLAlchemyError):
                            await self.messages.append(session_id, answered)
                    return message
                finally:
                    current_session_id.reset(_session_token)
                    current_turn_id.reset(_turn_token)
                    current_turn_deadline.reset(_deadline_token)
                    current_turn_locale.reset(_locale_token)

                final = outcome.final_content
                # loop.py never appends anything to history for an empty-content
                # turn (an empty assistant message renders as a blank bubble and
                # some providers reject it back in context) — but the fallback
                # text synthesized below IS shown to the user live. Remember to
                # persist that same text further down, or a reload shows the
                # user's message with no reply at all even though they saw one.
                persist_fallback = not final
                # True when `final` no longer matches the assistant message loop.py
                # already appended to history, so the stored copy has to be
                # rewritten below or a reload shows different text than the user saw.
                rewrite_final = False
                if not final:
                    # A turn that ends with nothing to say must still say
                    # something: publishing an empty TurnCompleted renders as a
                    # finished turn with no answer, which is indistinguishable
                    # from the app silently doing nothing.
                    if outcome.timed_out:
                        final = t("error.turn_timeout", locale)
                    elif outcome.reached_max_iterations:
                        final = t("error.max_iterations", locale)
                    elif outcome.finish_reason == "length":
                        final = t("error.truncated", locale)
                    else:
                        final = t("error.empty_response", locale)
                    # A timed-out turn very often HAS produced something — the
                    # files just never got a closing sentence. Naming them turns
                    # "it failed" into "here is what got done", and the chips
                    # attached further down make them openable. Fall back to the
                    # files that don't earn a chip when those are all there is:
                    # "nothing was produced" would be a lie, and their /files/
                    # URLs still work.
                    produced = outcome.artifacts or outcome.hidden_artifacts
                    if produced:
                        shown = produced[:_FALLBACK_ARTIFACTS_SHOWN]
                        extra = len(produced) - len(shown)
                        listed = ", ".join(shown) + (f", +{extra}" if extra > 0 else "")
                        final = f"{final}\n\n{t('error.partial_artifacts', locale, files=listed)}"
                    logger.warning(
                        "Turn {} produced no content (finish_reason={} iterations={} artifacts={})",
                        turn_id,
                        outcome.finish_reason,
                        outcome.iterations,
                        len(produced),
                    )
                elif outcome.timed_out:
                    # The deadline cut the stream off mid-answer. The text is real
                    # and already on screen, so it is kept — but unmarked it reads
                    # as a finished reply that merely trails off, and the next turn
                    # would get it back as history as if the model had said all it
                    # meant to. Mark where it stops instead. (Artifacts are not
                    # listed here the way they are above: this turn DID answer, and
                    # the file chips are attached further down regardless.)
                    final = f"{final}\n\n{t('error.turn_timeout_partial', locale)}"
                    rewrite_final = True
                    logger.warning(
                        "Turn {} was cut off mid-answer by its time budget (iterations={} chars={})",
                        turn_id,
                        outcome.iterations,
                        len(outcome.final_content or ""),
                    )

                # Enforce policy on the model's final output before it leaves the system.
                if self.policy is not None and final:
                    out_decision = self.policy.enforce(final, scope="output")
                    if out_decision.matched_rules:
                        await self.audit.log(
                            "policy",
                            {
                                "scope": "output",
                                "action": out_decision.action,
                                "rules": out_decision.matched_rules,
                            },
                            user_id=user_id,
                            session_id=session_id,
                        )
                    if out_decision.blocked:
                        final = out_decision.message or "Response withheld by the control policy."
                        rewrite_final = True
                    elif out_decision.masked:
                        final = out_decision.text
                        # Persist the masked copy, not the raw one: the transcript is
                        # replayed to the model and re-served on reload, so storing
                        # the unmasked text would hand back exactly what the rule
                        # just removed.
                        rewrite_final = True

                # User message was already persisted at turn start; store only the
                # new assistant/tool messages this turn produced.
                to_store = []
                for msg in outcome.new_messages:
                    entry = dict(msg)
                    if (
                        entry.get("role") == "tool"
                        and len(entry.get("content") or "") > _STORED_TOOL_RESULT_CAP
                    ):
                        entry["content"] = entry["content"][:_STORED_TOOL_RESULT_CAP] + "\n... (truncated)"
                    to_store.append(entry)
                if persist_fallback:
                    # Nothing was appended above for this turn (see the comment
                    # by `persist_fallback`'s assignment) — persist the fallback
                    # explanation itself, post-policy-enforcement, so history
                    # matches what the user actually saw.
                    to_store.append({"role": "assistant", "content": final})
                anchor = next(
                    (
                        e
                        for e in reversed(to_store)
                        if e.get("role") == "assistant" and not e.get("tool_calls")
                    ),
                    None,
                )
                if group is not None and anchor is not None:
                    anchor["meta"] = {**(anchor.get("meta") or {}), "coordinator_bot_id": bot_id,
                                      "speaker_name": bot.name, "speaker_role": "Group leader"}
                if rewrite_final and anchor is not None:
                    anchor["content"] = final
                # Attach artifacts to the final assistant message so they survive a
                # reload (rendered as openable file chips in the UI).
                if outcome.artifacts:
                    if anchor is None:
                        # A turn can create files and still end with no closing
                        # text (the model hits its output cap mid-thought). The
                        # files must stay reachable anyway, so carry them on an
                        # empty assistant message — the same shape the image
                        # tool stores, and what the transcript endpoint keeps.
                        anchor = {"role": "assistant", "content": ""}
                        to_store.append(anchor)
                    anchor["meta"] = {**(anchor.get("meta") or {}), "artifacts": outcome.artifacts}
                if vision_delegate:
                    # Record which model actually read the image, so a reloaded
                    # transcript still explains why a text-only model answered a
                    # question about a picture.
                    if anchor is None:
                        anchor = {"role": "assistant", "content": ""}
                        to_store.append(anchor)
                    anchor["meta"] = {**(anchor.get("meta") or {}), "vision_model": vision_delegate}
                # Added after every `anchor` use above, deliberately: these are
                # assistant rows without tool_calls, so an earlier insert would let
                # rewrite_final/artifacts/vision_model latch onto a specialist's
                # answer whenever the leader ends its turn with no closing text.
                answered = [d for d in delegations if d["content"]]
                if answered:
                    # Slotted in front of the closing summary rather than at index
                    # 0: the leader's "I'll ask X" narration rides on the same
                    # assistant row as the `delegate` call, so prepending would
                    # store the specialist ahead of the sentence that introduced
                    # it and a reload would read backwards.
                    at = next(
                        (
                            i
                            for i in range(len(to_store) - 1, -1, -1)
                            if to_store[i].get("role") == "assistant"
                            and not to_store[i].get("tool_calls")
                        ),
                        len(to_store),
                    )
                    to_store[at:at] = answered
                if to_store:
                    try:
                        await self.messages.append(session_id, to_store)
                    except SQLAlchemyError as exc:
                        raise _TranscriptSaveError from exc

                self.bus.publish(
                    session_id,
                    TurnCompleted(
                        turn_id=turn_id,
                        content=final or "",
                        usage=outcome.usage,
                        artifacts=outcome.artifacts,
                        vision_model=vision_delegate or "",
                        coordinator_bot_id=bot_id if group is not None else None,
                        speaker_name=bot.name if group is not None else "",
                    ),
                )
            except Exception as exc:
                logger.exception("Unhandled turn failure for session {}", session_id)
                # This block wraps the whole turn, so it also catches failures that
                # happen after the model has already answered — including other DB
                # access nested inside the turn (audit.log, a tool querying the DB).
                # Only the transcript write itself is tagged (_TranscriptSaveError,
                # raised at each self.messages.append call site above); a bare
                # SQLAlchemyError from elsewhere in the turn gets the generic
                # message instead of a misleading "your message/answer wasn't
                # saved". Which save point failed changes what's actually true:
                # the pre-turn user-message write never reached the model at
                # all, so it must not claim an answer was generated.
                if isinstance(exc, _TranscriptSaveError):
                    message = (
                        t("error.save_request", locale) if exc.stage == "request" else t("error.save", locale)
                    )
                else:
                    message = t("error.llm", locale, reason=t("reason.internal", locale))
                self.bus.publish(session_id, TurnError(turn_id=turn_id, message=message))
                raise TurnFailed(message) from exc

        logger.info(
            "Turn {} shape: iterations={} tool_calls={} ttft_ms={} duration_ms={} "
            "tool_defs_chars={} prompt_chars={}",
            turn_id,
            outcome.iterations,
            outcome.tool_calls,
            outcome.ttft_ms,
            outcome.duration_ms,
            outcome.tool_defs_chars,
            outcome.prompt_chars,
        )
        if self.usage is not None:
            self._spawn_background(
                self.usage.record(
                    user_id,
                    session_id,
                    model_used,
                    outcome.usage,
                    metrics={
                        "iterations": outcome.iterations,
                        "tool_calls": outcome.tool_calls,
                        "ttft_ms": outcome.ttft_ms,
                        "duration_ms": outcome.duration_ms,
                    },
                )
            )
        self._spawn_background(
            self.memory.maybe_consolidate(user_id, session_id, bot_id=mem_bot_id)
        )
        return final

    async def _delegate_vision(
        self,
        user_id: str,
        session_id: str,
        content: list[dict],
    ) -> tuple[str, str, str] | None:
        """Read this message's images with the operator's kind="vision" model and
        return (model_id, text-only content, storage note) for the chat model to
        answer from.

        The storage note is the same description in a form the transcript keeps,
        so a follow-up turn — which no longer carries the images — still has
        what was in them.

        None means "no delegation happened" — no vision model configured, or the
        read failed — and the caller falls back to refusing the turn. The failure
        is never surfaced as content: a description that is actually an error
        string would be answered from as if it were the image.
        """
        if self.llm_config is None:
            return None
        chosen = await self.llm_config.resolve_vision(user_id)
        if chosen is None:
            return None
        all_images = [b for b in content if b.get("type") == "image_url"]
        images = all_images[:_VISION_MAX_IMAGES]
        if not images:
            return None
        model_id = chosen["model_id"]
        try:
            async with asyncio.timeout(_VISION_TIMEOUT_SECONDS):
                result = await self.provider.chat(
                    [{"role": "user", "content": [*images, {"type": "text", "text": _VISION_PROMPT}]}],
                    model=model_id,
                    max_tokens=_VISION_MAX_TOKENS,
                    api_key=chosen["api_key"] or None,
                    api_base=chosen["api_base"] or None,
                )
        except (ProviderError, TimeoutError) as exc:
            logger.warning("Vision delegation to {} failed: {}", model_id, exc)
            return None
        # Bill the tokens even when the description turns out unusable below —
        # the upstream call was made and charged either way. count_turn=True
        # because the paths that reject a description (blocked, empty) return
        # before the turn's own usage.record ever runs; counting only there
        # would let a user who reliably trips one of them retry a paid upstream
        # call all day without their daily quota ever moving.
        if self.usage is not None and result.usage:
            self._spawn_background(
                self.usage.record(user_id, session_id, model_id, result.usage)
            )
        description = (result.content or "").strip()
        if not description:
            logger.warning("Vision delegation to {} returned no description", model_id)
            return None
        # The description re-enters the prompt as user text, so it goes through the
        # same input policy the typed message did. An image is just another way to
        # put text in front of the model; it must not be the way that skips the
        # guardrails.
        if self.policy is not None:
            decision = self.policy.enforce(description, scope="input")
            if decision.matched_rules:
                await self.audit.log(
                    "policy",
                    {
                        "scope": "input",
                        "source": "vision",
                        "action": decision.action,
                        "rules": decision.matched_rules,
                    },
                    user_id=user_id,
                    session_id=session_id,
                )
            if decision.blocked:
                raise _VisionBlocked(decision.message or "Request blocked by the control policy.")
            if decision.masked:
                description = decision.text
        # A reader that ran out of budget mid-sentence produces a description
        # that reads as complete. Saying it was cut off is the difference
        # between "I only have part of this table" and a confident wrong total.
        truncated = result.finish_reason == "length"
        logger.info(
            "Turn delegated {}/{} image(s) to vision model {} ({} chars{})",
            len(images),
            len(all_images),
            model_id,
            len(description),
            ", truncated" if truncated else "",
        )
        note = vision_note(model_id, len(images), len(all_images), truncated)
        return (
            model_id,
            swap_images_for_description(
                content, description, model_id, described=len(images), truncated=truncated
            ),
            f"{note.strip()}\n{description}",
        )

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
