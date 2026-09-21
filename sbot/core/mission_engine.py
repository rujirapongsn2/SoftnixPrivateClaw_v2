"""MissionEngine: deterministic DAG scheduler for long-horizon missions.

Graph Engineering (PRD §4.3): the engine schedules, bots think. It never calls
an LLM — every decision here is a pure function of persisted node state, which
is what lets a whole mission be replayed against a fake executor in a test.

A node is only ever executed by the coroutine that successfully *claimed* it in
the database (`MissionStore.claim_node`, a conditional UPDATE). Readiness alone
is not permission to run: the scheduler re-reads state on every pass, so any
node that is merely "selected but not yet started" would otherwise be selected
again on the next pass and run twice.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from claw.jobs.graph import CycleDetectedError, InvalidGraphError, validate_dag  # noqa: F401

from sbot.db.models import Mission, MissionNode
from sbot.db.stores import MissionStore

# Statuses a node never leaves. `skipped` is settled *and* satisfies dependents:
# skipping is a deliberate decision (a replan, or a human), so the graph has to
# be allowed past it instead of deadlocking behind it forever.
TERMINAL_STATUSES = {"done", "skipped"}
SATISFIES_DEPENDENTS = {"done", "skipped"}
# What an executor is allowed to report back. Anything else is treated as an
# error rather than silently persisted, so a buggy executor cannot invent a
# status that the scheduler's classification below does not understand.
EXECUTOR_STATUSES = {"done", "error", "skipped", "awaiting_human"}
# Kinds the engine parks for a human instead of executing (PRD §3.4 `gate`).
GATE_KINDS = {"gate"}

_DEFAULT_LEASE_SECONDS = 300.0
_RETRY_BASE_DELAY = 2.0
_RETRY_MAX_DELAY = 60.0
# Longest the scheduler will idle while waiting for a retry backoff to expire.
# Bounded so an externally-set status (cancel/pause) is noticed promptly.
_MAX_IDLE_SLEEP = 5.0
# What a parent's output contributes to a child's context. The full text lives
# on the node row and in the blackboard; the edge carries a handle, not a
# payload (PRD §3.5 context economy).
_PARENT_SUMMARY_CHARS = 400


def dependency_satisfied(node: MissionNode) -> bool:
    if node.status == 'done':
        return True
    # Preserve legacy skips of non-delivery steps, but never waive a requested
    # deliverable merely because a replanner changed the step status.
    return node.status == 'skipped' and not (node.budget or {}).get('required_files')


@dataclass(slots=True)
class NodeContext:
    """Everything a node gets besides its own instruction."""

    manifest: dict[str, dict[str, Any]]
    parent_outputs: dict[str, str]
    attempt: int
    # Why the previous attempt failed. A retry that isn't told what went wrong
    # just reproduces the same failure, so this is fed back into the prompt.
    last_error: str | None = None


@dataclass(slots=True)
class NodeResult:
    status: str
    output: str = ""
    blackboard_updates: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    # {"tokens": int, "cost": float} — charged against the mission budget.
    cost: dict[str, float] = field(default_factory=dict)
    task_result: dict[str, Any] | None = None


@dataclass(slots=True)
class ReplanOutcome:
    """What a replan attempt did to the graph, and what thinking about it cost.

    `changed` should reflect what the graph actually looks like afterwards, not
    what the replanner intended: a hook that reports a change it did not make
    would otherwise buy itself another pass, and the loop would spend its whole
    replan allowance discovering that nothing moved.
    """

    changed: bool
    # {"tokens": int, "cost": float} — a replan is an LLM call inside a budgeted
    # loop, so it is charged like a node instead of being free.
    cost: dict[str, float] = field(default_factory=dict)


NodeExecutor = Callable[[MissionNode, NodeContext], Awaitable[NodeResult]]
# Called when nodes have exhausted their attempts. The hook is where an LLM
# (Chief of Staff) is allowed to think; keeping it behind a callback is what
# keeps this module deterministic.
ReplanHook = Callable[[str, list[MissionNode]], Awaitable[ReplanOutcome]]


def _summarize(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= _PARENT_SUMMARY_CHARS:
        return text
    return text[:_PARENT_SUMMARY_CHARS] + f"… [{len(text)} chars; read the blackboard for the rest]"


class MissionEngine:
    def __init__(
        self,
        mission_store: MissionStore,
        node_executor: NodeExecutor,
        max_parallel_nodes: int = 4,
        worker_id: str | None = None,
        replan_hook: ReplanHook | None = None,
        max_replans: int = 3,
        lease_seconds: float = _DEFAULT_LEASE_SECONDS,
        retry_base_delay: float = _RETRY_BASE_DELAY,
        resource_hook: Callable[[str], Awaitable[bool]] | None = None,
    ):
        self.resource_hook = resource_hook
        self.store = mission_store
        self.node_executor = node_executor
        self.max_parallel_nodes = max_parallel_nodes
        self.worker_id = worker_id or uuid.uuid4().hex
        self.replan_hook = replan_hook
        self.max_replans = max_replans
        self.lease_seconds = lease_seconds
        self.retry_base_delay = retry_base_delay
        self._consecutive_failures: dict[str, int] = {}

    # ------------------------------------------------------------- validation
    validate_dag = staticmethod(validate_dag)

    # ---------------------------------------------------------------- budget
    @staticmethod
    def _budget_exceeded(
        mission: Mission, spent: dict[str, float], elapsed: float, nodes: list[MissionNode]
    ) -> str | None:
        budget = mission.budget or {}
        limit = budget.get("max_wall_seconds")
        if limit and spent.get("seconds", 0) + elapsed >= limit:
            return "max_wall_seconds"
        limit = budget.get("max_tokens")
        if limit and spent.get("tokens", 0) >= limit:
            return "max_tokens"
        # No `max_cost` check: nothing populates `spent["cost"]` — the specialist
        # runner reports tokens only — so it would be a ceiling that never fires.
        # MissionService rejects the key at plan time instead of accepting a
        # dollar limit it cannot honour.
        limit = budget.get("max_node_attempts")
        if limit and sum(n.attempts for n in nodes) >= limit:
            return "max_node_attempts"
        return None

    # ------------------------------------------------------------------- run
    async def run_mission(self, mission_id: str) -> str:
        """Drive the DAG until it settles. Returns the mission's final status.

        Safe to call again on the same mission: state lives in the database, so
        a second call resumes rather than restarts (expired leases are reclaimed
        first, which is also how a mission survives a process restart).
        """
        await self.store.reclaim_expired_leases(mission_id)

        mission = await self.store.get_mission_unchecked(mission_id)
        if mission is None:
            return "not_found"
        spent: dict[str, float] = {
            "tokens": float((mission.spent or {}).get("tokens", 0)),
            "cost": float((mission.spent or {}).get("cost", 0)),
            "seconds": float((mission.spent or {}).get("seconds", 0)),
        }
        started = time.monotonic()
        # node_id -> Task. Held for the whole run: asyncio keeps only a weak
        # reference to a running task, so dropping this would let a long node be
        # garbage-collected mid-flight and stall forever as `running`.
        inflight: dict[str, asyncio.Task[NodeResult | None]] = {}
        retry_at: dict[str, float] = {}
        replans = 0

        try:
            while True:
                elapsed = time.monotonic() - started
                mission = await self.store.get_mission_unchecked(mission_id)
                if mission is None:
                    return "not_found"
                if mission.status != "running":
                    # Paused/cancelled from outside. Let in-flight nodes finish
                    # rather than cancelling mid-LLM-call, which would leave them
                    # claimed with no result.
                    await self._drain(inflight, spent)
                    await self._persist_spent(mission_id, spent, time.monotonic() - started)
                    return mission.status

                await self.store.reclaim_expired_leases(mission_id)
                nodes = await self.store.get_nodes(mission_id)
                by_id = {n.id: n for n in nodes}
                now = time.monotonic()
                over_budget = self._budget_exceeded(mission, spent, elapsed, nodes)

                ready: list[MissionNode] = []
                unsettled = 0
                exhausted: list[MissionNode] = []
                gated = 0
                backoff_waits: list[float] = []

                for node in nodes:
                    if node.status in TERMINAL_STATUSES:
                        continue
                    unsettled += 1
                    if node.id in inflight or node.status == "running":
                        continue
                    if node.status == "awaiting_human":
                        gated += 1
                        continue
                    if node.status == "error":
                        if node.attempts >= node.max_attempts:
                            exhausted.append(node)
                            continue
                        due = retry_at.get(node.id)
                        if due is not None and due > now:
                            backoff_waits.append(due - now)
                            continue
                    deps = node.depends_on or []
                    if all(
                        by_id.get(d) is not None and dependency_satisfied(by_id[d])
                        for d in deps
                    ):
                        ready.append(node)

                if unsettled == 0 and any(
                    n.status == 'skipped' and not dependency_satisfied(n) for n in nodes
                ):
                    await self._drain(inflight, spent)
                    await self._persist_spent(mission_id, spent, time.monotonic() - started)
                    await self.store.update_mission(mission_id, status='failed')
                    return 'failed'
                if unsettled == 0:
                    # Drained before the spend is written, not after. A node task
                    # can have committed itself `done` — so it no longer counts as
                    # unsettled — while its Task is not yet marked complete and
                    # its cost is still unharvested. The `finally` drain below
                    # would fold that cost in *after* this write, and a resumed
                    # mission re-reads spend from the row, so those tokens would be
                    # permanently missing from the ceiling they are meant to count
                    # against.
                    await self._drain(inflight, spent)
                    await self._persist_spent(mission_id, spent, time.monotonic() - started)
                    await self.store.update_mission(mission_id, status="completed")
                    logger.info("Mission {} completed", mission_id)
                    return "completed"

                # Nothing can move on its own and something is permanently
                # broken: give the replan hook a bounded number of chances to
                # patch the graph before declaring failure (PRD §4.3).
                if exhausted and not inflight and not ready and not over_budget:
                    if self.replan_hook is not None and replans < self.max_replans:
                        replans += 1
                        logger.info(
                            "Mission {} replan {}/{} after {} exhausted node(s)",
                            mission_id, replans, self.max_replans, len(exhausted),
                        )
                        try:
                            replanned = await self.replan_hook(mission_id, list(exhausted))
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # A replan is a best-effort recovery attempt. Letting
                            # it escape would crash the scheduler and hide the
                            # node failure that is the real reason this mission
                            # cannot finish.
                            logger.exception("Mission {} replan hook raised", mission_id)
                            replanned = ReplanOutcome(changed=False)
                        spent["tokens"] += float(replanned.cost.get("tokens", 0) or 0)
                        spent["cost"] += float(replanned.cost.get("cost", 0) or 0)
                        if replanned.changed:
                            continue
                    await self._persist_spent(mission_id, spent, time.monotonic() - started)
                    await self.store.update_mission(mission_id, status="failed")
                    logger.error(
                        "Mission {} failed: {} node(s) exhausted their attempts",
                        mission_id, len(exhausted),
                    )
                    return "failed"

                slots = 0 if over_budget else max(0, self.max_parallel_nodes - len(inflight))
                for node in ready[:slots]:
                    claimed = await self.store.claim_node(
                        node.mission_id, node.id, self.worker_id, self.lease_seconds
                    )
                    if claimed is None:
                        # Another worker won the race, or the row moved on.
                        continue
                    inflight[claimed.id] = asyncio.create_task(
                        self._run_leased_node(mission_id, claimed, by_id),
                        name=f"mission-node:{claimed.id}",
                    )

                if inflight:
                    await self._settle(inflight, spent, retry_at)
                    continue

                if over_budget:
                    await self._persist_spent(mission_id, spent, time.monotonic() - started)
                    if self.resource_hook is not None:
                        if await self.resource_hook(mission_id):
                            continue
                        await self.store.update_mission(mission_id, status='failed')
                        return 'failed'
                    await self.store.update_mission(mission_id, status="paused")
                    logger.warning("Mission {} paused: budget {} exhausted", mission_id, over_budget)
                    return "paused"

                if any(n.status == "running" for n in nodes):
                    # Another worker (or an orphan with a live lease) still owns
                    # work. Keep polling until it settles or can be reclaimed.
                    await asyncio.sleep(min(_MAX_IDLE_SLEEP, max(0.05, self.lease_seconds / 3)))
                    continue

                if backoff_waits:
                    await asyncio.sleep(min(min(backoff_waits), _MAX_IDLE_SLEEP))
                    continue

                # Every remaining node is waiting on something that will never
                # arrive (a gate, or a dependency that settled as unsatisfying).
                await self._persist_spent(mission_id, spent, time.monotonic() - started)
                await self.store.update_mission(mission_id, status="blocked")
                logger.warning(
                    "Mission {} blocked: {} node(s) unsettled, {} awaiting a human",
                    mission_id, unsettled, gated,
                )
                return "blocked"
        finally:
            await self._drain(inflight, spent)

    # --------------------------------------------------------------- helpers
    async def _run_leased_node(self, mission_id, node, by_id):
        task = asyncio.create_task(self._run_node(mission_id, node, by_id))

        async def heartbeat():
            while True:
                await asyncio.sleep(max(0.01, self.lease_seconds / 3))
                if not await self.store.renew_node_lease(
                    mission_id, node.id, self.worker_id, node.attempts, self.lease_seconds
                ):
                    raise RuntimeError("mission node lease lost")

        pulse = asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait({task, pulse}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return await task
            await pulse  # propagate lost lease / database failure; don't report success
        finally:
            for pending in (pulse, task):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(pulse, task, return_exceptions=True)

    async def _run_node(
        self, mission_id: str, node: MissionNode, by_id: dict[str, MissionNode]
    ) -> NodeResult | None:
        if node.kind in GATE_KINDS:
            await self.store.finish_node(node.mission_id, node.id, status="awaiting_human",
                                         lease_owner=self.worker_id, attempt=node.attempts)
            return None

        try:
            manifest = await self.store.blackboard_manifest(mission_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._abandon_node(node, f"Error: reading the blackboard failed: {exc}")

        parents = {
            dep: _summarize(by_id[dep].output)
            for dep in (node.depends_on or [])
            if by_id.get(dep) is not None
        }
        # Full parent results must be retrievable even if the model did not
        # voluntarily publish a blackboard entry.
        for dep in parents:
            key = f"result:{dep}"
            parents[dep] += f"\nFull result: blackboard key {key}"
            metadata = await self.store.blackboard_read(mission_id, f'contract:{dep}')
            if isinstance(metadata, dict):
                # Preserve status, references and evidence independently of the
                # prose preview. Evidence payloads remain lazily retrievable.
                import json
                parents[dep] += '\nResult metadata: ' + json.dumps({
                    'status': metadata.get('status'),
                    'verification_status': metadata.get('verification_status', 'not_verified'),
                    'failure_reason': metadata.get('failure_reason'),
                    'evidence_ref': f'contract:{dep}',
                    'delivery': metadata.get('delivery', {}),
                }, ensure_ascii=False)
        context = NodeContext(
            manifest=manifest,
            parent_outputs=parents,
            attempt=node.attempts,
            # claim_node already incremented attempts, so >1 means node.output
            # still holds the previous attempt's error message.
            last_error=node.output if node.attempts > 1 else None,
        )

        try:
            result = await self.node_executor(node, context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # an executor blowing up is a node error, not an engine crash
            logger.exception("Mission node {} raised", node.id)
            result = NodeResult(status="error", output=f"Error: {exc}")

        try:
            # Runtime result/contract keys are committed atomically with the
            # lease-checked node transition below, never by a stale worker.
            for key, value in (result.blackboard_updates or {}).items():
                if key.startswith(('result:', 'contract:', 'scope:', 'delivery:')):
                    raise ValueError('runtime blackboard namespace is reserved')
                await self.store.blackboard_write(mission_id, key, value, node_id=node.id)

            status = result.status if result.status in EXECUTOR_STATUSES else "error"
            if status != result.status:
                result = NodeResult(
                    status=status,
                    output=f"Error: executor returned unknown status {result.status!r}",
                    cost=result.cost,
                )
            await self.store.finish_node(
                node.mission_id,
                node.id,
                status=status,
                output=result.output,
                artifacts=result.artifacts,
                cost=result.cost,
                lease_owner=self.worker_id, attempt=node.attempts,
                task_result=result.task_result,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The work is done and already paid for, so its cost still counts —
            # only the recording of it failed.
            return await self._abandon_node(
                node, f"Error: recording the result failed: {exc}", cost=result.cost
            )
        return result

    async def _abandon_node(
        self, node: MissionNode, output: str, cost: dict[str, float] | None = None
    ) -> NodeResult:
        """Release a node whose bookkeeping — not its executor — failed.

        Everything around the executor talks to the database, and a raise there
        escapes to _harvest, which only logs it. The node would keep the
        `running` status claim_node gave it, and the scheduler skips running
        nodes, so the mission settles as `blocked` with the finished work thrown
        away. Leases are reclaimed once per run_mission call, so nothing in this
        run picks it back up either.

        Reported as a node error so the ordinary retry and attempt limit apply:
        a transient database blip costs one attempt, not the whole mission.
        """
        logger.exception("Mission node {} failed around its executor", node.id)
        try:
            await self.store.finish_node(
                node.mission_id, node.id, status="error", output=output, cost=cost,
                lease_owner=self.worker_id, attempt=node.attempts
            )
        except Exception:
            # The database is what just failed, so there is nowhere left to
            # record this: the lease expiring is the remaining recovery path.
            logger.exception("Mission node {} could not be released", node.id)
        return NodeResult(status="error", output=output, cost=cost or {})

    async def _settle(
        self,
        inflight: dict[str, asyncio.Task[NodeResult | None]],
        spent: dict[str, float],
        retry_at: dict[str, float],
    ) -> None:
        """Wait for at least one node to finish, then fold its result in."""
        done, _ = await asyncio.wait(inflight.values(), return_when=asyncio.FIRST_COMPLETED)
        finished = [nid for nid, task in inflight.items() if task in done]
        for node_id in finished:
            task = inflight.pop(node_id)
            result = self._harvest(node_id, task, spent)
            if result is not None and result.status == "error":
                retry_at[node_id] = time.monotonic() + self._backoff(node_id)
            else:
                self._consecutive_failures.pop(node_id, None)

    def _backoff(self, node_id: str) -> float:
        # Doubling per consecutive failure of this node, capped. Held in memory
        # only: after a restart the first retry fires immediately, which is
        # harmless because `attempts` is persisted and still bounds the total.
        count = self._consecutive_failures.get(node_id, 0) + 1
        self._consecutive_failures[node_id] = count
        return min(self.retry_base_delay * (2 ** (count - 1)), _RETRY_MAX_DELAY)

    def _harvest(
        self, node_id: str, task: asyncio.Task[NodeResult | None], spent: dict[str, float]
    ) -> NodeResult | None:
        try:
            result = task.result()
        except asyncio.CancelledError:
            return None
        except Exception:
            logger.exception("Mission node task {} failed outside the executor", node_id)
            return None
        if result is not None:
            spent["tokens"] += float(result.cost.get("tokens", 0) or 0)
            spent["cost"] += float(result.cost.get("cost", 0) or 0)
        return result

    async def _drain(
        self, inflight: dict[str, asyncio.Task[NodeResult | None]], spent: dict[str, float]
    ) -> None:
        while inflight:
            await asyncio.wait(inflight.values(), return_when=asyncio.ALL_COMPLETED)
            for node_id in list(inflight):
                self._harvest(node_id, inflight.pop(node_id), spent)

    async def _persist_spent(self, mission_id: str, spent: dict[str, float], elapsed: float) -> None:
        await self.store.update_mission(
            mission_id,
            spent={
                "tokens": int(spent["tokens"]),
                "cost": round(spent["cost"], 6),
                "seconds": round(spent["seconds"] + elapsed, 3),
            },
        )
