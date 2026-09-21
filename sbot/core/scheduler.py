"""Schedule service: recurring/one-shot prompts delivered to the agent.

A single asyncio loop wakes once a minute — or immediately, via an event, after
schedule CRUD so new timings apply without waiting out the tick. Firing a job
runs a normal agent turn into the target session, so results appear in chat and
stream to any connected client.

A job carries the bot that owns it. Its run lands in that bot's own thread, the
same one delegation and missions write to, so a specialist's scheduled work is
executed with its charter, tools and memory instead of falling back to the
owner's Chief of Staff.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from loguru import logger

from sbot.db.stores import BotStore, ScheduleStore, SessionStore

# handle(user_id, session_id, prompt) -> final content
TurnHandler = Callable[[str, str, str], Awaitable[str | None]]

_MAX_SLEEP = 60.0
# Ceiling on turns this process starts from the scheduler at once. Enforced when
# a job is *spawned*, not inside the run: gating on a semaphore after the task
# exists only parks the overflow, so a cron minute every tenant shares
# ("0 9 * * *") still built one pending turn per user, each holding a stale row
# for up to _FIRE_TIMEOUT. Over the limit the job simply stays due and a later
# pass picks it up, earliest deadline first.
_MAX_CONCURRENT_FIRES = 8
# How long a run will stand aside for a live conversation in the same thread
# before going ahead anyway, so a chatty session can't postpone it forever.
# Measured in wall-clock, not passes: schedule CRUD wakes the loop immediately
# (notify_changed), so a pass count can be spent in seconds by an unrelated
# edit and the grace period would silently collapse to nothing.
_MAX_DEFER_SECONDS = 300.0
# Rows read per tick. Anything past this is still due on the next pass.
_DUE_BATCH = 200
# Backstop around a whole run. The runtime already caps a single turn
# (llm.max_turn_seconds), but a scheduled prompt can fan out into missions and
# subagents; without an outer bound a wedged run would hold its job in
# `_firing` forever, and since next_run_at only advances once a run finishes,
# that schedule would never fire again rather than losing one occurrence.
_FIRE_TIMEOUT = 3600.0
# Backstop around the bookkeeping that follows a run. Small, because it is two
# short queries — but it is what releases the job's slot, so it cannot be left
# unbounded either.
_SETTLE_TIMEOUT = 120.0


def resolve_tz(name: str) -> ZoneInfo:
    """Resolve an IANA tz name, falling back to UTC if it's unknown/unavailable
    (e.g. a slim container missing tzdata) rather than crashing the scheduler."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("Unknown scheduler timezone '{}', falling back to UTC", name)
        return ZoneInfo("UTC")


def compute_next_run(
    cron: str, interval_seconds: int, now: datetime | None = None, tz: str = "UTC"
) -> datetime | None:
    """Next fire time (as UTC). Cron is interpreted in ``tz`` so "0 7 * * *"
    means 07:00 wall-clock in that zone; the result is returned in UTC for
    storage/comparison."""
    now = now or datetime.now(timezone.utc)
    if cron:
        base = now.astimezone(resolve_tz(tz))
        try:
            nxt = croniter(cron, base).get_next(datetime)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"invalid cron expression: {cron}") from exc
        return nxt.astimezone(timezone.utc)
    if interval_seconds > 0:
        return now + timedelta(seconds=max(30, interval_seconds))
    return None  # one-shot with explicit next_run_at, or disabled


class SchedulerService:
    def __init__(
        self,
        schedules: ScheduleStore,
        sessions: SessionStore,
        handler: TurnHandler,
        timezone: str = "UTC",
        bots: BotStore | None = None,
        busy: Callable[[], set[str]] | None = None,
    ):
        self.schedules = schedules
        self.sessions = sessions
        self.handler = handler
        self.timezone = timezone
        self.bots = bots
        self.busy = busy
        self._wake = asyncio.Event()
        self._running = False
        self._task: asyncio.Task | None = None
        self._firing: set[str] = set()
        # job id -> monotonic clock when it first stood aside for a live chat
        self._defers: dict[str, float] = {}
        # Strong references to the in-flight runs. asyncio only keeps a weak one,
        # so a task nobody holds can be collected mid-run; and shutdown has to be
        # able to reach them.
        self._fires: set[asyncio.Task] = set()

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        fires = list(self._fires)
        for fire in fires:
            fire.cancel()
        if fires:
            await asyncio.gather(*fires, return_exceptions=True)

    def notify_changed(self) -> None:
        """Wake the loop after schedule CRUD so new timings apply immediately."""
        self._wake.set()

    async def _loop(self) -> None:
        while self._running:
            try:
                due = await self.schedules.due(datetime.now(timezone.utc), limit=_DUE_BATCH)
                # Deferral counts only mean anything while a job is still due;
                # dropping the rest keeps the map from outliving deleted rows.
                due_ids = {job.id for job in due}
                self._defers = {k: v for k, v in self._defers.items() if k in due_ids}
                for job in due:
                    # A job stays in `due` for the whole of its run — next_run_at
                    # only advances at mark_ran — so the head of this list is
                    # usually the jobs already in flight. Skip them before
                    # reporting the limit, or the log claims a backlog on every
                    # tick while one slow run is going.
                    if job.id in self._firing:
                        continue
                    if len(self._firing) >= _MAX_CONCURRENT_FIRES:
                        logger.info(
                            "Scheduler is at its {}-run limit; the rest stay due for a later pass",
                            _MAX_CONCURRENT_FIRES,
                        )
                        break
                    self._firing.add(job.id)
                    fire = asyncio.create_task(self._fire(job))
                    self._fires.add(fire)
                    fire.add_done_callback(self._fires.discard)
            except Exception:
                logger.exception("Scheduler tick failed")

            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=_MAX_SLEEP)
            except asyncio.TimeoutError:
                pass

    async def _runs_as_owner(self, session, job) -> bool:
        """Whether a turn in `session` would actually run as the job's own bot.

        The runtime picks the acting bot from the session, never from the job,
        so a schedule pointed at another bot's chat would quietly run with that
        bot's charter, model and tool allowlist while the API kept reporting the
        bot it was stamped with. The bot wins; the stale pointer is ignored.
        """
        if session.bot_id == job.bot_id:
            return True
        if session.group_id:
            from sbot.db.bot_groups import BotGroupStore

            group = await BotGroupStore(self.sessions.factory).get(session.group_id, job.user_id)
            return group is not None and group.leader_id == job.bot_id
        return False

    async def _target_session(self, job) -> str:
        """The chat this run writes into.

        An explicit target that has since been deleted (or was never the job
        owner's) falls through to the default rather than failing every run
        forever: `session_id` is a bare column, not a foreign key, so deleting
        a chat leaves schedules pointing at nothing.
        """
        stale = False
        if job.session_id:
            target = await self.sessions.get(job.session_id)
            if target is None or target.user_id != job.user_id:
                stale = True
                logger.warning(
                    "Schedule {} targets missing session {}, using the default chat instead",
                    job.id,
                    job.session_id,
                )
            elif not job.bot_id or await self._runs_as_owner(target, job):
                return job.session_id
            else:
                logger.warning(
                    "Schedule {} points at a chat that runs as another bot; using bot {}'s own thread",
                    job.id,
                    job.bot_id,
                )

        bot = None
        if job.bot_id and self.bots is not None:
            # Archived bots are excluded from this read, so a deleted specialist
            # degrades to the bot-less path — matching how the runtime resolves
            # a session whose bot is gone.
            bot = await self.bots.get(job.bot_id, job.user_id)
        if bot is not None:
            if stale:
                await self.schedules.update(job.user_id, job.id, session_id=None)
            thread = await self.sessions.thread_for_bot(job.user_id, bot.id, title=bot.name)
            return thread.id

        # channel "schedule" tags it so the UI shows an alarm-clock marker and
        # can flag it unread; the title stays clean (no emoji prefix). The id is
        # written back so later runs reuse this chat — recreating it every pass
        # left ~144 orphan sessions a day on a ten-minute job, with nothing to
        # collect them. A pointer the owner set deliberately is left alone.
        created = await self.sessions.create(job.user_id, title=job.name, channel="schedule")
        if stale or not job.session_id:
            await self.schedules.update(job.user_id, job.id, session_id=created.id)
        return created.id

    @staticmethod
    def _still_due(job) -> bool:
        """Whether the row, as it now stands, is still asking to run."""
        if not job.enabled or job.next_run_at is None:
            return False
        nxt = job.next_run_at
        if nxt.tzinfo is None:  # SQLite hands these back naive; they are UTC
            nxt = nxt.replace(tzinfo=timezone.utc)
        return nxt <= datetime.now(timezone.utc)

    def _defer_for_live_chat(self, job_id: str, session_id: str) -> bool:
        """Stand aside while someone is talking in the same thread.

        A run lands in the bot's own chat now, and a turn there takes a single
        per-session lock. Starting one while the user is mid-conversation would
        hang their reply for as long as the scheduled run takes — up to
        _FIRE_TIMEOUT, with nothing on screen to explain it. The job stays due
        instead and retries, bounded by _MAX_DEFER_SECONDS so it still runs.
        """
        if self.busy is None or session_id not in self.busy():
            self._defers.pop(job_id, None)
            return False
        now = time.monotonic()
        waiting_since = self._defers.setdefault(job_id, now)
        if now - waiting_since >= _MAX_DEFER_SECONDS:
            # Dropped, not left at the limit: otherwise an always-due job keeps a
            # spent counter forever and never stands aside again.
            self._defers.pop(job_id, None)
            logger.info(
                "Schedule {} waited {:.0f}s for session {}; running anyway",
                job_id,
                now - waiting_since,
                session_id,
            )
            return False
        logger.info(
            "Session {} is in use; schedule {} waits for the next pass",
            session_id,
            job_id,
        )
        return True

    async def _attempt(self, job_id: str) -> tuple[str, datetime] | None:
        """Run once and return status plus the claimed deadline.

        None means this pass was skipped and the row must be left untouched.
        """
        # Re-read before running. The row may have been paused, retimed or
        # deleted since the tick that queued it, and acting on the copy from
        # then would both ignore that edit and — via mark_ran — undo it.
        job = await self.schedules.get(job_id)
        if job is None or not self._still_due(job):
            logger.info("Schedule {} changed before it ran; skipping this pass", job_id)
            return None

        session_id = await self._target_session(job)
        if self._defer_for_live_chat(job_id, session_id):
            return None

        try:
            logger.info("Schedule {} firing into session {}", job.name, session_id)
            result = await self.handler(job.user_id, session_id, job.prompt)
            return ("ok" if result else "ok (no content)", job.next_run_at)
        except Exception as exc:
            logger.exception("Schedule {} failed", job_id)
            return f"error: {exc}", job.next_run_at

    async def _settle(self, job_id: str, status: str, expected_next_run_at: datetime) -> None:
        # Reschedule from the row as it stands now, not as it stood when the run
        # began: a retime made during a long run has to survive it.
        latest = await self.schedules.get(job_id)
        if latest is None:
            return
        try:
            next_run = compute_next_run(latest.cron, latest.interval_seconds, tz=self.timezone)
        except ValueError:
            next_run = None
        await self.schedules.settle_run(
            job_id,
            expected_next_run_at=expected_next_run_at,
            next_run_at=next_run,
            status=status,
        )

    async def _fire(self, job) -> None:
        job_id = job.id
        try:
            attempt = await asyncio.wait_for(self._attempt(job_id), timeout=_FIRE_TIMEOUT)
            status, expected_next_run_at = attempt if attempt is not None else (None, None)
        except asyncio.TimeoutError:
            logger.error("Schedule {} exceeded {}s and was abandoned", job_id, _FIRE_TIMEOUT)
            status = f"error: timed out after {int(_FIRE_TIMEOUT)}s"
            expected_next_run_at = job.next_run_at
        except asyncio.CancelledError:
            self._firing.discard(job_id)
            raise
        except Exception as exc:
            # Everything up to the handler — re-reading the row, resolving the
            # target chat — is settled too. Returning without recording a status
            # leaves next_run_at in the past, so the job is still due and the
            # loop restarts it on every tick with the failure never surfacing.
            logger.exception("Schedule {} could not be started", job_id)
            status = f"error: {exc}"
            expected_next_run_at = job.next_run_at

        try:
            if status is not None and expected_next_run_at is not None:
                await asyncio.wait_for(
                    self._settle(job_id, status, expected_next_run_at),
                    timeout=_SETTLE_TIMEOUT,
                )
        except Exception:
            logger.exception("Schedule {} ran but could not be rescheduled", job_id)
        finally:
            # Released last. Dropped any earlier and a CRUD-woken tick could see
            # the job neither firing nor yet rescheduled, and start it twice.
            self._firing.discard(job_id)
