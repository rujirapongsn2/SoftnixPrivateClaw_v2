"""compute_next_run timezone handling + tz fallback."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from claw.core.runtime import TurnFailed
from claw.core.scheduler import SchedulerService, compute_next_run, resolve_tz
from claw.db.stores import ScheduleStore, SessionStore, UserStore


def test_cron_interpreted_in_bangkok_returns_utc():
    now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)  # 21:00 ICT
    # "07:00 daily" in Bangkok is 00:00 UTC the next day.
    nxt = compute_next_run("0 7 * * *", 0, now=now, tz="Asia/Bangkok")
    assert nxt == datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
    assert nxt.astimezone(ZoneInfo("Asia/Bangkok")).hour == 7


def test_cron_utc_default_unchanged():
    now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("0 7 * * *", 0, now=now)  # tz defaults to UTC
    assert nxt == datetime(2026, 7, 6, 7, 0, tzinfo=timezone.utc)


def test_interval_ignores_tz():
    now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("", 600, now=now, tz="Asia/Bangkok")
    assert nxt == datetime(2026, 7, 5, 14, 10, tzinfo=timezone.utc)


def test_invalid_cron_raises():
    try:
        compute_next_run("not a cron", 0, tz="Asia/Bangkok")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_unknown_tz_falls_back_to_utc():
    assert resolve_tz("Not/AZone").key == "UTC"
    # And compute_next_run still works (treats as UTC) rather than crashing.
    now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
    nxt = compute_next_run("0 7 * * *", 0, now=now, tz="Not/AZone")
    assert nxt == datetime(2026, 7, 6, 7, 0, tzinfo=timezone.utc)


async def test_turn_failed_records_error_status_not_ok(db_factory):
    """A TurnFailed from the runtime must show up as an "error:" last_status,
    not "ok" — a scheduled job that's actually failing should never look
    healthy in its run history."""

    schedules = ScheduleStore(db_factory)
    sessions = SessionStore(db_factory, is_postgres=False)
    users = UserStore(db_factory)

    async def boom_handler(user_id: str, session_id: str, prompt: str) -> str | None:
        raise TurnFailed("the model could not be reached")

    service = SchedulerService(schedules, sessions, boom_handler)

    user = await users.get_or_create_by_email("scheduled@x.y")
    job = await schedules.create(
        user.id, name="daily report", cron="", interval_seconds=3600, prompt="hi"
    )

    await service._fire(job)

    updated = await schedules.get(job.id)
    assert updated.last_status.startswith("error:")
    assert "could not be reached" in updated.last_status
