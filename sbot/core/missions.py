"""MissionService: turns a persisted mission graph into running specialist bots.

The split is deliberate. `MissionEngine` decides *what* may run and never calls
an LLM, so it stays replayable in tests; this module is the executor it is given
— it resolves a node's bot, builds its prompt, and charges its tokens back to
the mission budget.
"""

import asyncio
import time
import hashlib
import json
import re
from typing import Any

from loguru import logger

from sbot.core.mission_engine import (
    TERMINAL_STATUSES,
    InvalidGraphError,
    MissionEngine,
    NodeContext,
    NodeResult,
    ReplanOutcome,
)
from sbot.core.specialist import SpecialistRunner
from sbot.core.keyed_locks import KeyedLocks
from sbot.core.keyed_slots import KeyedSlots
from sbot.core.turn_context import current_turn_deadline
from sbot.db.models import Mission, MissionNode
from sbot.db.stores import BotStore, MissionStore
from sbot.tools.blackboard import BlackboardTool

# A mission with no ceiling is an open-ended spend authorization, and missions
# are started by an LLM. A caller's budget is merged *over* these rather than
# replacing them: a partial budget used to drop every ceiling it did not mention,
# so asking for a tighter token cap silently removed the wall-clock and attempt
# limits and a looping graph could then run until the process died.
DEFAULT_BUDGET = {
    "max_tokens": 400_000,
    "max_wall_seconds": 3 * 3600,
    "max_node_attempts": 40,
}
# The keys MissionEngine._budget_exceeded actually enforces. `max_cost` is
# deliberately absent: nothing populates `spent["cost"]` — SpecialistRunner
# reports tokens only — so accepting it would hand back a dollar ceiling that is
# never checked. Rejected here instead, so the caller finds out at plan time.
_BUDGET_LIMITS = frozenset(DEFAULT_BUDGET)
MAX_NODES_PER_PLAN = 60
# One patch is a repair, not a new plan; a replan that wants to touch more of the
# graph than this is replanning wholesale and throwing finished work away.
MAX_REPLAN_NODES = 20
_NODE_SECONDS = 600.0
# A replan only reads the graph it was handed and calls one tool, so it gets far
# less room than a node — and it is charged to the same mission budget.
_REPLAN_SECONDS = 300.0
_REPLAN_ITERATIONS = 6
_FAILURE_PREVIEW_CHARS = 600
# Outcomes worth interrupting the user for. `cancelled` is absent because the
# user is the only thing that cancels a mission, and `not_found` has nobody to
# report to.
_REPORTED_STATUSES = frozenset({"completed", "failed", "blocked", "paused"})


class MissionService:
    def __init__(
        self,
        missions: MissionStore,
        bots: BotStore,
        provider: Any,
        sandbox: Any,
        settings: Any,
        llm_config: Any = None,
        skills: Any = None,
        memory: Any = None,
        max_parallel_nodes: int = 4,
        notifier: Any = None,
        connectors: Any = None,
        project_access: Any = None,
        arg_guard_for_owner: Any = None,
        require_verified_results: bool = True,
        local_broker: Any = None,
        messages: Any = None,
        bus: Any = None,
        sessions: Any = None,
    ):
        # handle(user_id, session_id, prompt) -> final content, same shape the
        # scheduler and heartbeat fire turns through. A mission deliberately
        # outlives the turn that planned it, so without this its result reaches
        # nobody: `session_id` was recorded on the row and then never read, and
        # the user had to think to ask.
        self.local_broker = local_broker
        self.notifier = notifier
        self.messages = messages
        self.bus = bus
        self.sessions = sessions
        self._node_slots = asyncio.Semaphore(max(1, max_parallel_nodes))
        self._mission_slots = asyncio.Semaphore(max(1, max_parallel_nodes))
        self.connectors = connectors
        self.missions = missions
        self.bots = bots
        self.provider = provider
        self.sandbox = sandbox
        self.settings = settings
        self.llm_config = llm_config
        # A mission node is the only place a specialist works unsupervised, so
        # it is the place that most needs its skills and its own memory — see
        # `SpecialistRunner.context_block`.
        self.skills = skills
        self.memory = memory
        self.project_access = project_access
        self.arg_guard_for_owner = arg_guard_for_owner
        self.require_verified_results = require_verified_results
        self.max_parallel_nodes = max_parallel_nodes
        # mission_id -> the task driving it. Held for the same reason the engine
        # holds its in-flight node tasks: asyncio keeps only a weak reference, so
        # a dropped handle lets a whole mission be garbage-collected mid-run.
        self._running: dict[str, asyncio.Task[str]] = {}
        self._admission_lock = asyncio.Lock()
        self._start_locks = KeyedLocks()
        self._bot_slots = KeyedSlots(1)
        self._owner_slots = KeyedSlots(settings.team_work.max_parallel_per_owner)

    async def submit_work(self, owner_id, session_id, mission_id, goal, nodes, coordinator_id, member_ids=None):
        """Persist a bounded job before acknowledging it. One application worker.

        No automatic replan or action replay: each node has one execution
        attempt. Recovery after an uncertain worker death needs inspection.
        """
        limits = self.settings.team_work
        if not limits.enabled:
            raise InvalidGraphError("background team work is disabled")
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= limits.max_steps:
            raise InvalidGraphError(f"provide 1–{limits.max_steps} steps")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
            raise InvalidGraphError('job goal must contain 1–2000 characters')
        safe = []
        for node in nodes:
            if not isinstance(node, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", str(node.get('id', ''))):
                raise InvalidGraphError("step ids must be 1–48 letters, digits, underscores or hyphens")
            if node['id'] == '__summary':
                raise InvalidGraphError("__summary is reserved for the coordinator")
            if not str(node.get('instruction') or '').strip():
                raise InvalidGraphError("every step needs self-contained instructions")
            if not isinstance(node['instruction'], str) or len(node['instruction']) > 50_000:
                raise InvalidGraphError('step instructions must be text of at most 50000 characters')
            for key in ('required_files', 'input_files', 'depends_on'):
                values = node.get(key, [])
                if not isinstance(values, list) or len(values) > 50 or any(
                    not isinstance(value, str) or not value.strip() for value in values
                ):
                    raise InvalidGraphError(f'{key} must contain at most 50 nonempty strings')
            safe.append({key: node[key] for key in (
                'id', 'bot_id', 'title', 'instruction', 'depends_on', 'kind',
                'required_files', 'input_files', 'acceptance_criteria', 'verification', 'delivery_target'
            ) if key in node})
            safe[-1]['max_attempts'] = 1
            if safe[-1].get('kind', 'task') not in ('task', 'gate'):
                raise InvalidGraphError("step kind must be task or gate")
        if len(safe) > 1:
            if not coordinator_id or await self.bots.get(coordinator_id, owner_id) is None:
                raise InvalidGraphError("a coordinator is required to combine results")
            safe.append({
                'id': '__summary', 'title': 'รวมผล / Final report', 'bot_id': coordinator_id,
                'depends_on': [n['id'] for n in safe], 'required_files': [], 'max_attempts': 1,
                'instruction': (
                    'Combine the completed steps into the final answer for this job in the language of its goal. '
                    'Read full result:<step> and contract:<step> blackboard entries as needed. '
                    'Check coverage of the original request; distinguish verified facts, assumptions and limitations. '
                    'Do not redo specialist work, invent missing results or perform new external actions. '
                    'Reference existing deliverables; do not recreate or republish them. '
                    'Put the complete user-facing report in finish_step.summary, with concrete evidence. '
                    'If essential work is missing, record blocked or failed, never completed.'
                ),
            })
        MissionEngine.validate_dag(safe)
        async with self._admission_lock:
            existing = await self.missions.get_mission(mission_id, owner_id)
            if existing is not None:
                if existing.status == 'planned':
                    raise InvalidGraphError(f'job {mission_id} was not fully submitted; inspect it before retrying')
                return existing
            pending = await self.missions.pending_counts(owner_id)
            if pending['owner'] >= limits.max_pending_per_owner or pending['total'] >= limits.max_pending_total:
                raise InvalidGraphError("background queue is full; finish or cancel a pending job before submitting more")
            mission = await self.plan(owner_id, goal, safe, session_id=session_id,
                                      member_ids=member_ids, mission_id=mission_id)
            await self.missions.blackboard_write(mission.id, 'scope:background', True)
            await self.missions.update_mission(mission.id, status='queued')
            self._spawn(mission.id)
            mission.status = 'queued'
            return mission

    # ------------------------------------------------------------------ planning
    async def _current_members(self, owner_id: str, session_id: str | None) -> frozenset[str] | None:
        if self.sessions is None or not session_id:
            return None
        session = await self.sessions.get(session_id)
        if session is None or session.user_id != owner_id:
            raise InvalidGraphError('Mission conversation is unavailable')
        if session.kind != 'group':
            return None
        from sbot.db.bot_groups import BotGroupStore
        group = await BotGroupStore(self.sessions.factory).get(session.group_id, owner_id)
        return frozenset(group.member_ids) if group else frozenset()

    @staticmethod
    def _resolve_budget(budget: dict | None) -> dict:
        """Overlay a caller's ceilings on the defaults, rejecting what is not
        enforceable.

        `budget` reaches here as a free-form dict off the HTTP body, and every
        value is compared against accumulated spend inside the scheduler's loop.
        A string there raises mid-run, which surfaces as a crashed scheduler and
        a mission marked `failed` for no visible reason; a key the engine does
        not check is a ceiling the caller believes in and nobody applies.
        """
        if not budget:
            return dict(DEFAULT_BUDGET)
        unknown = sorted(set(budget) - _BUDGET_LIMITS)
        if unknown:
            raise InvalidGraphError(
                f"unsupported budget limit(s): {unknown}; "
                f"choose from {sorted(_BUDGET_LIMITS)}"
            )
        resolved = dict(DEFAULT_BUDGET)
        for key, value in budget.items():
            # bool is an int, and `max_tokens: True` would compare as a ceiling
            # of one token — every mission instantly over budget.
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise InvalidGraphError(f"budget limit {key!r} must be a positive number")
            resolved[key] = value
        return resolved

    async def plan(
        self,
        owner_id: str,
        goal: str,
        nodes: list[dict],
        session_id: str | None = None,
        budget: dict | None = None,
        member_ids: frozenset[str] | None = None,
        mission_id: str | None = None,
    ) -> Mission:
        """Persist a mission and its graph, or raise InvalidGraphError.

        Every node's `bot_id` is checked against the caller's own bots here
        rather than at execution time: the plan comes from a model, and a node
        naming a bot that does not exist would otherwise burn all its attempts
        before the mission failed for a reason nobody can see.
        """
        goal = (goal or "").strip()
        if not goal:
            raise InvalidGraphError("a mission needs a goal")
        if not nodes:
            raise InvalidGraphError("a mission needs at least one node")
        if len(nodes) > MAX_NODES_PER_PLAN:
            raise InvalidGraphError(f"a single plan may define at most {MAX_NODES_PER_PLAN} nodes")

        budget = self._resolve_budget(budget)
        current_members = await self._current_members(owner_id, session_id)
        if current_members is not None:
            member_ids = current_members if member_ids is None else member_ids & current_members

        nodes = [{**node, 'budget': {**(node.get('budget') or {}), **{
            key: node[key] for key in ('input_files', 'delivery_target', 'acceptance_criteria', 'verification') if key in node
        }}} for node in nodes]
        for node in nodes:
            bot_id = node.get("bot_id")
            if node.get("kind", "task") == "gate":
                continue
            if not bot_id:
                raise InvalidGraphError(f"node {node.get('id') or node.get('title')!r} has no bot_id")
            if member_ids is not None and bot_id not in member_ids:
                raise InvalidGraphError(f"bot {bot_id!r} is outside this group")
            if await self.bots.get(bot_id, owner_id) is None:
                raise InvalidGraphError(f"node {node.get('id') or node.get('title')!r} names unknown bot {bot_id!r}")

        mission = await self.missions.create_mission(
            owner_id=owner_id,
            goal=goal,
            session_id=session_id,
            budget=budget,
            status="planned",
            mission_id=mission_id,
        )
        try:
            if member_ids is not None:
                await self.missions.blackboard_write(mission.id, 'scope:members', sorted(member_ids))
            await self.missions.add_nodes(mission.id, nodes)
        except InvalidGraphError:
            # Leave no half-built mission behind for the scheduler to find.
            await self.missions.update_mission(mission.id, status="failed")
            raise
        return mission

    # ------------------------------------------------------------------ running
    async def start(self, mission_id: str, owner_id: str) -> str:
        """Begin (or resume) a mission in the background. Returns its status."""
        async with self._start_locks.get(mission_id):
            mission = await self.missions.get_mission(mission_id, owner_id)
            if mission is None:
                return "not_found"
            task = self._running.get(mission_id)
            if task is not None and not task.done():
                return mission.status
            if mission.status == 'completed' or (mission.status == 'cancelled' and
                    await self.missions.blackboard_read(mission.id, 'scope:background')):
                return mission.status
            await self.missions.update_mission(mission_id, status="running")
            self._spawn(mission_id)
            return "running"

    def _spawn(self, mission_id: str) -> asyncio.Task[str]:
        async def drive():
            # Queue before starting the engine's wall-clock budget or claiming leases.
            async with self._mission_slots:
                mission = await self.missions.get_mission_unchecked(mission_id)
                if mission is None:
                    return 'not_found'
                if mission.status not in ('queued', 'running'):
                    return mission.status
                if mission.status == 'queued':
                    if not await self.missions.begin_queued(mission_id):
                        return 'cancelled'
                return await self._run(mission_id)

        task = asyncio.create_task(drive(), name=f"mission:{mission_id}")
        self._running[mission_id] = task

        def _release(finished: asyncio.Task[str], mid: str = mission_id) -> None:
            # Only ever clear our own handle. A mission that settles and is
            # immediately started again would otherwise have the new task's
            # handle dropped by the old task's callback, and asyncio holds only
            # a weak reference — so the fresh run could be collected mid-flight.
            if self._running.get(mid) is finished:
                del self._running[mid]

        task.add_done_callback(_release)
        return task

    async def resume_interrupted(self, limit: int = 20) -> int:
        """Pick missions back up after a process restart. Returns how many.

        A mission deliberately outlives the turn that started it, but the handle
        driving it lives in `self._running` — in memory. A restart therefore
        leaves the row saying `running` with nobody scheduling it: the mission is
        stalled forever, and nothing in the product ever notices, because every
        other path only reads status. This sweep is the "survives a process
        restart" half of `MissionEngine.run_mission`'s contract.

        Also includes missions the previous process left `blocked`: that status
        covers both "parked on a gate" and "a claimed node's worker died before
        the lease expired," and the two are indistinguishable without running
        the loop again. `run_mission` bails immediately unless the row says
        `running`, so this resets it first — same as `start()` does — before
        spawning. A real gate re-blocks within one pass at no real cost; an
        orphaned node gets its expired lease reclaimed and, since retries are
        already exhausted, the mission properly fails and gets reported instead
        of sitting `blocked` forever with nothing left to revisit it.

        Safe to run with another worker already on the same mission: a node is
        only executed by whoever wins `claim_node`, and expired leases are
        reclaimed at the start of each run.
        """
        limit = min(200, max(1, limit))
        missions = []
        offset = 0
        while True:
            page = await self.missions.interrupted_missions(limit, offset=offset)
            missions.extend(page)
            if len(page) < min(limit, 200):
                break
            offset += len(page)
        resumed = 0
        for mission in missions:
            task = self._running.get(mission.id)
            if task is not None and not task.done():
                continue
            if mission.status != 'queued':
                await self.missions.update_mission(mission.id, status="running")
            self._spawn(mission.id)
            resumed += 1
        if resumed:
            logger.info("Resumed {} interrupted mission(s) after restart", resumed)
        return resumed

    async def run_to_completion(self, mission_id: str, owner_id: str) -> str:
        """Start and await a mission. For tests and for callers that genuinely
        want to block; the chat path must use `start`."""
        status = await self.start(mission_id, owner_id)
        task = self._running.get(mission_id)
        return await task if task is not None else status

    async def _run(self, mission_id: str) -> str:
        # A mission deliberately outlives the turn that started it, but
        # asyncio.create_task copies the current context — so without this every
        # node would inherit the starting turn's deadline and, once it passed, be
        # clamped to the 10s floor forever.
        current_turn_deadline.set(None)
        mission = await self.missions.get_mission_unchecked(mission_id)
        if mission is None:
            return "not_found"
        engine = MissionEngine(
            self.missions,
            node_executor=self._executor_for(mission),
            max_parallel_nodes=self.max_parallel_nodes,
            worker_id=f"mission-service:{mission_id[:8]}",
            replan_hook=(None if await self.missions.blackboard_read(mission.id, 'scope:background')
                         else self._replan_hook_for(mission)),
        )
        try:
            status = await engine.run_mission(mission_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background task boundary; nothing above catches
            # The scheduler crashing must not leave the mission looking alive:
            # nothing else will ever move it, so say so.
            logger.exception("Mission {} scheduler crashed", mission_id)
            await self.missions.update_mission(mission_id, status="failed")
            status = "failed"
        await self._report(mission, status)
        return status

    async def _report(self, mission: Mission, status: str) -> None:
        """Persist a deterministic result and files without another model call.

        The outcome snapshot hashes to the message primary key, so retries and
        competing reporters cannot duplicate it. Maintenance retries committed
        outcomes after outages. The notifier remains a compatibility adapter
        for callers without a message store.
        """
        if not mission.session_id or status not in _REPORTED_STATUSES:
            return
        try:
            if self.messages is not None:
                nodes = await self.missions.get_nodes(mission.id)
                snapshot = [(n.id, n.status, n.output, n.artifacts, n.attempts) for n in nodes]
                key = hashlib.sha256(json.dumps([mission.id, status, snapshot], sort_keys=True).encode()).hexdigest()[:32]
                artifacts = list(dict.fromkeys(p for n in nodes for p in (n.artifacts or [])))
                final_node = next((n for n in nodes if n.id == '__summary' and n.status == 'done'), None)
                content = f"Mission: {mission.goal}\nJob ID: {mission.id}\nStatus: {status}\n\n" + '\n\n'.join(
                    f"### {n.title} · {n.status}\n{(n.output or '')[:8000]}" for n in nodes
                )
                if final_node and status == 'completed':
                    content = f"{mission.goal}\nJob ID: {mission.id}\n\n{final_node.output or ''}"
                seq = await self.messages.append(mission.session_id, [{
                    'role': 'assistant', 'content': content,
                    'meta': {'artifacts': artifacts, 'mission_id': mission.id, 'delivery_id': key},
                }], delivery_key=key)
                if seq and self.bus:
                    from sbot.core.events import MissionReported
                    self.bus.publish(mission.session_id, MissionReported(key, content, artifacts))
            elif self.notifier:
                await self.notifier(mission.owner_id, mission.session_id, self._report_prompt(mission, status))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed report must not fail the mission
            logger.exception("Mission {} settled as {} but could not be reported", mission.id, status)

    async def reconcile_reports(self) -> None:
        """Retry committed outcomes, including a crash between completion and delivery."""
        offset = 0
        while self.messages is not None:
            page = await self.missions.reportable_missions(offset)
            for mission in page:
                await self._report(mission, mission.status)
            if len(page) < 100:
                return
            offset += len(page)

    async def maintenance(self) -> None:
        while True:
            try:
                await self.resume_interrupted()
                await self.reconcile_reports()
            except Exception:  # noqa: BLE001 - background recovery boundary; retry next pass
                logger.exception("Mission recovery/delivery reconciliation failed; will retry")
            await asyncio.sleep(60)

    async def stop(self) -> None:
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _report_prompt(mission: Mission, status: str) -> str:
        """What Chief of Staff is asked to do once a mission settles.

        Deliberately does not inline the results. The status tool truncates each
        step's output, and a mission's full transcript can be far larger than the
        turn that has to summarize it — so this points at the tool and lets the
        model pull only what it needs.
        """
        lines = [
            f"Mission {mission.id} has settled as {status}.",
            f"Its goal was: {mission.goal}",
            "",
            (
                "Report it to the user now. Call `mission_status` with that id first — you have "
                "not seen what the specialists produced. Then write the report yourself: what the "
                "mission delivered, which specialist produced each part, and anything that still "
                "needs the user's decision."
            ),
        ]
        if status == "blocked":
            lines.append(
                "Steps of this mission are parked waiting for the user to review them. Say "
                "which ones, and what approving each would let the mission do next. Call "
                "`mission_gate` only after the user has told you their decision."
            )
        elif status == "paused":
            lines.append(
                "It ran out of budget rather than finishing. Say what is left undone, and what "
                "it would take to finish."
            )
        lines.append(
            "This is a report: do not plan another mission, delegate, or start new work."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ replan
    async def apply_replan(
        self,
        mission_id: str,
        owner_id: str,
        skip: list[str] | None = None,
        retry: list[dict] | None = None,
        add: list[dict] | None = None,
    ) -> str:
        """Patch a stuck mission's graph in place. Raises InvalidGraphError.

        A patch, deliberately, and not a fresh plan: finished steps keep their
        results (PRD §4.3). `retry` is the primitive that matters — it keeps the
        node's id, so everything already depending on it still receives its
        output, which re-adding the step under a new id could never do.

        Every target is validated before anything is written, so a patch that is
        half-wrong does not leave a graph that is half-patched.
        """
        if await self.missions.get_mission(mission_id, owner_id) is None:
            raise InvalidGraphError(f"no mission {mission_id!r} belongs to this user")
        skip = [str(n) for n in (skip or [])]
        retry = list(retry or [])
        add = list(add or [])
        if not (skip or retry or add):
            raise InvalidGraphError("a replan must skip, retry, or add at least one step")
        if len(skip) + len(retry) + len(add) > MAX_REPLAN_NODES:
            raise InvalidGraphError(f"a single replan may touch at most {MAX_REPLAN_NODES} steps")

        by_id = {n.id: n for n in await self.missions.get_nodes(mission_id)}
        members = await self.missions.blackboard_read(mission_id, 'scope:members')
        mission = await self.missions.get_mission(mission_id, owner_id)
        current_members = await self._current_members(owner_id, mission.session_id)
        if current_members is not None:
            members = current_members if members is None else set(members) & current_members
        if members is not None:
            for item in [*retry, *add]:
                if item.get('bot_id') is not None and item['bot_id'] not in members:
                    raise InvalidGraphError('Cannot assign a mission step outside its group')

        for node_id in skip:
            node = by_id.get(node_id)
            if node is None:
                raise InvalidGraphError(f"step {node_id!r} is not part of this mission")
            if node.status in TERMINAL_STATUSES:
                raise InvalidGraphError(f"step {node_id!r} already settled as {node.status}")
            if node.status == "running":
                # Skipping satisfies dependents, so they would start while the
                # step they depend on is still executing.
                raise InvalidGraphError(f"step {node_id!r} is still running; it cannot be skipped")

        revisions: list[tuple[str, str | None, str | None]] = []
        for item in retry:
            if not isinstance(item, dict):
                raise InvalidGraphError("each retry must be an object with a step id")
            node_id = str(item.get("id") or "")
            node = by_id.get(node_id)
            if node is None:
                raise InvalidGraphError(f"step {node_id!r} is not part of this mission")
            if node.status != "error":
                raise InvalidGraphError(
                    f"step {node_id!r} is {node.status}, and only a failed step can be retried"
                )
            bot_id = item.get("bot_id")
            if bot_id is not None:
                bot_id = str(bot_id)
                if await self.bots.get(bot_id, owner_id) is None:
                    raise InvalidGraphError(f"step {node_id!r} names unknown bot {bot_id!r}")
            instruction = item.get("instruction")
            instruction = str(instruction) if instruction else None
            if instruction is None and bot_id is None:
                # Re-running the same instruction with the same bot is what
                # already failed every attempt it was given.
                raise InvalidGraphError(
                    f"retrying step {node_id!r} needs a new instruction or a different bot"
                )
            revisions.append((node_id, instruction, bot_id))

        for node in add:
            if node.get("kind", "task") == "gate":
                continue
            bot_id = node.get("bot_id")
            if not bot_id:
                raise InvalidGraphError(f"new step {node.get('id') or node.get('title')!r} has no bot_id")
            if await self.bots.get(bot_id, owner_id) is None:
                raise InvalidGraphError(
                    f"new step {node.get('id') or node.get('title')!r} names unknown bot {bot_id!r}"
                )

        # Added first: add_nodes re-validates the whole graph and is the only
        # part that can still reject the patch, so nothing else is written yet
        # when it does.
        if add:
            await self.missions.add_nodes(mission_id, add)
        for node_id, instruction, bot_id in revisions:
            revised = await self.missions.revise_node(
                mission_id, node_id, instruction=instruction, bot_id=bot_id
            )
            if revised is None:
                # It was claimed between validation and here.
                raise InvalidGraphError(f"step {node_id!r} changed while the replan was being applied")
        for node_id in skip:
            await self.missions.finish_node(
                mission_id,
                node_id,
                status="skipped",
                output="Skipped by a replan: this step was abandoned.",
            )

        logger.info(
            "Mission {} replanned: {} skipped, {} retried, {} added",
            mission_id, len(skip), len(revisions), len(add),
        )
        return (
            f"Replan applied: {len(skip)} step(s) skipped, {len(revisions)} retried, "
            f"{len(add)} added."
        )

    def _replan_hook_for(self, mission: Mission):
        async def replan(mission_id: str, exhausted: list[MissionNode]) -> ReplanOutcome:
            return await self._replan(mission, exhausted)

        return replan

    async def _replan(self, mission: Mission, exhausted: list[MissionNode]) -> ReplanOutcome:
        """Let Chief of Staff patch a stuck graph, and report what changed.

        The patch is applied through `apply_replan` — the same validated path the
        `mission_replan` tool uses — rather than by parsing a graph out of the
        reply, so a confused replan is rejected the way a bad plan is.
        """
        # Local import: sbot.tools.missions imports this module.
        from sbot.tools.missions import MissionReplanTool

        cos = None
        members = await self._current_members(mission.owner_id, mission.session_id)
        if members is not None:
            from sbot.db.bot_groups import BotGroupStore
            session = await self.sessions.get(mission.session_id)
            group = await BotGroupStore(self.sessions.factory).get(session.group_id, mission.owner_id)
            if group:
                cos = await self.bots.get(group.leader_id, mission.owner_id)
            if cos is None:
                return ReplanOutcome(changed=False)
        else:
            cos = await self.bots.get_or_create_cos(mission.owner_id)
        # No skills or memory here, unlike a node: a replan gets
        # `_REPLAN_ITERATIONS` to make one `mission_replan` call, and every extra
        # tool is another way to spend that allowance without patching the graph.
        runner = SpecialistRunner(
            provider=self.provider,
            sandbox=self.sandbox,
            workspace=self.settings.workspaces_root / mission.owner_id,
            model=self.settings.llm.model,
            llm_config=self.llm_config,
            owner_id=mission.owner_id,
            project_access=self.project_access,
            arg_guard=self.arg_guard_for_owner(mission.owner_id) if self.arg_guard_for_owner else None,
        )
        before = await self._graph_fingerprint(mission.id)
        outcome = await runner.run(
            cos,
            self._replan_system_prompt(mission, cos),
            await self._replan_user_prompt(mission, exhausted),
            lambda _event: None,
            turn_id=f"mr_{mission.id[:8]}",
            max_seconds=_REPLAN_SECONDS,
            max_iterations=_REPLAN_ITERATIONS,
            extra_tools=[MissionReplanTool(self, mission.owner_id, mission_id=mission.id)],
            # The prompt below reserves answering in text as the "nothing can
            # save this mission" signal, and the budget notice's "always finish
            # with your findings in your reply" pushes the model straight into
            # it — which reads as giving up on a mission that was patchable.
            budget_notice=False,
        )
        after = await self._graph_fingerprint(mission.id)
        if after == before:
            logger.warning("Mission {} replan changed nothing: {}", mission.id, outcome.text[:200])
        return ReplanOutcome(changed=after != before, cost=outcome.cost)

    async def _graph_fingerprint(self, mission_id: str) -> set[tuple]:
        """Everything about the graph a replan could usefully change. Compared
        before and after so `changed` describes the database, not the reply."""
        return {
            (n.id, n.status, n.attempts, n.max_attempts, n.bot_id, n.instruction)
            for n in await self.missions.get_nodes(mission_id)
        }

    @staticmethod
    def _replan_system_prompt(mission: Mission, cos: Any) -> str:
        return (
            f"You are {cos.name}, {cos.role_title}.\n\n"
            f"# Charter\n{cos.charter}\n\n"
            f"# Mission\n{mission.goal}\n\n"
            "Steps of this mission have now failed every attempt they were given, and it "
            "cannot proceed. Patch the graph with the `mission_replan` tool so it can.\n\n"
            "Patch it — do not plan it again. Steps that already finished keep their "
            "results, and re-adding work under a new id throws that away. For each failed "
            "step take the cheapest option that could actually work:\n"
            "- `retry` it with a corrected instruction, or a better-suited specialist, when "
            "the step is still the right step and the instruction or the assignment was the "
            "problem. This keeps the step's id, so everything depending on it still receives "
            "its result.\n"
            "- `skip` it when the mission does not really need it. Its dependents then run "
            "without its output, so only skip when they can cope without it.\n"
            "- `add` steps for work the original plan was missing.\n\n"
            "Call the tool once. If nothing you can do would make this mission succeed, "
            "answer without calling it and say why — the mission will be reported as failed."
        )

    async def _replan_user_prompt(self, mission: Mission, exhausted: list[MissionNode]) -> str:
        nodes = await self.missions.get_nodes(mission.id)
        graph = "\n".join(
            f"- {n.id} [{n.status}] {n.title}"
            + (f" (bot {n.bot_id})" if n.bot_id else "")
            + (f" depends on {', '.join(n.depends_on)}" if n.depends_on else "")
            for n in nodes
        )
        failures = "\n\n".join(
            f"### {n.id} — {n.title}\nattempts: {n.attempts}/{n.max_attempts}\n"
            f"instruction: {n.instruction}\nlast error: {(n.output or '')[:_FAILURE_PREVIEW_CHARS]}"
            for n in exhausted
        )
        # The roster is inlined because a replan runs outside a chat turn and is
        # not granted `list_bots` — without this, reassigning a failed step to a
        # better-suited specialist would mean guessing at bot ids.
        members = await self.missions.blackboard_read(mission.id, 'scope:members')
        current_members = await self._current_members(mission.owner_id, mission.session_id)
        if current_members is not None:
            members = current_members if members is None else set(members) & current_members
        roster = "\n".join(
            f"- {b.id}: {b.name} — {b.role_title}"
            for b in await self.bots.list_for_user(mission.owner_id)
            if members is None or b.id in members
        )
        return (
            f"# Current Graph\n{graph}\n\n"
            f"# Steps That Failed Every Attempt\n{failures}\n\n"
            f"# Specialists Available\n{roster}\n\n"
            "Decide the patch and call `mission_replan`."
        )

    # -------------------------------------------------------------------- gates
    async def resolve_gate(
        self, mission_id: str, owner_id: str, node_id: str, approved: bool, note: str = ""
    ) -> str:
        """Apply a human's decision to a parked gate. Returns the mission's status.

        A gate is the one node kind the engine will not settle on its own, so
        before this existed a mission that reached one stopped for good: it went
        `awaiting_human` → `blocked`, and the only way past was a replan skipping
        the very step someone asked to review.

        Declining leaves the gate parked rather than settling it. `skipped` is the
        only other terminal status available, and it *satisfies* dependents — so
        recording a refusal that way would mean "carry on without it", and a
        later restart would run exactly the work the user declined.
        """
        if await self.missions.get_mission(mission_id, owner_id) is None:
            raise InvalidGraphError(f"no mission {mission_id!r} belongs to this user")
        node = next((n for n in await self.missions.get_nodes(mission_id) if n.id == node_id), None)
        if node is None:
            raise InvalidGraphError(f"step {node_id!r} is not part of this mission")
        if node.status != "awaiting_human":
            raise InvalidGraphError(
                f"step {node_id!r} is {node.status}, and only a step waiting for a review "
                "can be approved or declined"
            )

        note = (note or "").strip()
        applied = await self.missions.resolve_gate(
            mission_id,
            node_id,
            status="done" if approved else "awaiting_human",
            output=note or ("Approved by the user." if approved else "Declined by the user."),
        )
        if not applied:
            raise InvalidGraphError(f"step {node_id!r} changed while the decision was being applied")

        logger.info(
            "Mission {} gate {} {} by the user", mission_id, node_id,
            "approved" if approved else "declined",
        )
        if not approved:
            await self.cancel(mission_id, owner_id)
            return "cancelled"
        # Clearing the gate is not enough on its own: reaching one settles the
        # mission as `blocked`, so nothing is scheduling it any more and the graph
        # would be correct and still stalled. `start` is a no-op when a run is
        # already in flight, which is the case when other nodes are still going.
        return await self.start(mission_id, owner_id)

    async def cancel(self, mission_id: str, owner_id: str) -> bool:
        """Ask a mission to stop. In-flight nodes are allowed to finish — the
        engine notices the status change on its next pass, which is what keeps a
        cancelled node from being left claimed with no result."""
        mission = await self.missions.get_mission(mission_id, owner_id)
        if mission is None:
            return False
        await self.missions.update_mission(mission_id, status="cancelled")
        return True

    # ------------------------------------------------------------------- status
    async def status(self, mission_id: str, owner_id: str) -> dict[str, Any] | None:
        mission = await self.missions.get_mission(mission_id, owner_id)
        if mission is None:
            return None
        nodes = await self.missions.get_nodes(mission_id)
        # Resolved once for the whole graph rather than per node. Without the
        # name a caller asking "how is the team doing" gets back opaque ids and
        # has to cross-reference the roster itself to answer — and Chief of Staff
        # spent a whole extra tool round on `list_bots` doing exactly that.
        names = {b.id: b.name for b in await self.bots.list_for_user(mission.owner_id)}
        contracts = {}
        activity = {}
        for node in nodes:
            record = await self.missions.blackboard_read(mission_id, f'scope:activity:{node.id}')
            activity[node.id] = (record or {}).get('phase', 'running') if node.status == 'running' else node.status
            record = await self.missions.blackboard_read(mission_id, f'contract:{node.id}')
            if isinstance(record, dict):
                contracts[node.id] = {key: record.get(key) for key in (
                    'status', 'verification_status', 'failure_reason', 'delivery', 'attempts', 'duration_seconds')}
                contracts[node.id]['evidence_ref'] = f'contract:{node.id}'

        return {
            "id": mission.id,
            "goal": mission.goal,
            "status": mission.status,
            "progress": {
                "done": sum(n.status == 'done' for n in nodes), "total": len(nodes),
                "steps": [{"id": n.id, "title": n.title, "bot_name": names.get(n.bot_id or '', ''),
                           "state": activity[n.id], "depends_on": n.depends_on or []} for n in nodes],
            },
            "budget": mission.budget or {},
            "spent": mission.spent or {},
            "created_at": mission.created_at.isoformat() if mission.created_at else None,
            "nodes": [
                {
                    "id": n.id,
                    "title": n.title,
                    "kind": n.kind,
                    "bot_id": n.bot_id,
                    # "" for a gate (nobody runs it) and for a bot that has since
                    # been archived — the id stays, so the step is still traceable.
                    "bot_name": names.get(n.bot_id or "", ""),
                    "status": n.status,
                    "execution_state": activity[n.id],
                    "depends_on": n.depends_on or [],
                    "attempts": n.attempts,
                    "max_attempts": n.max_attempts,
                    "output": n.output or "",
                    "artifacts": n.artifacts or [],
                    **({"task_result": contracts[n.id]} if n.id in contracts else {}),
                }
                for n in nodes
            ],
        }

    # ----------------------------------------------------------------- executor
    def _executor_for(self, mission: Mission):
        runner = SpecialistRunner(
            provider=self.provider,
            sandbox=self.sandbox,
            workspace=self.settings.workspaces_root / mission.owner_id,
            model=self.settings.llm.model,
            llm_config=self.llm_config,
            owner_id=mission.owner_id,
            skills=self.skills,
            memory=self.memory,
            connectors=self.connectors,
            project_access=self.project_access,
            arg_guard=self.arg_guard_for_owner(mission.owner_id) if self.arg_guard_for_owner else None,
        )

        async def execute(node: MissionNode, context: NodeContext) -> NodeResult:
            background = await self.missions.blackboard_read(mission.id, 'scope:background')
            if not background:
                async with self._node_slots:
                    return await self._execute_node(mission, runner, node, context)
            await self.missions.blackboard_write(mission.id, f'scope:activity:{node.id}', {'phase': 'queued'})
            # Waiting for a busy bot must not consume a global execution slot.
            async with self._bot_slots.hold(f'{mission.owner_id}:{node.bot_id}'):
                async with self._owner_slots.hold(mission.owner_id), self._node_slots:
                    current = await self.missions.get_mission_unchecked(mission.id)
                    if current is None or current.status != 'running':
                        return NodeResult(status='error', output='Job stopped before this step began.')
                    await self.missions.blackboard_write(mission.id, f'scope:activity:{node.id}', {'phase': 'running'})
                    return await self._execute_node(mission, runner, node, context)

        return execute

    async def _execute_node(
        self, mission: Mission, runner: SpecialistRunner, node: MissionNode, context: NodeContext
    ) -> NodeResult:
        members = await self.missions.blackboard_read(mission.id, 'scope:members')
        current_members = await self._current_members(mission.owner_id, mission.session_id)
        if current_members is not None:
            members = current_members if members is None else set(members) & current_members
        if members is not None and node.bot_id not in members:
            return NodeResult(status='error', output='Assigned bot is outside the mission group.')
        bot = await self.bots.get(node.bot_id, mission.owner_id) if node.bot_id else None
        if bot is None:
            return NodeResult(
                status="error",
                output=f"Assigned bot {node.bot_id!r} is no longer available to this owner.",
            )

        from sbot.tools.finish_step import FinishStepTool
        from sbot.config import ReliabilitySettings
        from sbot.core.deep_verification import TaskVerifier
        policy = getattr(self.settings, 'reliability', ReliabilitySettings())
        mode = policy.verification_mode if policy.enabled_for(mission.owner_id) else 'off'
        root_workspace = runner.workspace
        input_manifest = {}
        background = await self.missions.blackboard_read(mission.id, 'scope:background')
        if background:
            from sbot.core.team_workspace import scope_runner
            inputs = list((node.budget or {}).get('input_files') or [])
            for dep in node.depends_on or []:
                contract = await self.missions.blackboard_read(mission.id, f'contract:{dep}')
                if isinstance(contract, dict):
                    inputs.extend(contract.get('artifacts') or [])
            try:
                if policy.isolated_assignments and policy.enabled_for(mission.owner_id):
                    from sbot.core.assignment_workspace import isolate_runner
                    runner, input_manifest = isolate_runner(runner, inputs)
                else:
                    runner, input_manifest = scope_runner(runner, mission.id, node.id, inputs)
            except (ValueError, OSError) as exc:
                return NodeResult(status='error', output=f'Cannot prepare job inputs: {exc}')
        elif policy.isolated_assignments and policy.enabled_for(mission.owner_id):
            from sbot.core.assignment_workspace import isolate_runner
            inputs = list((node.budget or {}).get('input_files') or [])
            for dep in node.depends_on or []:
                contract = await self.missions.blackboard_read(mission.id, f'contract:{dep}')
                if isinstance(contract, dict):
                    inputs.extend(contract.get('artifacts') or [])
            try:
                runner, input_manifest = isolate_runner(runner, inputs)
            except (ValueError, OSError) as exc:
                return NodeResult(status='error', output=f'Cannot isolate assignment: {exc}')
        if background and node.id == '__summary':
            parent_guard = runner.arg_guard

            def summary_guard(name, args):
                readable = {'read_file', 'list_dir', 'read_docx', 'read_pdf', 'read_excel', 'read_csv'}
                allowed = name in readable or (name == 'blackboard' and args.get('action') in ('read', 'list'))
                allowed = allowed or (name == 'finish_step' and not args.get('files'))
                if not allowed:
                    return args, 'The coordinator may only read existing results and record its final report.'
                return parent_guard(name, args) if parent_guard else (args, None)

            runner.arg_guard = summary_guard
            runner.connectors = None
        verifier = TaskVerifier(runner, policy, bot, node.instruction,
                                    (node.budget or {}).get('acceptance_criteria', ''),
                                    (node.budget or {}).get('verification')) if mode != 'off' else None
        if verifier is not None:
            verifier.task_deadline = time.monotonic() + _NODE_SECONDS
        from sbot.core.delivery import LocalDelivery
        target = (node.budget or {}).get('delivery_target')
        delivery = LocalDelivery(self.local_broker, mission.owner_id, target, runner.arg_guard) if target else None
        completion = FinishStepTool(runner.workspace, (node.budget or {}).get('required_files'), verifier, mode, delivery)
        completion.require_record = bool(background)
        started = time.monotonic()
        outcome = await runner.run(
            bot,
            await self._system_prompt(mission, bot, runner),
            self._user_prompt(node, context) + ("\nInput snapshots (keep unchanged): " + str(input_manifest) if input_manifest else ""),
            lambda _event: None,
            turn_id=f"mn_{mission.id[:8]}_{node.id}",
            max_seconds=_NODE_SECONDS,
            extra_tools=[BlackboardTool(self.missions, mission.id, node.id), completion],
        )
        from sbot.core.task_result import TaskResult
        if runner.workspace != root_workspace:
            from sbot.core.assignment_workspace import rebase_result
            outcome.artifacts = rebase_result({'artifacts': outcome.artifacts}, runner.workspace, root_workspace)['artifacts']
            if completion.result:
                completion.result = rebase_result(completion.result, runner.workspace, root_workspace)
        result = TaskResult.from_outcome(
            outcome, completion.result if self.require_verified_results or background else None,
            require_completion=self.require_verified_results or bool(background),
        )
        result.attempts = context.attempt
        result.duration_seconds = round(time.monotonic() - started, 3)
        logger.info('Task reliability {}', json.dumps({
            'kind': 'mission_node', 'task_id': node.id, 'status': result.status,
            'failure_reason': result.failure_reason, 'verification_status': result.verification_status,
            'attempts': result.attempts, 'duration_seconds': result.duration_seconds,
            'tokens': result.cost.get('tokens', 0),
        }))
        output = result.summary
        if completion.result and completion.result.get('evidence'):
            output += '\n\nEvidence: ' + completion.result['evidence']
        if result.failure_reason:
            output = f"Task incomplete: {result.failure_reason}. " + output
        if result.failure_reason == 'empty_output':
            output += 'The assigned bot produced no output.'
        if result.failure_reason == 'missing_completion_record':
            output += ' Call finish_step with status, evidence and deliverable paths.'
        return NodeResult(status=result.scheduler_status, output=output,
                          artifacts=result.artifacts, cost=result.cost,
                          task_result=result.as_dict())

    async def _system_prompt(self, mission: Mission, bot: Any, runner: SpecialistRunner) -> str:
        prompt = (
            f"You are {bot.name}, {bot.role_title}.\n\n"
            f"# Charter\n{bot.charter}\n\n"
            f"# Mission\n{mission.goal}\n\n"
            "You are executing one step of this mission alongside other specialists. "
            "Do only your assigned step. Finish with the result itself — the next "
            "steps receive your final message and nothing else, so anything they "
            "need must either be in it or written to the shared blackboard. "
            "Before your final reply call finish_step. Report completed only after checking "
            "the assigned acceptance criteria; include concrete evidence and every requested "
            "deliverable path in files. File delivery is validated and published by that tool. "
            "If you cannot satisfy the task, report blocked or failed with the reason. "
            "A reply without finish_step is not a completed step."
        )
        # Appended rather than prepended so the bot's identity still leads. A
        # node used to run on charter alone, which made an assigned skill
        # unreachable and the bot's own memory unreadable.
        context = await runner.context_block(bot)
        return f"{prompt}\n\n{context}" if context else prompt

    @staticmethod
    def _user_prompt(node: MissionNode, context: NodeContext) -> str:
        parts = [f"# Your Step\n{node.title}\n\n{node.instruction}".strip()]
        required = (node.budget or {}).get('required_files', [])
        if required:
            parts.append('# Required Deliverables\n' + '\n'.join(required))
        if context.parent_outputs:
            lines = [f"### {nid}\n{text}" for nid, text in context.parent_outputs.items() if text]
            if lines:
                parts.append("# Results From Earlier Steps\n" + "\n\n".join(lines))
        if context.manifest:
            keys = "\n".join(
                f"- {key}: {entry.get('type')}"
                + (f", {entry['chars']} chars" if "chars" in entry else "")
                + (f", {entry['items']} items" if "items" in entry else "")
                for key, entry in context.manifest.items()
            )
            parts.append(
                "# Shared Blackboard\nThese keys exist. Use the `blackboard` tool to read "
                f"the ones you need — their contents are not included here.\n{keys}"
            )
        if context.last_error:
            parts.append(
                f"# Previous Attempt Failed (attempt {context.attempt})\n{context.last_error}\n\n"
                "Do not repeat the same approach."
            )
        return "\n\n".join(parts)
