"""Chief of Staff (CoS) specialized tools.

Enables the Chief of Staff bot to orchestrate the team:
- list_bots: list specialist bots available in the team
- create_bot: create a new specialist bot configuration
- create_bots: create a bounded set of specialist bot configurations
- delegate: delegate a task to a specialist bot synchronously
"""

import asyncio
import difflib
import itertools
import json
import time
import uuid
from typing import Any

from loguru import logger

from sbot.core.specialist import (
    DELEGATABLE_TOOLS,
    SPECIALIST_ALWAYS_TOOLS,
    SpecialistRunner,
    specialist_progress,
)
from sbot.core.task_result import TaskResult
from sbot.tools.finish_step import FinishStepTool
from sbot.core.turn_context import current_turn_locale, current_turn_deadline
from sbot.db.stores import BotStore
from sbot.i18n import t
from sbot.tools.base import Tool

__all__ = [
    "DELEGATABLE_TOOLS",
    "MAX_BOTS_PER_OWNER",
    "MAX_PARALLEL_DELEGATIONS",
    "CreateBotTool",
    "CreateBotsTool",
    "DelegateManyTool",
    "DelegateTool",
    "ListBotsTool",
]

# A team this large is a runaway loop, not a team. Bounded because create_bot is
# driven by a model and each row costs a prompt on every list_bots call.
MAX_BOTS_PER_OWNER = 50

# How much of a mistyped bot id still resolves. A dropped or swapped character
# in 32 hex ones scores ~0.97, while two unrelated ids score ~0.2 — so this sits
# far from both, and a match this close to only one bot on the team is the one
# the model was copying.
_ID_MATCH_CUTOFF = 0.85

# How many specialists one `delegate_many` may run at once. Each is a nested
# agent loop against the same provider and the same workspace, so this is a
# fan-out bound, not a scheduling one.
MAX_PARALLEL_DELEGATIONS = 4


class ListBotsTool(Tool):
    name = "list_bots"
    description = (
        "List all available bots in the user's team along with their roles, charters, and capabilities. "
        "Chief of Staff should call this to see who is available to assign tasks to."
    )
    parameters = {
        "type": "object",
        "properties": {
            "include_archived": {"type": "boolean", "default": False},
        },
    }

    def __init__(self, bot_store: BotStore, owner_id: str, member_ids: frozenset[str] | None = None):
        self.bot_store = bot_store
        self.owner_id = owner_id
        self.member_ids = member_ids

    async def execute(self, include_archived: bool = False, **_: Any) -> str:
        bots = await self.bot_store.list_for_user(self.owner_id, include_archived=include_archived)
        if self.member_ids is not None:
            bots = [b for b in bots if b.id in self.member_ids]
        if not bots:
            return "No bots found."
        items = []
        for b in bots:
            items.append({
                "id": b.id,
                "name": b.name,
                "role_title": b.role_title,
                "kind": b.kind,
                "charter": b.charter,
                "tool_allowlist": b.tool_allowlist,
                "intrinsic_tools": sorted(SPECIALIST_ALWAYS_TOOLS),
                # So a delegation can be aimed at the specialist that actually
                # holds the relevant skill, instead of at whoever sounds right.
                "skill_ids": b.skill_ids,
            })
        return json.dumps(items, ensure_ascii=False, indent=2)


class CreateBotTool(Tool):
    name = "create_bot"
    # Creating a team member is itself the requested action. Ending the turn
    # here prevents a model from treating the new bot as an invitation to plan,
    # delegate a smoke test, create files, or mutate the user's plan.
    ends_turn_on_success = True
    description = (
        "Create a new specialist bot for the user's team. "
        "Use only when the user asked to create a member. This completes that request: "
        "do not test, delegate to, plan work for, or otherwise use the new bot unless the "
        "user explicitly requested that as a separate action."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The bot's display name, e.g. 'นักวิจัย' or 'SEO Expert'"},
            "role_title": {"type": "string", "description": "The title/role, e.g. 'Content Marketer' or 'QA Engineer'"},
            "charter": {"type": "string", "description": "Detailed persona and instructions for this bot"},
            "tool_allowlist": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Built-in tool names (including project) or exact enabled mcp_* connector tool names.",
            },
            "skill_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional: names of the skills this bot should see, from your own skills "
                    "list. Omit to give it every skill; use this to keep a narrow specialist's "
                    "prompt focused. It can still open any skill by name with read_skill."
                ),
            },
        },
        "required": ["name", "role_title", "charter"],
    }
    # No `model` parameter on purpose. A bot's model is resolved on the
    # specialist path without the caller's plan cost ceiling, so letting Chief
    # of Staff choose one would be a way to reach a pricier model than the
    # user's tier allows. Setting it stays with the user, in Settings.

    def __init__(self, bot_store: BotStore, owner_id: str, creator_bot_id: str, connectors=None):
        self.bot_store = bot_store
        self.owner_id = owner_id
        self.creator_bot_id = creator_bot_id
        self.connectors = connectors

    async def execute(
        self,
        name: str,
        role_title: str,
        charter: str,
        tool_allowlist: list[str] | None = None,
        skill_ids: list[str] | None = None,
        **_: Any,
    ) -> str:
        name = name.strip()
        if not name:
            return "Error: name is required."

        if skill_ids is not None:
            # Kept as given rather than resolved to rows: `scope_skills` matches
            # on id or name because built-in skills are code-defined and have no
            # row to point at, so a name is a legitimate value here. A name that
            # matches nothing narrows the bot to fewer skills, which is a worse
            # bot but not a broken one — unlike an unknown tool, it cannot grant
            # anything, so it is clamped rather than rejected.
            skill_ids = [s.strip() for s in skill_ids if str(s).strip()]

        if tool_allowlist is not None:
            available = set(DELEGATABLE_TOOLS)
            if self.connectors is not None:
                from sbot.tools.registry import ToolRegistry
                connected = ToolRegistry()
                await self.connectors.sync_tools(self.owner_id, connected)
                available.update(connected.tool_names)
            unknown = sorted(set(tool_allowlist) - available)
            if unknown:
                return (
                    f"Error: cannot grant {unknown} — a specialist may only be given "
                    f"{sorted(DELEGATABLE_TOOLS)}."
                )

        existing = await self.bot_store.get_by_name(self.owner_id, name)
        if existing:
            return f"Bot with name '{name}' already exists (id: {existing.id}, role: {existing.role_title})."

        if await self.bot_store.count_for_user(self.owner_id) >= MAX_BOTS_PER_OWNER:
            return (
                f"Error: the team already has the maximum of {MAX_BOTS_PER_OWNER} bots. "
                "Reuse or archive an existing specialist instead of creating another."
            )

        bot = await self.bot_store.create(
            owner_id=self.owner_id,
            name=name,
            role_title=role_title.strip(),
            charter=charter.strip(),
            tool_allowlist=tool_allowlist,
            skill_ids=skill_ids,
            kind="specialist",
            created_by=f"bot:{self.creator_bot_id}",
        )
        return (
            f"สร้างบอท '{bot.name}' เรียบร้อยแล้ว "
            f"(บทบาท: {bot.role_title}, รหัส: {bot.id}). "
            "ยังไม่ได้มอบหมายหรือทดสอบงานให้บอทนี้."
        )


class CreateBotsTool(CreateBotTool):
    """Create several explicitly requested specialists as one bounded action.

    The single-bot tool ends the turn by design.  Reusing that behavior for a
    user-requested roster used to silently discard every bundled call after the
    first one.  This tool validates the full batch before writing anything, so
    a malformed or duplicate member never leaves a partial team behind.
    """

    name = "create_bots"
    ends_turn_on_success = True
    _MAX_BATCH_SIZE = 10
    description = (
        "Create multiple specialist bots in one requested roster. Use this instead of repeated "
        "create_bot calls when the user explicitly names two or more bots. This completes only "
        "the creation request; do not test, delegate to, plan work for, or otherwise use them."
    )
    parameters = {
        "type": "object",
        "properties": {
            "bots": {
                "type": "array",
                "minItems": 2,
                "maxItems": _MAX_BATCH_SIZE,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "role_title": {"type": "string"},
                        "charter": {"type": "string"},
                        "tool_allowlist": {"type": "array", "items": {"type": "string"}},
                        "skill_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "role_title", "charter"],
                },
            },
        },
        "required": ["bots"],
    }

    async def execute(self, bots: Any, **_: Any) -> str:
        if not isinstance(bots, list) or not 2 <= len(bots) <= self._MAX_BATCH_SIZE:
            return f"Error: provide between 2 and {self._MAX_BATCH_SIZE} bots."
        if not all(isinstance(item, dict) for item in bots):
            return "Error: every bot must include name, role_title, and charter."

        normalized: list[dict[str, Any]] = []
        names: set[str] = set()
        for item in bots:
            name = str(item.get("name") or "").strip()
            role_title = str(item.get("role_title") or "").strip()
            charter = str(item.get("charter") or "").strip()
            if not name or not role_title or not charter:
                return "Error: every bot needs a non-empty name, role_title, and charter."
            key = name.casefold()
            if key in names:
                return f"Error: duplicate bot name '{name}' in this request."
            names.add(key)
            normalized.append({
                "name": name,
                "role_title": role_title,
                "charter": charter,
                "tool_allowlist": item.get("tool_allowlist"),
                "skill_ids": item.get("skill_ids"),
            })

        existing_count = await self.bot_store.count_for_user(self.owner_id)
        if existing_count + len(normalized) > MAX_BOTS_PER_OWNER:
            return (
                f"Error: creating {len(normalized)} bots would exceed the maximum of "
                f"{MAX_BOTS_PER_OWNER} bots for this team."
            )
        for item in normalized:
            existing = await self.bot_store.get_by_name(self.owner_id, item["name"])
            if existing:
                return f"Error: bot with name '{item['name']}' already exists (id: {existing.id})."

        available = set(DELEGATABLE_TOOLS)
        if self.connectors is not None:
            from sbot.tools.registry import ToolRegistry
            connected = ToolRegistry()
            await self.connectors.sync_tools(self.owner_id, connected)
            available.update(connected.tool_names)
        for item in normalized:
            allowed = item["tool_allowlist"]
            if allowed is not None:
                unknown = sorted(set(allowed) - available)
                if unknown:
                    return f"Error: cannot grant {unknown} to '{item['name']}'."
            skills = item["skill_ids"]
            if skills is not None:
                item["skill_ids"] = [str(skill).strip() for skill in skills if str(skill).strip()]

        created = []
        for item in normalized:
            bot = await self.bot_store.create(
                owner_id=self.owner_id,
                name=item["name"],
                role_title=item["role_title"],
                charter=item["charter"],
                tool_allowlist=item["tool_allowlist"],
                skill_ids=item["skill_ids"],
                kind="specialist",
                created_by=f"bot:{self.creator_bot_id}",
            )
            created.append(f"- {bot.name} ({bot.role_title}, รหัส: {bot.id})")
        return "สร้างบอทครบ {} คนเรียบร้อยแล้ว:\n{}\nยังไม่ได้มอบหมายหรือทดสอบงานให้บอทเหล่านี้.".format(
            len(created), "\n".join(created)
        )


class DelegateTool(Tool):
    name = "delegate"
    description = (
        "Delegate a specific task to a specialist bot synchronously and wait for their response. "
        "Use this for tasks that require the specialist's expertise, charter, or domain knowledge. "
        "For file tasks list every required_file and the requested delivery_target. Supply input_files "
        "for existing files. Use verification for code tests or important sourced research. "
        "Report incomplete results explicitly; do not repeat the whole assignment."
    )
    parameters = {
        "type": "object",
        "properties": {
            "bot_name": {"type": "string", "description": "The name of the target bot, e.g. 'นักวิจัย'"},
            "bot_id": {"type": "string", "description": "Optional: exact bot id if known"},
            "task": {"type": "string", "description": "Clear and self-contained instructions for the specialist"},
            "context": {"type": "string", "description": "Optional context or background information"},
            "verification": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["code", "research"]},
                "test_command": {"type": "array", "items": {"type": "string"}},
                "source_urls": {"type": "array", "items": {"type": "string"}},
            }, "required": ["kind"]},
            "input_files": {"type": "array", "items": {"type": "string"}},
            "required_files": {"type": "array", "items": {"type": "string"}},
            "acceptance_criteria": {"type": "string"},
            "delivery_target": {"type": "object", "properties": {
                "workspace_id": {"type": "string"},
                "paths": {"type": "object", "additionalProperties": {"type": "string"}},
            }, "required": ["workspace_id"]},
        },
        "required": ["task"],
    }
    # A delegation runs a whole nested agent loop, so it is one of the longest
    # tool calls there is; without this the UI shows a single opaque spinner for
    # minutes. (The time budget comes from current_turn_deadline, not the
    # `deadline` kwarg, so the specialist is clamped either way.)
    wants_progress = True

    def __init__(
        self,
        bot_store: BotStore,
        owner_id: str,
        provider: Any,
        sandbox: Any,
        workspace: Any,
        model: str | None = None,
        llm_config: Any = None,
        skills: Any = None,
        memory: Any = None,
        mirror: Any = None,
        leader_bot_id: str | None = None,
        member_ids: frozenset[str] | None = None,
        connectors: Any = None,
        project_access: Any = None,
        arg_guard: Any = None,
        reliability: Any = None,
        llm_settings: Any = None,
    ):
        self.bot_store = bot_store
        self.owner_id = owner_id
        from sbot.config import ReliabilitySettings
        self.reliability = reliability or ReliabilitySettings()
        # Optional on purpose: a delegation is delivered through `progress`, and
        # the mirror is only a second view of it. Missions and tests run the
        # same tool with no session to mirror into.
        self.local_broker = None
        self.mirror = mirror
        self.leader_bot_id = leader_bot_id
        self.member_ids = member_ids
        self.runner = SpecialistRunner(
            provider=provider,
            sandbox=sandbox,
            workspace=workspace,
            model=model,
            llm_settings=llm_settings,
            llm_config=llm_config,
            owner_id=owner_id,
            skills=skills,
            memory=memory,
            connectors=connectors,
            project_access=project_access,
            arg_guard=arg_guard,
        )

    def _delegatable(self, bot: Any) -> bool:
        """Whether this bot may be handed work — i.e. it is not the leader.

        A leader that delegates to itself runs its own turn nested inside
        itself, and the mirror then writes that nested run into the very
        session the user is watching: a fabricated user message, the specialist
        stream inside the leader's own bubble, and a `turn_completed` that ends
        the real turn early. Both ways of naming the leader are excluded, since
        a user has one Chief of Staff but a delegation can also arrive from a
        mission with no leader id at all — and the prompt's roster already
        leaves chiefs of staff out, so this only makes the tool agree with it.
        """
        if bot is None:
            return False
        if self.member_ids is not None and bot.id not in self.member_ids:
            return False
        if self.member_ids is None and getattr(bot, "kind", "") == "chief_of_staff":
            return False
        return bot.id != self.leader_bot_id

    async def _team(self) -> list[Any]:
        return [b for b in await self.bot_store.list_for_user(self.owner_id) if self._delegatable(b)]

    async def _resolve_target(self, bot_id: str | None, bot_name: str | None) -> Any:
        """Find the specialist the model meant, not just the one it spelled right.

        Both fields come from the model, so every lookup here is owner-scoped —
        an unfiltered one would delegate into another tenant's bot — and every
        path goes through `_delegatable`, including the near-match: a resolution
        that lands back on the leader is the one result that must never be
        returned, however it was spelled.

        An id is 32 random hex characters that the model copies out of
        `list_bots` by hand, and dropping one of them cost a real delegation: it
        gave up on the specialist entirely, did the work itself, and told the
        user the bot was broken. So a miss on the id falls through to the name
        instead of ending the call, and a near-miss on the id resolves as long
        as exactly one bot on the team is close enough that there is nothing to
        confuse it with. Ids are random, so two of them landing that close is
        not a collision the team can realistically produce.
        """
        if bot_id:
            exact = await self.bot_store.get(bot_id, self.owner_id)
            if self.member_ids is not None and exact is not None and exact.id not in self.member_ids:
                return None
            if self._delegatable(exact):
                return exact
        if bot_name:
            exact = await self.bot_store.get_by_name(self.owner_id, bot_name)
            if self.member_ids is not None and exact is not None and exact.id not in self.member_ids:
                return None
            if self._delegatable(exact):
                return exact
        if not bot_id and not bot_name:
            return None

        roster = await self._team()
        if bot_name:
            wanted = bot_name.strip().casefold()
            for bot in roster:
                if bot.name.casefold() == wanted:
                    return bot
        if bot_id:
            by_id = {bot.id: bot for bot in roster}
            near = difflib.get_close_matches(bot_id, list(by_id), n=2, cutoff=_ID_MATCH_CUTOFF)
            if len(near) == 1:
                logger.warning("Delegate: resolved mistyped bot id {} to {}", bot_id, near[0])
                return by_id[near[0]]
        return None

    async def execute(
        self,
        task: str,
        bot_name: str | None = None,
        bot_id: str | None = None,
        context: str = "",
        progress: Any = None,
        next_substep: Any = None,
        required_files: list[str] | None = None,
        acceptance_criteria: str = "",
        delivery_target: dict | None = None,
        input_files: list[str] | None = None,
        verification: dict | None = None,
        **_: Any,
    ) -> str:
        if required_files is not None and (not isinstance(required_files, list) or
                any(not isinstance(path, str) or not path.strip() for path in required_files)):
            return "Error: required_files must be a list of nonempty paths."
        if delivery_target is not None and (not isinstance(delivery_target, dict) or not required_files):
            return "Error: delivery_target requires an object and explicit required_files."
        if verification is not None and (not isinstance(verification, dict) or
                verification.get('kind') not in {'code', 'research'}):
            return 'Error: verification.kind must be code or research.'
        runner = self.runner
        target_bot = await self._resolve_target(bot_id, bot_name)

        if target_bot is None:
            roster = await self._team()
            available = ", ".join(f"{bot.name} ({bot.id})" for bot in roster) or "none"
            if not roster:
                # Not "not found": the team really is empty, and a leader told to
                # pick someone else goes looking for a specialist that does not
                # exist instead of doing the work.
                return (
                    "Error: You have no specialists to delegate to — this work is yours to do. "
                    "Create one with `create_bot` only if the user asked for it."
                )
            # Naming the team inline rather than pointing at list_bots: the model
            # that got here already has the roster and still missed, so another
            # lookup round-trip is the one thing it has shown it will not do.
            return (
                f"Error: Specialist bot '{bot_name or bot_id}' not found. "
                f"You cannot delegate to yourself. "
                f"Delegate to one of these instead: {available}"
            )

        input_manifest = {}
        root_workspace = runner.workspace
        if self.reliability.isolated_assignments and self.reliability.enabled_for(self.owner_id):
            from sbot.core.assignment_workspace import isolate_runner
            try:
                runner, input_manifest = isolate_runner(runner, input_files or [])
            except (ValueError, OSError) as exc:
                return f'Error: cannot isolate assignment: {exc}'
        # The handoff is rendered as bot-to-bot communication, not as a tool
        # call, so the UI needs the bot that was actually resolved rather than
        # the name the model typed. Emitted before the run so the card can show
        # who is working while they still are.
        emit_event = progress if callable(progress) else lambda _payload: None
        # The same bot can be delegated to more than once in a turn, and a
        # reconnect replays the turn — so the pair needs an identity of its own
        # for the client and the store to match a reply to its assignment.
        delegation_id = uuid.uuid4().hex[:12]
        emit_event(
            {
                "kind": "delegation_started",
                "delegation_id": delegation_id,
                "bot_id": target_bot.id,
                "bot_name": target_bot.name,
                "role_title": target_bot.role_title,
                "task": task,
            }
        )

        system_prompt = (
            f"You are {target_bot.name}, {target_bot.role_title}.\n\n"
            f"# Charter\n{target_bot.charter}\n\n"
            "You are working on a delegated task from Chief of Staff. Complete it thoroughly and return your findings. "
            "For file deliverables, use publish_artifact to attach every requested file, even when it already existed. "
            "This tool and document-reading tools are available independently of your configured tool allowlist."
        )
        specialist_context = await runner.context_block(target_bot)
        if specialist_context:
            system_prompt = f"{system_prompt}\n\n{specialist_context}"

        user_prompt = task if not context else f"{task}\n\nContext:\n{context}"

        if input_manifest:
            user_prompt += '\nRead-only input snapshots (original path -> available path): ' + json.dumps(input_manifest)
        from sbot.core.delivery import LocalDelivery
        delivery = LocalDelivery(self.local_broker, self.owner_id, delivery_target, runner.arg_guard) if delivery_target else None
        mode = self.reliability.verification_mode if self.reliability.enabled_for(self.owner_id) else 'off'
        from sbot.core.deep_verification import TaskVerifier
        verifier = TaskVerifier(runner, self.reliability, target_bot, task, acceptance_criteria, verification) if mode != 'off' else None
        completion = FinishStepTool(runner.workspace, required_files, verifier, mode, delivery) if required_files or verification else None
        if completion is not None:
            # A delegated artifact is not complete merely because the worker
            # wrote a file or emitted some prose.  Require the structured
            # completion record so AgentLoop can recover the common case where
            # a worker finishes its tool work and then returns an empty final
            # message.  Recovery keeps the same conversation/history and
            # exposes only finish_step, so it cannot replay writes, commands,
            # uploads, or other side effects.
            completion.require_record = True
            system_prompt += "\nReserve the final 60 seconds for validation and delivery. Call finish_step to record completion with every required file and evidence before ending."
            user_prompt += "\nRequired deliverables: " + json.dumps(required_files)
        if acceptance_criteria:
            user_prompt += "\nAcceptance criteria: " + acceptance_criteria

        logger.info("Delegating task to bot {} ({})", target_bot.name, target_bot.id)
        # Per delegation, not per bot: `delegate_many` can hand the same
        # specialist two assignments at once, and a bot-derived id would leave
        # the two runs' events indistinguishable from each other.
        turn_id = f"del_{delegation_id}"
        mirrored = (
            await self.mirror.open(target_bot, task, turn_id) if self.mirror is not None else None
        )
        sink = specialist_progress(
            progress,
            target_bot.name,
            next_substep,
            delegation={"delegation_id": delegation_id, "bot_id": target_bot.id},
        )
        specialist_sink: Any = sink
        if mirrored is not None:
            # One stream of loop events, two audiences: the leader's assignment
            # card, and the specialist's own thread where the same events render
            # as an ordinary turn.
            def emit_to_both(event: Any) -> None:
                sink(event)
                self.mirror.relay(mirrored, event)

            specialist_sink = emit_to_both
        started = time.monotonic()
        attempts = 1
        try:
            outcome = await runner.run(
                target_bot,
                system_prompt,
                user_prompt,
                specialist_sink,
                turn_id=turn_id,
                **({"extra_tools": [completion]} if completion is not None else {}),
            )
        except BaseException as exc:
            # The nested loop can raise (a re-raised TimeoutError, a provider
            # stream that ends without a result) and the registry turns that
            # into an error string rather than letting it surface — so without
            # closing the pair here the assignment card spins forever and the
            # store discards the row for lack of content.
            #
            # `BaseException`, so a cancellation is closed out too: a Telegram
            # worker restart cancels the task awaiting this turn, and an
            # `except Exception` left the card spinning and the specialist's
            # thread claimed for the rest of the process. `CancelledError`
            # carries no message, hence the class name as a fallback.
            detail = str(exc) or type(exc).__name__
            emit_event(
                {
                    "kind": "delegation_finished",
                    "delegation_id": delegation_id,
                    "bot_id": target_bot.id,
                    "text": f"Error: {detail}",
                    "artifacts": [],
                    "is_error": True,
                    "result": TaskResult("failed", summary=f"Error: {detail}",
                                         failure_reason=type(exc).__name__).as_dict(),
                }
            )
            if mirrored is not None:
                await self.mirror.close(mirrored, turn_id, f"Error: {detail}", is_error=True)
            raise
        finally:
            # Whatever the specialist had written but not yet forwarded. Without
            # it a reply shorter than one flush that reached for no tool along
            # the way previewed nothing at all, and its card showed a spinner
            # and then an answer with no working-out in between.
            sink.flush()
        # A cut-off specialist often returns no text at all, and calling that
        # "completed the task" told both the user and the delegating model the
        # run had succeeded — after which the model redid the whole task itself,
        # doubling a turn that had already spent its budget. Both cut-off modes
        # count: running out of steps produces an empty answer exactly like
        # running out of time, and only the wording differs.
        #
        # `.strip()` on every path, not just the cut-off one: a reply of "\n\n"
        # is truthy, so it used to pass through as the specialist's stored
        # message and render as a blank bubble under its name.
        # One output-recovery attempt only. No new full task/clock allowance,
        # no retries after cutoff or after a terminal completion record.
        if (completion is None and self.reliability.retry_empty_output
                and self.reliability.enabled_for(self.owner_id)
                and not outcome.text.strip() and not outcome.artifacts and not outcome.cut_off):
            remaining = 300 - (time.monotonic() - started)
            deadline = current_turn_deadline.get()
            if deadline is not None:
                remaining = min(remaining, deadline - time.monotonic())
            if remaining >= 5:
                attempts = 2
                previous_cost = dict(outcome.cost)
                try:
                    recovered = await runner.run(
                        target_bot, system_prompt,
                        user_prompt + '\nPrevious attempt ended without an answer. Inspect existing work and '
                        'return the missing result only; do not repeat completed actions. If no work exists, '
                        'report failure rather than restarting the whole assignment.',
                        specialist_sink, turn_id=turn_id + '_recovery',
                        max_seconds=min(remaining, 30), max_iterations=2,
                        **({'extra_tools': [completion]} if completion else {}),
                    )
                except Exception as exc:
                    # Keep the first attempt's usage and still close the card.
                    recovered = outcome
                    previous_cost = {}
                    logger.warning('Delegation output recovery failed: {}', type(exc).__name__)
                except BaseException:
                    emit_event({'kind': 'delegation_finished', 'delegation_id': delegation_id,
                                'bot_id': target_bot.id, 'text': 'Output recovery cancelled.',
                                'is_error': True, 'artifacts': [],
                                'result': TaskResult('failed', failure_reason='cancelled',
                                                     cost=previous_cost).as_dict()})
                    if mirrored is not None:
                        await self.mirror.close(mirrored, turn_id, 'Output recovery cancelled.', is_error=True)
                    raise
                for key, value in previous_cost.items():
                    recovered.cost[key] = recovered.cost.get(key, 0) + value
                outcome = recovered
        if runner.workspace != root_workspace:
            from sbot.core.assignment_workspace import rebase_result
            outcome.artifacts = rebase_result({'artifacts': outcome.artifacts}, runner.workspace, root_workspace)['artifacts']
            if completion and completion.result:
                completion.result = rebase_result(completion.result, runner.workspace, root_workspace)
        result = TaskResult.from_outcome(outcome, completion.result if completion else None,
                                         require_completion=completion is not None)
        result.attempts = attempts
        result.duration_seconds = round(time.monotonic() - started, 3)
        logger.info('Task reliability {}', json.dumps({
            'kind': 'delegate', 'task_id': delegation_id, 'status': result.status,
            'failure_reason': result.failure_reason, 'verification_status': result.verification_status,
            'attempts': result.attempts, 'duration_seconds': result.duration_seconds,
            'tokens': result.cost.get('tokens', 0),
        }))
        locale = current_turn_locale.get()
        text = result.summary
        if not text:
            if outcome.timed_out:
                key = "delegate.no_output_timeout"
            elif outcome.reached_max_iterations:
                key = "delegate.no_output_steps"
            else:
                key = "delegate.no_output"
            # User-visible and persisted as a transcript row, so it goes through
            # the message catalog like every other message the user reads.
            text = t(key, locale, bot=target_bot.name)
        # Full text, no preview: this is the specialist's message in the
        # transcript. The prefixed form below is for the delegating model, which
        # needs to be told whose answer it is reading.
        emit_event(
            {
                "kind": "delegation_finished",
                "delegation_id": delegation_id,
                "bot_id": target_bot.id,
                "text": text,
                "artifacts": result.artifacts,
                "is_error": result.status != "completed",
                "result": result.as_dict(),
            }
        )
        if mirrored is not None:
            # The specialist's own thread gets its answer, not the framing the
            # delegating model is handed below — read there, it is a reply to
            # the instruction above it, from the bot whose thread it is.
            await self.mirror.close(
                mirrored, turn_id, text, is_error=result.status != "completed", artifacts=result.artifacts,
                result=result.as_dict(),
            )
        if outcome.cut_off and result.status != "completed":
            # The instruction leads, ahead of the specialist's text. Tool
            # results are head-truncated (12k on the wire, 800 once stale, 4k
            # in the store) and the truncation footer tells the model to re-run
            # the tool — so a note appended after a long partial report would be
            # cut off and replaced by advice to do the very thing it forbids.
            limit = "หมดเวลา" if outcome.timed_out else "ใช้ขั้นตอนครบจำนวน"
            return (
                f"[{target_bot.name} ({target_bot.role_title}) ยังทำไม่เสร็จ — {limit}]\n"
                "อย่าลงมือทำงานเดิมซ้ำเอง งบของ turn นี้ถูกใช้ไปแล้ว — "
                "ให้รายงานผลเท่าที่ได้ต่อผู้ใช้ หรือมอบหมายใหม่ด้วยขอบเขตที่แคบลง\n\n"
                f"{text}"
            )
        if result.status != 'completed':
            return ('[Task incomplete: ' + (result.failure_reason or result.status) + ']\n'
                    'Report the missing result; do not claim success or redo the entire assignment.\n' + text)
        manifest = ''
        if result.artifacts:
            manifest = ('[Runtime attachment manifest: these files were attached to the specialist reply. '
                        'They are system-recorded, not a claim from the specialist. Do not republish them or '
                        'claim the specialist lacked the publication tool.]\n'
                        + '\n'.join(result.artifacts) + '\n\n')
        return f"{manifest}[{target_bot.name} ({target_bot.role_title}) ตอบกลับ]:\n\n{text}"


class DelegateManyTool(Tool):
    """Hand several specialists their work at once, and wait for all of them.

    The agent loop runs a turn's tool calls one after another, so a leader that
    emitted two `delegate` calls to answer "ask both of them at the same time"
    still got them one after the other — the second specialist did not start
    until the first had finished, and the turn's clock paid for both in full.
    The concurrency lives in here rather than in the loop because a delegation
    is the one long tool whose UI is already safe to run in parallel: its
    started/finished events are paired by `delegation_id`, so several open
    assignment cards fill themselves in independently. Tool events are matched
    by "the most recent call still running", which does not survive overlap.
    """

    name = "delegate_many"
    description = (
        "Delegate several independent tasks to different specialists at the same time and wait "
        "for all of them. Use this instead of calling `delegate` repeatedly whenever the tasks "
        "do not depend on each other — separate `delegate` calls run one after another. The "
        "specialists share one workspace, so do not give two of them work that writes the same "
        "file. For work that must happen in order, or that needs to survive past this turn, plan "
        "a mission instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "description": "The tasks to run at the same time, one per specialist.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "bot_name": {
                            "type": "string",
                            "description": "The name of the target bot, e.g. 'นักวิจัย'",
                        },
                        "bot_id": {"type": "string", "description": "Optional: exact bot id if known"},
                        "task": {
                            "type": "string",
                            "description": "Clear and self-contained instructions for the specialist",
                        },
                        "required_files": {"type": "array", "items": {"type": "string"}},
                        "acceptance_criteria": {"type": "string"},
                        "context": {
                            "type": "string",
                            "description": "Optional context or background information",
                        },
                    },
                    "required": ["task"],
                },
            }
        },
        "required": ["assignments"],
    }
    wants_progress = True

    def __init__(self, delegate: DelegateTool):
        self.delegate = delegate

    async def execute(self, assignments: Any = None, progress: Any = None, **_: Any) -> str:
        if not isinstance(assignments, list) or any(
            not isinstance(item, dict) or not str(item.get("task") or "").strip()
            for item in assignments
        ):
            return "Error: every assignment must be an object with a nonempty task."
        wanted = [
            item
            for item in (assignments or [])
            if isinstance(item, dict) and str(item.get("task") or "").strip()
        ]
        if not wanted:
            return (
                "Error: `assignments` must be a list of objects, each with a `task` and the "
                "specialist to run it (`bot_name`, or `bot_id`)."
            )
        if len(wanted) > MAX_PARALLEL_DELEGATIONS:
            # Each assignment is a whole nested agent loop against the same
            # provider and workspace, so an unbounded fan-out is a runaway
            # rather than a plan — same reason `create_bot` is bounded.
            return (
                f"Error: {len(wanted)} assignments at once is too many. Send at most "
                f"{MAX_PARALLEL_DELEGATIONS} and delegate the rest afterwards."
            )

        assignment_ids = [str(item.get('id') or f'assignment-{i + 1}') for i, item in enumerate(wanted)]
        if len(set(assignment_ids)) != len(assignment_ids):
            return 'Error: assignment IDs must be unique.'
        # One numbering across all of them: they report their steps into the
        # same tool row, which keys a substep by its index alone.
        substeps = itertools.count(1)
        completed = {}

        def assignment_progress(index):
            def receive(event):
                if event.get('kind') == 'delegation_finished':
                    completed[index] = event.get('result') or {
                        'status': 'failed' if event.get('is_error') else 'partial',
                        'failure_reason': 'missing_result_contract',
                    }
                if callable(progress):
                    progress(event)
            return receive

        results = await asyncio.gather(
            *(
                self.delegate.execute(
                    task=str(item.get("task") or ""),
                    bot_name=item.get("bot_name"),
                    bot_id=item.get("bot_id"),
                    context=str(item.get("context") or ""),
                    progress=assignment_progress(index),
                    verification=item.get("verification"),
                    input_files=item.get("input_files"),
                    required_files=item.get("required_files"),
                    delivery_target=item.get("delivery_target"),
                    acceptance_criteria=str(item.get("acceptance_criteria") or ""),
                    next_substep=lambda: next(substeps),
                )
                for index, item in enumerate(wanted)
            ),
            # One specialist raising must not take the others' finished work
            # down with it: they already ran, and the leader needs what it got
            # to report anything at all.
            return_exceptions=True,
        )
        counts = {assignment_ids[i]: completed.get(i, {"status": "failed", "failure_reason": "missing_result"})
                  for i in range(len(wanted))}
        successful = sum(value["status"] == "completed" for value in counts.values())
        sections = [json.dumps({"expected": len(wanted), "completed": successful,
                              "incomplete_ids": [key for key, value in counts.items() if value["status"] != "completed"]})]
        for assignment_id, item, result in zip(assignment_ids, wanted, results):
            if isinstance(result, BaseException):
                who = item.get("bot_name") or item.get("bot_id") or "?"
                sections.append(f"[{who} ล้มเหลว]: {result}")
            else:
                sections.append(f"[Assignment {assignment_id}]\n{result}")
        return "\n\n---\n\n".join(sections)

DelegateManyTool.parameters["properties"]["assignments"]["items"]["properties"].update({
    key: DelegateTool.parameters["properties"][key]
    for key in ("required_files", "input_files", "acceptance_criteria", "delivery_target", "verification")
})
