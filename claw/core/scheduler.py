"""Schedule service: recurring/one-shot prompts delivered to the agent.

A single asyncio loop sleeps until the earliest due job (woken early on config
changes via an event — no fixed-interval polling of the API path). Firing a job
runs a normal agent turn into the target session, so results appear in chat and
stream to any connected client.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from loguru import logger

from claw.db.stores import ScheduleStore, SessionStore

# handle(user_id, session_id, prompt) -> final content
TurnHandler = Callable[[str, str, str], Awaitable[str | None]]

_MAX_SLEEP = 60.0
# Ceiling on turns this process starts from the scheduler at once. Enforced when
# a job is spawned: a cron minute every tenant shares ("0 9 * * *") would
# otherwise build one concurrent turn per user. Over the limit the job simply
# stays due and a later pass picks it up, earliest deadline first.
_MAX_CONCURRENT_FIRES = 8
# Rows read per tick. Anything past this is still due on the next pass.
_DUE_BATCH = 200
# Backstop around a whole run. next_run_at only advances once a run finishes, so
# without an outer bound a wedged run means that schedule never fires again.
_FIRE_TIMEOUT = 3600.0
# Backstop around the bookkeeping that follows a run — it is what releases the
# job's slot, so it cannot be unbounded either.
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
    ):
        self.schedules = schedules
        self.sessions = sessions
        self.handler = handler
        self.timezone = timezone
        self._wake = asyncio.Event()
        self._running = False
        self._task: asyncio.Task | None = None
        self._firing: set[str] = set()
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

    @staticmethod
    def _still_due(job) -> bool:
        """Whether the row, as it now stands, is still asking to run."""
        if not job.enabled or job.next_run_at is None:
            return False
        nxt = job.next_run_at
        if nxt.tzinfo is None:  # SQLite hands these back naive; they are UTC
            nxt = nxt.replace(tzinfo=timezone.utc)
        return nxt <= datetime.now(timezone.utc)

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

        session_id = job.session_id
        if session_id:
            # `session_id` is a bare column, not a foreign key, so a deleted chat
            # leaves the schedule pointing at nothing. Fall back rather than
            # failing every run forever.
            target = await self.sessions.get(session_id)
            if target is None or target.user_id != job.user_id:
                logger.warning(
                    "Schedule {} targets missing session {}, using a fresh chat instead",
                    job_id,
                    session_id,
                )
                session_id = None
        if not session_id:
            # channel "schedule" tags it so the UI shows an alarm-clock marker
            # and can flag it unread; the title stays clean (no emoji prefix).
            # The id is written back so later runs reuse this chat instead of
            # leaving a fresh orphan session behind on every pass.
            created = await self.sessions.create(job.user_id, title=job.name, channel="schedule")
            session_id = created.id
            await self.schedules.update(job.user_id, job_id, session_id=session_id)

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
