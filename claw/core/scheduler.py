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

    def notify_changed(self) -> None:
        """Wake the loop after schedule CRUD so new timings apply immediately."""
        self._wake.set()

    async def _loop(self) -> None:
        while self._running:
            try:
                due = await self.schedules.due(datetime.now(timezone.utc))
                for job in due:
                    if job.id not in self._firing:
                        self._firing.add(job.id)
                        asyncio.create_task(self._fire(job))
            except Exception:
                logger.exception("Scheduler tick failed")

            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=_MAX_SLEEP)
            except asyncio.TimeoutError:
                pass

    async def _fire(self, job) -> None:
        try:
            session_id = job.session_id
            if not session_id:
                # A fresh session each run (job.session_id stays None). channel
                # "schedule" tags it so the UI shows an alarm-clock marker and
                # can flag it unread; the title stays clean (no emoji prefix).
                created = await self.sessions.create(
                    job.user_id, title=job.name, channel="schedule"
                )
                session_id = created.id
            logger.info("Schedule {} firing into session {}", job.name, session_id)
            result = await self.handler(job.user_id, session_id, job.prompt)
            status = "ok" if result else "ok (no content)"
        except Exception as exc:
            logger.exception("Schedule {} failed", job.id)
            status = f"error: {exc}"
        finally:
            self._firing.discard(job.id)

        try:
            next_run = compute_next_run(job.cron, job.interval_seconds, tz=self.timezone)
        except ValueError:
            next_run = None
        await self.schedules.mark_ran(job.id, next_run_at=next_run, status=status)
