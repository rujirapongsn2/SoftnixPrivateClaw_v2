"""Multi-tenant agent runtime: many users, many agents, one process.

Each user gets a lightweight ClawAgent (workspace + tools + persona), not an
OS process. Turns are serialized per session — never globally — so hundreds
of concurrent users share the event loop.
"""

import asyncio
import platform
import re
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from claw.config import Settings
from claw.core.bus import EventBus
from claw.core.context import (
    ContextAssembler,
    build_runtime_context,
    build_user_content,
    render_plan,
    swap_images_for_description,
    vision_note,
)
from claw.core.turn_context import current_session_id, current_turn_deadline
from claw.core.events import (
    ToolConfirmRequest,
    ToolConfirmResolved,
    TurnCompleted,
    TurnError,
    TurnStarted,
    ArtifactJobProgress,
)
from claw.core.limits import RateLimiter
from claw.core.loop import AgentLoop
from claw.core.memory import MemoryService
from claw.browser.broker import BrowserBrokerStore
from claw.browser.manager import BrowserManager
from claw.core.connectors import ConnectorManager
from claw.core.connector_presets import list_presets
from claw.core.subagent import SubagentManager
from claw.core.scheduler import SchedulerService
from claw.db.stores import (
    AuditStore,
    ArtifactJobStore,
    KnowledgeStore,
    MessageStore,
    PolicyPlanStore,
    ScheduleStore,
    SessionStore,
    SkillStore,
    UsageStore,
    UserStore,
)
from claw.i18n import (
    classify_error_reason,
    is_no_tool_support_error,
    is_no_vision_support_error,
    locale_for_text,
    t,
)
from claw.providers.base import LLMProvider, ProviderError
from claw.providers.registry import estimated_cost_usd, supports_vision as model_supports_vision
from claw.sandbox.ephemeral import EphemeralSandbox
from claw.security.policy import PolicyEngine
from claw.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from claw.tools.registry import ToolRegistry
from claw.tools.browser import BrowserTool
from claw.tools.documents import build_document_tools
from claw.tools.memory import MemoryTool, RecallMemoryTool
from claw.tools.plan import PlanTool
from claw.tools.shell import ExecTool
from claw.core.builtin_skills import builtin_skills
from claw.tools.knowledge import SearchKnowledgeTool
from claw.tools.dataset import QueryKnowledgeDatasetTool
from claw.tools.skills import ManageSkillTool, ReadSkillTool, build_skills_summary
from claw.tools.spawn import SpawnTool
from claw.tools.web import WebFetchTool, WebSearchTool
from claw.tools.workflow import WorkflowTool
from claw.workflows.service import WorkflowService

if TYPE_CHECKING:
    from claw.db.stores import BlueprintStore, LLMConfigStore

_STORED_TOOL_RESULT_CAP = 4000

# How many filenames a "no answer, but files were produced" message names before
# collapsing to a "+N" count — a long run can touch dozens, and an unbounded list
# would bury the explanation it is appended to.
_FALLBACK_ARTIFACTS_SHOWN = 8

# How long an "ask"-mode tool waits for the user's approve/deny before it is
# auto-declined, so a turn can never hang forever on an unanswered card.
_CONFIRM_TIMEOUT_SECONDS = 600

_ARTIFACT_ACTION_RE = re.compile(
    r"(?:\b(?:create|build|generate|export|write|make|produce|save|convert|deliver|send)\b|"
    r"สร้าง|จัดทำ|ทำไฟล์|ส่งออก|เขียนไฟล์|บันทึกเป็น|แปลงเป็น|ส่งเป็น)",
    re.IGNORECASE,
)
_ARTIFACT_TARGET_RE = re.compile(
    r"(?:\b(?:xlsx|xls|csv|docx|pptx|pdf|zip|html|excel|spreadsheet|workbook|"
    r"file|document|presentation|slides)\b|ไฟล์|เอกสาร|สเปรดชีต|เวิร์กบุ๊ก|สไลด์|งานนำเสนอ)",
    re.IGNORECASE,
)
_ARTIFACT_CLAUSE_SPLIT_RE = re.compile(
    # A dot is a sentence boundary only when followed by whitespace (or end of
    # input); the dot inside names such as report.pdf must stay with its target.
    r"[!?;]+|\.(?=\s|$)|\n+|\s+(?:and then|and|but|then|also|while|as well as|และ|แต่|แล้ว|จากนั้น|โดย|พร้อมกับ)\s+",
    re.IGNORECASE,
)
_INFORMATION_REQUEST_RE = re.compile(
    r"^\s*(?:(?:(?:please|can you|could you|would you)\s+)?(?:help me\s+)?(?:find|search|look for|research|investigate|"
    r"explain|summari[sz]e|review|analy[sz]e|check)\b|"
    r"(?:ช่วย)?(?:หา|ค้นหา|ค้นคว้า|ศึกษา|อธิบาย|สรุป|ตรวจสอบ|วิเคราะห์|รีวิว))",
    re.IGNORECASE,
)
_ARTIFACT_CORE_TOOLS = {
    "generate_workbook",
    "read_skill", "read_file", "read_excel", "read_csv", "read_pdf", "read_docx",
    "list_dir", "write_file", "edit_file", "exec", "update_plan",
}
_ARTIFACT_DISCOVERY_TOOLS = {"web_search", "web_fetch", "search_knowledge", "query_knowledge_dataset"}
_ARTIFACT_SKILL_HINTS = {
    "xlsx": ("xlsx", "xls", "csv", "excel", "spreadsheet", "workbook", "เอ็กซ์เซล", "สเปรดชีต"),
    "docx": ("docx", "word"),
    "pptx": ("pptx", "powerpoint", "slide", "presentation", "สไลด์"),
    "pdf": ("pdf",),
    "html-report": ("html", "dashboard", "แดชบอร์ด"),
}
_GENERIC_ARTIFACT_TERMS = {
    "create", "build", "generate", "export", "write", "make", "produce",
    "file", "report", "data", "edit", "analyze", "สร้าง", "จัดทำ", "รายงาน", "ไฟล์",
}


def _is_artifact_task(content: str, media: list[str] | None = None) -> bool:
    # File types in links, attachments, or unrelated clauses are source context,
    # not proof that the user wants a generated file. Require an explicit output
    # action close to a file target within the same clause.
    text = re.sub(r'https?://\S+', '', content or '', flags=re.IGNORECASE)
    text = re.sub(r"\b(?:do not|don't|never)\s+(?:create|generate|write|make|build)\s+(?:any\s+)?files?\b"
                  r"|(?:ไม่ต้อง|ห้าม)(?:สร้าง|ทำ|จัดทำ)(?:ไฟล์|เอกสาร)", '', text, flags=re.IGNORECASE)
    for clause in _ARTIFACT_CLAUSE_SPLIT_RE.split(text):
        clause = clause.strip()
        if not clause or _INFORMATION_REQUEST_RE.search(clause):
            continue
        # Output markers also cover natural wording such as “ส่งออกเป็น Excel”.
        output_format = re.search(
            r"(?:\b(?:as|into|to|in)\s+(?:a\s+)?|(?:ออก)?เป็น\s*(?:ไฟล์)?|ในรูปแบบ\s*)"
            r"(?:\b(?:xlsx|xls|csv|docx|pptx|pdf|zip|html|excel|spreadsheet|workbook|"
            r"file|document|presentation|slides)\b|ไฟล์|เอกสาร|เอ็กซ์เซล|สเปรดชีต|เวิร์กบุ๊ก|สไลด์)",
            clause,
            re.IGNORECASE,
        )
        action = _ARTIFACT_ACTION_RE.search(clause)
        target = _ARTIFACT_TARGET_RE.search(clause)
        if action and (output_format or (target and abs(action.start() - target.start()) <= 100)):
            return True
    return False


def _checkpoint_tool_names(messages: list[dict]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            name = ((call.get("function") or {}).get("name") or "").strip()
            if name:
                names.add(name)
    return names


def _artifact_tool_scope(
    available: list[str], request: str, checkpoint: list[dict], connector_names: set[str]
) -> set[str]:
    scope = _ARTIFACT_CORE_TOOLS | _ARTIFACT_DISCOVERY_TOOLS | _checkpoint_tool_names(checkpoint)
    lowered = request.lower()
    # Match the configured connector slug, never generic operation fragments
    # such as "read" or "search" from the tail of a generated tool name.
    mentioned = {
        name for name in connector_names
        if name.lower() in lowered or name.lower().replace("_", " ") in lowered
    }
    for connector in mentioned:
        prefixes = (f"mcp_{connector}_".lower(), f"api_{connector}_".lower())
        scope.update(name for name in available if name.lower().startswith(prefixes))
    return scope


def _artifact_skill_scope(skills: list, request: str) -> list:
    """Keep only artifact skills named or implied by the requested output."""
    lowered = request.lower()
    request_terms = {
        term
        for term in re.split(r"[^\w\u0E00-\u0E7F]+", lowered)
        if len(term) >= 4 and term not in _GENERIC_ARTIFACT_TERMS
    }
    wanted = {
        name
        for name, hints in _ARTIFACT_SKILL_HINTS.items()
        if any(hint in lowered for hint in hints)
    }
    if not wanted:
        wanted.add("html-report")
    relevant = []
    for skill in skills:
        skill_text = f"{skill.name} {getattr(skill, 'description', '')}".lower()
        skill_terms = {
            term for term in re.split(r"[^\w\u0E00-\u0E7F]+", skill_text) if len(term) >= 4
        }
        if skill.name in wanted or skill.name.lower() in lowered or request_terms & skill_terms:
            relevant.append(skill)
    return relevant

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
        blueprints: "BlueprintStore | None" = None,
        llm_config: "LLMConfigStore | None" = None,
    ):
        self.user_id = user_id
        self.workspace = workspace
        self.policy = policy
        self.tools = ToolRegistry(on_execute=self._audit_tool)
        if blueprints is not None:
            from sbot.tools.blueprint import SaveBlueprintTool
            self.tools.register(SaveBlueprintTool(blueprints, settings.blueprints_root, workspace, user_id))
        self._audit_store = audit
        # Network mode of the sandbox exec runs, recorded in the security audit
        # trail so admins can see when a command had internet access.
        self._sandbox_network = settings.sandbox.network
        for tool_cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(tool_cls(workspace))
        self.tools.register(ExecTool(sandbox, workspace))
        self.tools.register(WebFetchTool())
        self.tools.register(WebSearchTool())
        if memory is not None:
            self.tools.register(MemoryTool(memory, user_id))
            self.tools.register(RecallMemoryTool(memory, user_id))
        if sessions is not None:
            self.tools.register(PlanTool(sessions))
        subagents = SubagentManager(
            provider=provider,
            sandbox=sandbox,
            workspace=workspace,
            model=settings.llm.model,
            max_tokens=settings.llm.max_tokens,
            max_turn_seconds=settings.llm.max_turn_seconds,
            owner_id=user_id,
            llm_config=llm_config,
        )
        self.tools.register(SpawnTool(subagents))
        self.tools.register(WorkflowTool(WorkflowService(provider, subagents, model=settings.llm.model)))
        # One tool named "browser": when client-extension pairing is enabled, the
        # unified tool prefers the user's paired Chrome and falls back to the
        # server-side browser; otherwise keep the server-side-only tool.
        if browser_broker is not None and settings.browser.client_extension_enabled:
            from claw.tools.client_browser import ClientBrowserTool

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
        from claw.tools.diagram import RenderDiagramTool
        self.tools.register(RenderDiagramTool(workspace, user_id))
        if skills is not None:
            self.tools.register(ReadSkillTool(skills, user_id, workspace=workspace))
            self.tools.register(ManageSkillTool(skills, user_id, workspace=workspace))
        if knowledge is not None:
            self.tools.register(SearchKnowledgeTool(knowledge, user_id))
            self.tools.register(QueryKnowledgeDatasetTool(knowledge, user_id, settings.knowledge_root))
        if schedules is not None:
            from claw.tools.schedule import ScheduleTool

            self.tools.register(ScheduleTool(schedules, scheduler, user_id))
        for doc_tool in build_document_tools(workspace):
            self.tools.register(doc_tool)
        from claw.jobs.workbook import GenerateWorkbookTool
        self.tools.register(GenerateWorkbookTool(workspace))
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
        action = "monitor" if extra.get("exempt") else decision.action
        payload = {"scope": scope, "action": action, "rules": decision.matched_rules, **extra}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._audit_store.log("policy", payload, user_id=self.user_id))

    def _audit_tool(self, name: str, params: dict, result: str) -> None:
        payload = {"tool": name, "params_preview": str(params)[:500], "result_preview": result[:500]}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._audit_store.log("tool_call", payload, user_id=self.user_id))
        # Dedicated security trail for sandbox shell runs — records the command
        # and whether it had network access, so an admin can review every
        # potentially unsafe action (esp. when NETWORK=bridge gives internet).
        if name == "exec":
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

    def system_prompt(
        self,
        memory_context: str,
        persona: str = "",
        skills_summary: str = "",
        knowledge_summary: str = "",
        connectors_summary: str = "",
        plan_context: str = "",
    ) -> str:
        runtime = f"{platform.system()} {platform.machine()}, Python {platform.python_version()}"
        parts = [
            "# Claw Agent\n\n"
            "You are Claw, the user's personal AI agent — a careful, reliable partner "
            "that finishes what it starts.\n\n"
            f"## Runtime\n{runtime}\n\n"
            f"## Workspace\nYour workspace is mounted for file tools; shell commands run "
            f"in an isolated sandbox with the same workspace at /workspace.\n\n"
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
            "Never search the filesystem for your own instructions, skills, or capabilities — "
            "everything you have is already in this prompt or one `read_skill` call away.\n\n"
            "## Guidelines\n"
            "- State intent before tool calls; never claim results before receiving them.\n"
            "- Read a file before modifying it. Analyze tool errors before retrying.\n"
            "- Ask for clarification when the request is ambiguous.\n"
            "- Match the user's language in your replies.\n"
            "- When a task will genuinely take several tool calls, call `update_plan` to record "
            "the goal and steps, and keep it updated as you progress. The plan stays pinned in "
            "your context, so you keep the thread even after earlier messages scroll away — lean "
            "on it instead of losing track of the original request. A request you can answer in "
            "one written reply needs no plan; writing one just delays the answer.\n"
            "- You have long-term memory: when the user shares something worth keeping "
            "(their name, preferences, ongoing work, or asks you to remember something), "
            "call the `remember` tool to save it. Your saved memory is shown under "
            "Long-term Memory below and persists across conversations, so don't claim you "
            "can't remember."
        ]
        # The pinned plan goes right after the base prompt (ahead of persona/memory)
        # so it's the first thing the model orients on each turn.
        if plan_context:
            parts.append(plan_context)
        if persona:
            parts.append(f"# Persona\n\n{persona}")
        if memory_context:
            parts.append(memory_context)
        if skills_summary:
            parts.append(skills_summary)
        if knowledge_summary:
            parts.append(knowledge_summary)
        if connectors_summary:
            parts.append(connectors_summary)
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
        llm_config: "LLMConfigStore | None" = None,
        plans: "PolicyPlanStore | None" = None,
        browser_broker: BrowserBrokerStore | None = None,
        knowledge: "KnowledgeStore | None" = None,
        blueprints: "BlueprintStore | None" = None,
        artifact_jobs: ArtifactJobStore | None = None,
    ):
        self.settings = settings
        self.blueprints = blueprints
        from claw.jobs.runtime_bridge import CheckpointStore
        self.artifact_jobs = CheckpointStore(artifact_jobs) if artifact_jobs else None
        self.durable_jobs = None
        self.provider = provider
        self.llm_config = llm_config
        self.plans = plans
        self.knowledge = knowledge
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
        self.browser_broker = browser_broker
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
        self._plan_rate_limiter = RateLimiter(0)
        self._background: set[asyncio.Task] = set()
        self._inflight = 0
        # session_id -> in-flight turn count, so the UI can show "processing"
        # status in the sidebar even when the client isn't viewing that session.
        self._active_turns: dict[str, int] = {}
        # request_id -> pending confirmation (Ask-mode gate). The awaiting turn
        # holds the future; the WS handler resolves it when the user answers.
        self._confirmations: dict[str, dict] = {}
        self._artifact_tasks: dict[str, asyncio.Task] = {}

    def _artifact_event(self, job: dict, message: str = "") -> ArtifactJobProgress:
        return ArtifactJobProgress(
            turn_id=str(job.get("turn_id") or job.get("id") or ""),
            job_id=str(job.get("id") or ""),
            status=str(job.get("status") or "running"),
            segment=int(job.get("segment") or 1),
            max_segments=int(job.get("max_segments") or self.settings.llm.artifact_job_max_segments),
            elapsed_seconds=float(job.get("elapsed_seconds") or 0),
            token_count=int(job.get("token_count") or 0),
            artifacts=list(job.get("artifacts") or job.get("written") or []),
            message=message,
        )

    async def active_artifact_events(self, user_id: str, session_id: str) -> list[ArtifactJobProgress]:
        if self.artifact_jobs is None:
            return []
        return [self._artifact_event(job) for job in await self.artifact_jobs.list_for_session(user_id, session_id)]

    async def cancel_artifact_job(self, user_id: str, job_id: str) -> bool:
        if self.artifact_jobs is None:
            return False
        job = await self.artifact_jobs.get(job_id)
        if job is None or job.get("user_id") != user_id or job.get("status") not in {
            "queued", "running", "recovering", "waiting_dependency", "blocked"
        }:
            return False
        job = await self.artifact_jobs.finish(job_id, "cancelled") or job
        task = self._artifact_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        self.bus.publish(str(job["session_id"]), self._artifact_event(job, t("artifact.cancelled", str(job.get("locale") or "en"))))
        return True

    async def recover_artifact_jobs(self) -> None:
        """Resume persisted jobs after a dev-server restart."""
        if self.artifact_jobs is None:
            return
        await self.artifact_jobs.prune_finished()
        for job in await self.artifact_jobs.list_recoverable():
            job_id = str(job["id"])
            if job_id in self._artifact_tasks:
                continue
            user = await self.users.get(str(job.get("user_id") or ""))
            session = await self.sessions.get(str(job.get("session_id") or ""))
            if (
                user is None
                or not user.is_active
                or session is None
                or session.user_id != str(job.get("user_id") or "")
            ):
                await self.artifact_jobs.finish(
                    job_id,
                    "failed",
                    error="Artifact recovery ownership check failed.",
                )
                continue
            claimed = await self.artifact_jobs.claim_recovery(job_id)
            if claimed is None:
                continue
            job = claimed
            task = asyncio.create_task(
                self.handle_message(
                    str(job["user_id"]),
                    str(job["session_id"]),
                    str(job.get("content") or ""),
                    channel=str(job.get("channel") or "web"),
                    locale=str(job.get("locale") or "en"),
                    media=list(job.get("media") or []),
                    model=job.get("model") or None,
                    permission_mode=str(job.get("permission_mode") or "auto"),
                    artifact_job_id=job_id,
                )
            )
            self._artifact_tasks[job_id] = task
            self._background.add(task)
            task.add_done_callback(self._background.discard)

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
            browser_broker=self.browser_broker,
            schedules=self.schedules,
            scheduler=self.scheduler,
            memory=self.memory,
            knowledge=self.knowledge,
            sessions=self.sessions,
            blueprints=self.blueprints,
            llm_config=self.llm_config,
        )
        self._agents[user_id] = agent
        self._agents.move_to_end(user_id)
        while len(self._agents) > self.settings.max_resident_agents:
            self._agents.popitem(last=False)
        return agent

    async def warm_connectors(self, user_id: str) -> None:
        """Connect the user's MCP connectors outside a chat turn so their live
        status is accurate for the composer's connector menu. Tools are synced
        into the same per-user registry a chat turn uses, so this both populates
        status and leaves the connector ready. Cheap when already up to date."""
        if self.connectors is None:
            return
        agent = self.get_agent(user_id)
        try:
            await self.connectors.sync_tools(user_id, agent.tools)
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
        artifact_job_id: str | None = None,
    ) -> str | None:
        """Process one user message; tracks in-flight count for graceful shutdown."""
        locale = locale_for_text(locale, content)
        artifact_job: dict | None = None
        # A continuation is tied to this user's session and the retained job,
        # rather than classified as a fresh, ordinary 600-second chat turn.
        if self.artifact_jobs is not None and artifact_job_id is None and re.fullmatch(
            r"\s*(?:continue|resume|ทำต่อ(?:ให้จบ)?|ต่อเลย)\s*[.!]?", content, re.IGNORECASE
        ):
            previous = await self.artifact_jobs.resumable(user_id, session_id)
            if previous:
                artifact_job_id = str(previous["id"])
                content = str(previous.get("content") or content)
                media = list(previous.get("media") or [])
                locale = str(previous.get("locale") or locale)
                model = previous.get("model") or model
                permission_mode = str(previous.get("permission_mode") or "ask")
        checkpoint_content = content
        create_artifact_job = channel == "web" and _is_artifact_task(content, media)
        from claw.jobs.research import is_multistep_research
        shared_kind = ('artifact' if create_artifact_job else
                       'research' if channel == 'web' and is_multistep_research(content) else None)
        if (create_artifact_job or shared_kind) and self.policy is not None:
            checkpoint_decision = self.policy.enforce(content, scope="input")
            if checkpoint_decision.blocked:
                create_artifact_job = False
                shared_kind = None
            elif checkpoint_decision.masked:
                checkpoint_content = checkpoint_decision.text
        if (self.durable_jobs is not None and self.settings.durable_jobs_privateclaw
                and shared_kind and artifact_job_id is None):
            from claw.jobs.runtime_bridge import admit
            await admit(self, user_id, session_id, checkpoint_content, locale, media, model, permission_mode, kind=shared_kind)
            # The worker owns completion. End only the foreground websocket turn.
            self.bus.publish(session_id, TurnCompleted(turn_id=uuid.uuid4().hex[:12], content='', usage={}))
            return None
        if self.artifact_jobs is not None and (
            artifact_job_id is not None or create_artifact_job
        ):
            if artifact_job_id is not None:
                artifact_job = await self.artifact_jobs.get(artifact_job_id)
                if (
                    artifact_job is None
                    or artifact_job.get("user_id") != user_id
                    or artifact_job.get("session_id") != session_id
                ):
                    return None
            else:
                job_policy = await self.artifact_jobs.resource_policy()
                configured_policy = self.settings.team_work
                now = datetime.now(timezone.utc).isoformat()
                artifact_job_id = uuid.uuid4().hex
                artifact_job = await self.artifact_jobs.create(
                    {
                        "id": artifact_job_id,
                        "turn_id": uuid.uuid4().hex[:12],
                        "user_id": user_id,
                        "session_id": session_id,
                        "content": checkpoint_content,
                        "channel": channel,
                        "locale": locale,
                        "media": list(media or []),
                        "model": model or "",
                        "permission_mode": permission_mode,
                        "status": "queued",
                        "segment": 1,
                        "max_segments": self.settings.llm.artifact_job_max_segments,
                        "budget": {
                            # Use the same effective defaults the Control Plane
                            # displays. Falling back to LLMSettings here made a
                            # fresh installation show 5M tokens in the UI while
                            # normal-chat jobs silently stopped at 300K.
                            "seconds": job_policy.get(
                                "max_job_seconds", configured_policy.max_job_seconds
                            ),
                            "tokens": job_policy.get(
                                "max_job_tokens", configured_policy.max_job_tokens
                            ),
                            "extensions": job_policy.get(
                                "max_resource_adjustments",
                                configured_policy.max_resource_adjustments,
                            ) if job_policy.get(
                                "automatic_resources", configured_policy.automatic_resources
                            ) else 0,
                            "recoveries": job_policy.get(
                                "max_step_recoveries", configured_policy.max_step_recoveries
                            ),
                        },
                        "elapsed_seconds": 0.0,
                        "token_count": 0,
                        "cost_usd": 0.0,
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                        "metrics": {
                            "iterations": 0,
                            "tool_calls": 0,
                            "duration_ms": 0,
                            "tool_defs_chars": 0,
                            "prompt_chars": 0,
                            "ttft_ms": 0,
                        },
                        "checkpoint_messages": [],
                        "tool_results": {},
                        "pending_call": None,
                        "written": [],
                        "user_message_persisted": False,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
        from claw.jobs.provider import ForegroundAccounting, foreground_accounting
        from claw.jobs.runtime_bridge import Promoted
        accounting_token = foreground_accounting.set(
            ForegroundAccounting(self.durable_jobs, user_id, session_id) if channel == "web" and self.settings.durable_jobs_privateclaw
            and self.durable_jobs is not None and artifact_job is None else None)
        self._inflight += 1
        self._active_turns[session_id] = self._active_turns.get(session_id, 0) + 1
        try:
            if artifact_job is not None and artifact_job_id is not None:
                current = asyncio.current_task()
                if current is not None:
                    self._artifact_tasks[artifact_job_id] = current
                artifact_job = await self.artifact_jobs.update(artifact_job_id, status="running") or artifact_job
                self.bus.publish(session_id, self._artifact_event(artifact_job, t("artifact.started", locale)))
            result = await self._process_turn(
                user_id, session_id, content, channel, locale, media, model, permission_mode,
                artifact_job=artifact_job,
            )
            # Policy/rate/model-gating failures return before the segmented loop
            # can set a terminal job state. Never leave such a job looking active
            # (and therefore eligible for an incorrect restart recovery).
            if artifact_job_id and self.artifact_jobs is not None:
                latest = await self.artifact_jobs.get(artifact_job_id)
                if latest and latest.get("status") in {"queued", "running", "recovering"}:
                    latest = await self.artifact_jobs.finish(
                        artifact_job_id,
                        "failed",
                        error=result or t("error.empty_response", locale),
                    ) or latest
                    self.bus.publish(
                        session_id,
                        self._artifact_event(latest, str(result or latest.get("error") or "")),
                    )
            return result
        except Promoted as signal:
            self.bus.publish(session_id, TurnCompleted(turn_id=signal.turn_id, content=''))
            return None
        except asyncio.CancelledError:
            if artifact_job_id and self.artifact_jobs is not None:
                latest = await self.artifact_jobs.get(artifact_job_id)
                if latest and latest.get("status") != "cancelled":
                    # A shutdown cancellation retains recovery data; only an
                    # explicit cancel action discards the user's checkpoint.
                    await self.artifact_jobs.update(artifact_job_id, status="queued")
                return t("artifact.cancelled", locale)
            raise
        finally:
            foreground = foreground_accounting.get()
            foreground_accounting.reset(accounting_token)
            if foreground is not None:
                try:
                    await foreground.close()
                except Exception:
                    logger.warning("Foreground journal retained for reconciliation after close failure")
            if artifact_job_id:
                self._artifact_tasks.pop(artifact_job_id, None)
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
        artifact_job: dict | None = None,
    ) -> str | None:
        """Process one user message; events stream to the bus, messages persist to DB.

        Returns the final assistant content (for non-streaming callers/tests).
        """
        turn_id = str(artifact_job.get("turn_id")) if artifact_job else uuid.uuid4().hex[:12]

        # Resolve the caller's usage-tier plan once (None = no plan / unlimited).
        # It governs the per-minute cap, the daily message quota, and the chat
        # model cost ceiling used further down.
        plan = await self.plans.resolve_for_user(user_id) if self.plans is not None else None

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
                model_scope = "global"
                fallback_model: str | None = None
                fallback_key: str | None = None
                fallback_base: str | None = None
                fallback_window: int | None = None
                requested_unavailable = False
                if self.llm_config is not None:
                    requested = model or (session.model if session else None)
                    if requested:
                        resolved = await self.llm_config.resolve(requested, user_id, max_cost=plan_chat_cost)
                        if resolved is not None:
                            effective_model = resolved["model_id"]
                            model_key = resolved["api_key"] or None
                            model_base = resolved["api_base"] or None
                            model_window = resolved["context_window"]
                            model_scope = resolved.get("scope", "global")
                        else:
                            requested_unavailable = True
                    if requested_unavailable:
                        msg = t("error.model_selection_required", locale)
                        self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg, code="model_selection_required"))
                        return msg
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
                    #      only CLAW_LLM__MODEL set would be unusable for every
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
                    # A My Models turn uses the caller's credentials. Do not
                    # silently move it onto the operator-funded global fallback;
                    # that would make a nominally exempt turn consume shared
                    # capacity without a safe point to reserve Plan quota.
                    fallback_resolver = (
                        getattr(self.llm_config, "fallback_model_for", None)
                        if model_scope != "private"
                        else None
                    )
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
                    # A configured-but-disabled lineup must not silently route
                    # through the env default, which may be the same failed model.
                    if effective_model is None and await self.llm_config.has_configured_global_chat_models():
                        available = await self.llm_config.enabled_models(user_id, max_cost=plan_chat_cost)
                        msg = t("error.model_selection_required" if available else "error.no_available_model", locale)
                        self.bus.publish(session_id, TurnError(
                            turn_id=turn_id, message=msg,
                            code="model_selection_required" if available else "no_available_model",
                        ))
                        return msg
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

                # My Models use credentials owned by the caller, so plan-level
                # message and RPM quotas do not apply to those turns. The global
                # RPM backstop remains active to protect shared application
                # resources regardless of who pays the model provider.
                uses_private_model = model_scope == "private"
                if (
                    not uses_private_model
                    and plan
                    and plan["messages_per_day"] > 0
                    and self.usage is not None
                ):
                    used_today = (await self.usage.usage_today(user_id))["plan_turns"]
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
                        self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
                        return msg

                # Separate counters are required: private turns count against
                # the global capacity backstop but must never fill the Plan RPM
                # bucket used by later global-model turns.
                if not self._rate_limiter.allow(user_id):
                    msg = t("error.rate_limited", locale)
                    self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
                    return msg
                plan_rpm = plan["turns_per_minute"] if plan and not uses_private_model else 0
                if plan_rpm and not self._plan_rate_limiter.allow(
                    user_id, per_minute=plan_rpm
                ):
                    msg = t("error.rate_limited", locale)
                    self.bus.publish(session_id, TurnError(turn_id=turn_id, message=msg))
                    return msg
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
                        self.memory.build_context(user_id),
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
                enabled_skills = [
                    *(b for b in builtin_skills() if b.name not in user_names),
                    *user_skills,
                ]
                if artifact_job:
                    enabled_skills = _artifact_skill_scope(enabled_skills, content)
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
                            if names:
                                tool_names_by_skill[s.name] = names
                skills_summary = build_skills_summary(enabled_skills, tool_names_by_skill)
                # Tell the agent which knowledge bases exist so it knows to reach for
                # the search_knowledge tool when a question may be answered by them.
                knowledge_summary = ""
                usable = [b for b in bases if b["docs"] > 0]
                if usable:
                    lines = "\n".join(
                        f"- {b['name']} [{b.get('kind', 'general')}]"
                        + (f": {b['description']}" if b["description"] else "")
                        for b in usable
                    )
                    knowledge_summary = (
                        "# Knowledge bases\n\n"
                        "For general knowledge, use `search_knowledge` and cite the returned passages. "
                        "For queryable knowledge, use `query_knowledge_dataset`: inspect its schema first, "
                        "then run exact filters, grouping or calculations.\n\n" + lines
                    )
                # Tell the agent which integrations exist but aren't connected yet,
                # so it points the user at Settings -> Connectors instead of
                # improvising a workaround with the generic browser/web-fetch tools
                # (e.g. asking them to make a private Google Sheet public).
                connectors_summary = ""
                connected_names: set[str] = set()
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
                if artifact_job:
                    # Artifact segments pay this context cost on every provider
                    # round trip. Retain secondary context only when the request
                    # actually refers to it; otherwise the output skill and scoped
                    # tool schemas are enough for deterministic generation.
                    lowered_request = content.lower()
                    if not any(term in lowered_request for term in ("memory", "remember", "ความจำ", "จำไว้")):
                        memory_context = ""
                    base_named = any(str(base["name"]).lower() in lowered_request for base in usable)
                    if not base_named and not any(
                        term in lowered_request for term in ("knowledge", "ฐานความรู้", "ค้นคว้า", "search")
                    ):
                        knowledge_summary = ""
                    preset_named = any(
                        str(value).lower() in lowered_request
                        for preset in list_presets()
                        for value in (preset["name"], preset["label"])
                    )
                    if not preset_named:
                        connectors_summary = ""
                runtime_ctx = build_runtime_context(channel, locale)
                if artifact_job:
                    runtime_ctx += (
                        "\n\n[Resumable Artifact Job]\n"
                        "Work in explicit phases. Read each skill and source document once, then save normalized "
                        "structured intermediate data (JSON or CSV) in the workspace. Generate the final workbook/file "
                        "from that data with a deterministic script. Validate the generated file before reporting it. "
                        "Completed tool calls are checkpointed; do not repeat a completed phase or create a second copy "
                        "of the same final artifact."
                        " For paginated source documents, follow every next_offset until null, including tables. "
                        "Do not treat an old intermediate dataset as complete until source coverage is verified. "
                        "If a command fails, inspect the error and change the relevant code or input; "
                        "changing only echo/print probes is not a recovery strategy. "
                        "A requested output is complete only after the file exists and can be reopened "
                        "with the expected sheets/sections and source coverage. "
                        "For a source-derived BOM or inventory, preserve only explicit quantities and units; "
                        "mark inferred values as Assumption/TBD, separate optional or alternative components, "
                        "and do not count a broad scope item again when its sub-deliverables are listed. Include "
                        "a source reference and confirmation status per row, then validate source headings, "
                        "quantities, duplicate scope, and source inconsistencies before claiming coverage."
                    )
                    if artifact_job.get("pending_call"):
                        runtime_ctx += (
                            "\nA tool call from the previous process has an uncertain outcome. "
                            "Inspect the workspace/output before continuing and do not repeat that "
                            "same call blindly."
                        )
                model_content, storage_text = build_user_content(content, media, agent.workspace)
                recovering_job = bool(artifact_job and artifact_job.get("user_message_persisted"))
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
                user_seq = 0
                if not recovering_job:
                    try:
                        user_seq = await self.messages.append(
                            session_id, [{"role": "user", "content": stored_content}]
                        )
                    except SQLAlchemyError as exc:
                        raise _TranscriptSaveError("request") from exc
                    if artifact_job and self.artifact_jobs is not None:
                        artifact_job = await self.artifact_jobs.update(
                            str(artifact_job["id"]), user_message_persisted=True, user_seq=user_seq
                        ) or artifact_job
                vision_delegate: str | None = None
                if (
                    not recovering_job
                    and
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
                if recovering_job and artifact_job.get("checkpoint_messages"):
                    user_message = {
                        "role": "user",
                        "content": (
                            f"{runtime_ctx}\n\n[Resume the existing artifact job from its durable checkpoint. "
                            "Do not repeat completed reads or writes. Continue with the next unfinished phase.]"
                        ),
                    }
                elif isinstance(model_content, str):
                    user_message = {"role": "user", "content": f"{runtime_ctx}\n\n{model_content}"}
                else:
                    # Multimodal: prepend the runtime context as a leading text block.
                    user_message = {
                        "role": "user",
                        "content": [{"type": "text", "text": runtime_ctx}, *model_content],
                    }
                prompt_messages = self.assembler.assemble(
                    agent.system_prompt(
                        memory_context,
                        skills_summary=skills_summary,
                        knowledge_summary=knowledge_summary,
                        connectors_summary=connectors_summary,
                        # Pin the session's working plan into the (never-trimmed)
                        # system prompt so the agent keeps the thread across a long
                        # conversation and updates it via the update_plan tool.
                        plan_context=render_plan(session.plan if session else None),
                    ),
                    history,
                    user_message,
                )

                async def _confirm(t_id: str, tool: str, args_preview: str) -> bool:
                    from claw.jobs.provider import current_execution
                    ctx = current_execution.get()
                    if ctx is not None:
                        from claw.jobs.runtime_bridge import Handoff
                        from claw.jobs.contracts import StepOutcome
                        raise Handoff(StepOutcome('paused', checkpoint=ctx.state, reason='approval_required'))
                    return await self.request_confirmation(session_id, t_id, tool, args_preview)

                # Expose the active session to session-scoped tools (update_plan) for
                # the duration of the turn; reset after so it never leaks to another.
                _session_token = current_session_id.set(session_id)
                # Same idea for the wall-clock deadline, read by tools that start a
                # nested agent so they can't hand themselves a fresh budget on top
                # of this turn's. Set from the same value the loop enforces.
                budget = agent.loop.max_turn_seconds
                _deadline_token = current_turn_deadline.set(
                    time.monotonic() + budget if budget > 0 else None
                )
                try:
                    from claw.jobs.provider import current_execution
                    durable_context = current_execution.get()
                    if durable_context is not None:
                        durable_context.count_plan_turn = not uses_private_model
                    from claw.jobs.provider import foreground_accounting
                    foreground = foreground_accounting.get()
                    if foreground is not None:
                        foreground.count_plan_turn = not uses_private_model
                        foreground.state['request'] = dict(content=content, locale=locale,
                            media=list(media or []), model=model, permission_mode=permission_mode,
                            session_id=session_id)
                    model_used = effective_model or self.settings.llm.model
                    artifact_limit_reached = False
                    if artifact_job and self.artifact_jobs is not None:
                        artifact_job = await self.artifact_jobs.update(
                            str(artifact_job["id"]), model=model_used
                        ) or artifact_job

                    def _on_fallback(model_id: str) -> None:
                        nonlocal model_used
                        model_used = model_id

                    checkpoint_messages = list((artifact_job or {}).get("checkpoint_messages") or [])
                    resume_tool_results = dict((artifact_job or {}).get("tool_results") or {})
                    resume_written = list((artifact_job or {}).get("written") or [])
                    segment = int((artifact_job or {}).get("segment") or 1)
                    elapsed_seconds = float((artifact_job or {}).get("elapsed_seconds") or 0)
                    cumulative_cost = float((artifact_job or {}).get("cost_usd") or 0)
                    job_budget = (artifact_job or {}).get("budget") or {}
                    seconds_cap = float(job_budget.get("seconds", self.settings.llm.artifact_job_max_seconds))
                    tokens_cap = int(job_budget.get("tokens", self.settings.llm.artifact_job_max_tokens))
                    extensions = int((artifact_job or {}).get("extensions_used") or 0)
                    recovery_attempts = 0
                    pending_call = (artifact_job or {}).get("pending_call")
                    cumulative_metrics = {
                        "iterations": int(((artifact_job or {}).get("metrics") or {}).get("iterations") or 0),
                        "tool_calls": int(((artifact_job or {}).get("metrics") or {}).get("tool_calls") or 0),
                        "duration_ms": int(((artifact_job or {}).get("metrics") or {}).get("duration_ms") or 0),
                        "tool_defs_chars": int(((artifact_job or {}).get("metrics") or {}).get("tool_defs_chars") or 0),
                        "prompt_chars": int(((artifact_job or {}).get("metrics") or {}).get("prompt_chars") or 0),
                        "ttft_ms": int(((artifact_job or {}).get("metrics") or {}).get("ttft_ms") or 0),
                    }
                    cumulative_usage = {
                        "prompt_tokens": int(((artifact_job or {}).get("usage") or {}).get("prompt_tokens") or 0),
                        "completion_tokens": int(((artifact_job or {}).get("usage") or {}).get("completion_tokens") or 0),
                    }

                    while True:
                        if artifact_job and (
                            elapsed_seconds >= seconds_cap
                            or sum(cumulative_usage.values()) >= tokens_cap
                            or (self.settings.llm.artifact_job_max_cost_usd > 0
                                and cumulative_cost >= self.settings.llm.artifact_job_max_cost_usd)
                            or artifact_job.get("stop_reason") == "budget_exhausted"
                        ):
                            message = t("artifact.limitReached", locale)
                            artifact_job = await self.artifact_jobs.update(
                                str(artifact_job["id"]), status="limit_reached",
                                stop_reason="budget_exhausted",
                            ) or artifact_job
                            self.bus.publish(session_id, self._artifact_event(artifact_job, message))
                            self.bus.publish(session_id, TurnError(turn_id=turn_id, message=message))
                            return message
                        checkpoint_prefix = list(checkpoint_messages)

                        async def _checkpoint(state: dict) -> None:
                            nonlocal artifact_job, pending_call
                            if not artifact_job or self.artifact_jobs is None:
                                if foreground is not None:
                                    await foreground.checkpoint({**foreground.state, 'pending_tool': None, 'runtime': {
                                        'checkpoint_messages': state['messages'], 'tool_results': state['tool_results'],
                                        'written': state['written'], 'pending_call': None,
                                        'user_message_persisted': True}})
                                    from claw.jobs.runtime_bridge import promote_if_planned
                                    await promote_if_planned(self, user_id, session_id, content,
                                        locale, media, model, permission_mode, state, turn_id)
                                return
                            pending_call = None
                            artifact_job = await self.artifact_jobs.update(
                                str(artifact_job["id"]),
                                status="running",
                                segment=segment,
                                checkpoint_messages=[*checkpoint_prefix, *state["messages"]],
                                tool_results=state["tool_results"],
                                written=state["written"],
                                pending_call=None,
                            ) or artifact_job

                        async def _before_tool(call: dict) -> None:
                            nonlocal artifact_job, pending_call
                            if not artifact_job or self.artifact_jobs is None:
                                if foreground is not None:
                                    await foreground.checkpoint({**foreground.state, 'pending_tool': call})
                                return
                            pending_call = call
                            artifact_job = await self.artifact_jobs.update(
                                str(artifact_job["id"]), pending_call=call
                            ) or artifact_job

                        # The first segment may need a connector/source tool we
                        # cannot infer yet. Resumed segments keep only artifact
                        # tools plus tools already used in the checkpoint.
                        scoped_tools = (
                            _artifact_tool_scope(
                                agent.tools.tool_names,
                                content,
                                checkpoint_messages,
                                connected_names,
                            )
                            if artifact_job
                            else None
                        )
                        from claw.jobs.provider import current_execution
                        execution = current_execution.get()
                        if (scoped_tools is not None and execution is not None
                                and execution.lease.spec.acceptance.get('kind') == 'research'):
                            scoped_tools -= {'write_file', 'edit_file', 'exec', 'generate_workbook'}
                        current_turn_deadline.set(
                            time.monotonic() + budget if budget > 0 else None
                        )
                        outcome = await agent.loop.run_turn(
                            turn_id,
                            prompt_messages,
                            lambda ev: self.bus.publish(session_id, ev),
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
                            tool_names=scoped_tools,
                            resume_messages=checkpoint_messages,
                            resume_tool_results=resume_tool_results,
                            resume_written=resume_written,
                            checkpoint=_checkpoint if artifact_job or foreground is not None else None,
                            before_tool=_before_tool if artifact_job or foreground is not None else None,
                            resume_pending_call=pending_call,
                            max_usage_tokens=(
                                max(0, tokens_cap - sum(cumulative_usage.values()))
                                if artifact_job else None
                            ),
                        )
                        segment_usage = dict(outcome.usage)
                        segment_duration_ms = outcome.duration_ms
                        for key in cumulative_usage:
                            cumulative_usage[key] += int(segment_usage.get(key, 0))
                        segment_cost = estimated_cost_usd(model_used, segment_usage)
                        if segment_cost is not None:
                            cumulative_cost += segment_cost
                        cumulative_metrics["iterations"] += outcome.iterations
                        cumulative_metrics["tool_calls"] += outcome.tool_calls
                        cumulative_metrics["duration_ms"] += outcome.duration_ms
                        cumulative_metrics["tool_defs_chars"] = max(
                            cumulative_metrics["tool_defs_chars"], outcome.tool_defs_chars
                        )
                        cumulative_metrics["prompt_chars"] = max(
                            cumulative_metrics["prompt_chars"], outcome.prompt_chars
                        )
                        if not cumulative_metrics["ttft_ms"] and outcome.ttft_ms:
                            cumulative_metrics["ttft_ms"] = outcome.ttft_ms
                        outcome.usage = dict(cumulative_usage)
                        outcome.iterations = cumulative_metrics["iterations"]
                        outcome.tool_calls = cumulative_metrics["tool_calls"]
                        outcome.duration_ms = cumulative_metrics["duration_ms"]
                        outcome.tool_defs_chars = cumulative_metrics["tool_defs_chars"]
                        outcome.prompt_chars = cumulative_metrics["prompt_chars"]
                        outcome.ttft_ms = cumulative_metrics["ttft_ms"]
                        elapsed_seconds += segment_duration_ms / 1000
                        checkpoint_messages = [*checkpoint_prefix, *outcome.new_messages]
                        resume_written = list(dict.fromkeys([*resume_written, *outcome.artifacts, *outcome.hidden_artifacts]))
                        token_count = sum(cumulative_usage.values())
                        if artifact_job and self.artifact_jobs is not None:
                            artifact_job = await self.artifact_jobs.update(
                                str(artifact_job["id"]),
                                segment=segment,
                                elapsed_seconds=elapsed_seconds,
                                token_count=token_count,
                                cost_usd=cumulative_cost,
                                usage=cumulative_usage,
                                metrics=cumulative_metrics,
                                checkpoint_messages=checkpoint_messages,
                                written=resume_written,
                            ) or artifact_job

                        from claw.jobs.runtime_bridge import handoff_outcome
                        await handoff_outcome(outcome, agent.workspace)
                        if outcome.blocked_reason:
                            message = t("artifact.sandboxBlocked", locale)
                            if artifact_job and self.artifact_jobs is not None:
                                artifact_job = await self.artifact_jobs.update(
                                    str(artifact_job["id"]), status="waiting_dependency",
                                    blocked_reason=outcome.blocked_reason,
                                ) or artifact_job
                                self.bus.publish(session_id, self._artifact_event(artifact_job, message))
                                # Probe capability, never rerun the user's side effect.
                                ready = False
                                for attempt in range(int(job_budget.get("recoveries", 2))):
                                    if recovery_attempts >= int(job_budget.get("recoveries", 2)):
                                        break
                                    recovery_attempts += 1
                                    wait = min(30, 5 * (2 ** attempt))
                                    if elapsed_seconds + wait >= seconds_cap:
                                        break
                                    await asyncio.sleep(wait)
                                    probe_started = time.monotonic()
                                    probe = await self.sandbox.run("true", agent.workspace)
                                    elapsed_seconds += wait + time.monotonic() - probe_started
                                    if probe.exit_code == 0 and not probe.timed_out:
                                        ready = True
                                        break
                                if ready and elapsed_seconds < seconds_cap:
                                    resume_tool_results = dict(artifact_job.get("tool_results") or {})
                                    artifact_job = await self.artifact_jobs.update(
                                        str(artifact_job["id"]), status="running",
                                        elapsed_seconds=elapsed_seconds, blocked_reason="",
                                    ) or artifact_job
                                    self.bus.publish(session_id, self._artifact_event(artifact_job, t("artifact.resuming", locale, segment=segment)))
                                    continue
                                artifact_job = await self.artifact_jobs.update(
                                    str(artifact_job["id"]), status="blocked", elapsed_seconds=elapsed_seconds,
                                ) or artifact_job
                                self.bus.publish(session_id, self._artifact_event(artifact_job, message))
                            await self.messages.append(session_id, [{"role": "assistant", "content": message}])
                            self.bus.publish(session_id, TurnError(turn_id=turn_id, message=message))
                            return message

                        if outcome.usage_limit_reached:
                            artifact_limit_reached = True
                            artifact_job = await self.artifact_jobs.update(
                                str(artifact_job["id"]), stop_reason="budget_exhausted"
                            ) or artifact_job
                            break

                        if not (
                            artifact_job
                            and (outcome.timed_out or outcome.reached_max_iterations)
                        ):
                            break
                        can_resume = (
                            elapsed_seconds < seconds_cap
                            and token_count < tokens_cap
                            and (
                                self.settings.llm.artifact_job_max_cost_usd <= 0
                                or cumulative_cost < self.settings.llm.artifact_job_max_cost_usd
                            )
                        )
                        if segment >= int(artifact_job.get("max_segments") or 1):
                            # Extensions require a newly produced file, not a
                            # successful-looking infrastructure error or retry.
                            progressed = bool(set(resume_written) - set(artifact_job.get("extension_files") or []))
                            if can_resume and progressed and extensions < int(job_budget.get("extensions", 0)):
                                extensions += 1
                                artifact_job = await self.artifact_jobs.update(
                                    str(artifact_job["id"]), max_segments=segment + 1,
                                    extensions_used=extensions, extension_files=resume_written,
                                ) or artifact_job
                            else:
                                can_resume = False
                        if not can_resume:
                            artifact_limit_reached = True
                            artifact_job = await self.artifact_jobs.update(
                                str(artifact_job["id"]), stop_reason="budget_exhausted"
                            ) or artifact_job
                            break
                        segment += 1
                        resume_tool_results = dict(artifact_job.get("tool_results") or resume_tool_results)
                        artifact_job = await self.artifact_jobs.update(
                            str(artifact_job["id"]), segment=segment, status="running"
                        ) or artifact_job
                        self.bus.publish(
                            session_id,
                            self._artifact_event(artifact_job, t("artifact.resuming", locale, segment=segment)),
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
                        reason_key = (
                            "reason.provider_unavailable"
                            if exc.error_type == "provider_unavailable"
                            else classify_error_reason(detail)
                        )
                        if reason_key == "reason.provider_unavailable":
                            message = t("error.provider_unavailable", locale)
                        else:
                            reason = t(reason_key, locale)
                            message = t("error.llm", locale, reason=reason)
                    self.bus.publish(session_id, TurnError(turn_id=turn_id, message=message))
                    if artifact_job and self.artifact_jobs is not None:
                        artifact_job = await self.artifact_jobs.finish(
                            str(artifact_job["id"]), "failed", error=message
                        ) or artifact_job
                        self.bus.publish(session_id, self._artifact_event(artifact_job, message))
                    # The user message is already persisted (above); never persist the
                    # error text, so a bad provider response can't poison future
                    # context (legacy #1303).
                    return message
                finally:
                    current_session_id.reset(_session_token)
                    current_turn_deadline.reset(_deadline_token)

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
                        final = t("artifact.limitReached", locale) if artifact_limit_reached else t("error.turn_timeout", locale)
                    elif outcome.usage_limit_reached:
                        final = t("artifact.limitReached", locale)
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
                    marker = "artifact.limitReachedPartial" if artifact_limit_reached else "error.turn_timeout_partial"
                    final = f"{final}\n\n{t(marker, locale)}"
                    rewrite_final = True
                    logger.warning(
                        "Turn {} was cut off mid-answer by its time budget (iterations={} chars={})",
                        turn_id,
                        outcome.iterations,
                        len(outcome.final_content or ""),
                    )
                elif outcome.interrupted:
                    final = f"{final}\n\n{t('error.provider_stream_partial', locale)}"
                    rewrite_final = True
                    logger.warning(
                        "Turn {} kept a partial answer after its provider stream was interrupted "
                        "(iterations={} chars={})",
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
                from claw.jobs.runtime_bridge import handoff_delivery
                await handoff_delivery(agent.workspace, final, outcome.artifacts)
                if foreground is not None:
                    await foreground.deliver(to_store, metrics={
                        'iterations': outcome.iterations, 'tool_calls': outcome.tool_calls,
                        'ttft_ms': outcome.ttft_ms, 'duration_ms': outcome.duration_ms})
                elif to_store:
                    try:
                        await self.messages.append(session_id, to_store)
                    except SQLAlchemyError as exc:
                        raise _TranscriptSaveError from exc

                if artifact_job and self.artifact_jobs is not None:
                    final_status = "limit_reached" if artifact_limit_reached else "completed"
                    artifact_job = await self.artifact_jobs.finish(
                        str(artifact_job["id"]),
                        final_status,
                        written=resume_written,
                        artifacts=outcome.artifacts,
                        usage=cumulative_usage,
                        cost_usd=cumulative_cost,
                        metrics=cumulative_metrics,
                    ) or artifact_job
                    self.bus.publish(
                        session_id,
                        self._artifact_event(
                            artifact_job,
                            t("artifact.limitReached", locale)
                            if artifact_limit_reached
                            else t("artifact.completed", locale),
                        ),
                    )

                self.bus.publish(
                    session_id,
                    TurnCompleted(
                        turn_id=turn_id,
                        content=final or "",
                        usage=outcome.usage,
                        artifacts=outcome.artifacts,
                        vision_model=vision_delegate or "",
                    ),
                )
            except Exception as exc:
                from claw.jobs.store import BudgetExhausted, LeaseLost
                if isinstance(exc, (BudgetExhausted, LeaseLost)):
                    raise
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
                if artifact_job and self.artifact_jobs is not None:
                    artifact_job = await self.artifact_jobs.finish(
                        str(artifact_job["id"]), "failed", error=message
                    ) or artifact_job
                    self.bus.publish(session_id, self._artifact_event(artifact_job, message))
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
        if self.usage is not None and not (foreground and foreground.delivered):
            self._spawn_background(
                self.usage.record(
                    user_id,
                    session_id,
                    model_used,
                    outcome.usage,
                    count_plan_turn=not uses_private_model,
                    metrics={
                        "iterations": outcome.iterations,
                        "tool_calls": outcome.tool_calls,
                        "ttft_ms": outcome.ttft_ms,
                        "duration_ms": outcome.duration_ms,
                    },
                )
            )
        self._spawn_background(self.memory.maybe_consolidate(user_id, session_id))
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
        from contextvars import copy_context
        from claw.jobs.provider import foreground_accounting
        background_context = copy_context()
        background_context.run(foreground_accounting.set, None)
        task = asyncio.create_task(self._guard(coro), context=background_context)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    async def _guard(coro) -> None:
        try:
            await coro
        except Exception:
            logger.exception("Background task failed")
