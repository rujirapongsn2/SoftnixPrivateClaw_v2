"""Tests for skills, schedules, and the management stores/services."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from claw.core.scheduler import SchedulerService, compute_next_run
from claw.db.stores import ConnectorStore, ScheduleStore, SkillStore
from claw.tools.skills import ReadSkillTool, build_skills_summary


def as_utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes where Postgres returns aware; normalize for asserts."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- skills

async def test_skill_upsert_and_summary(db_factory, stores):
    skills = SkillStore(db_factory)
    user = await stores["users"].get_or_create_by_email("s@x.y")

    await skills.upsert(user.id, "daily-report", description="Summarize my day", content="Steps: ...")
    await skills.upsert(user.id, "translate", description="Translate carefully", content="...", enabled=False)

    enabled = await skills.enabled_for_user(user.id)
    assert [s.name for s in enabled] == ["daily-report"]

    summary = build_skills_summary(enabled)
    assert "daily-report" in summary and "Summarize my day" in summary
    assert "translate" not in summary


async def test_read_skill_tool(db_factory, stores):
    skills = SkillStore(db_factory)
    user = await stores["users"].get_or_create_by_email("s2@x.y")
    await skills.upsert(user.id, "recipe", description="", content="Secret sauce steps")
    tool = ReadSkillTool(skills, user.id)

    assert "Secret sauce steps" in await tool.execute(name="recipe")
    assert (await tool.execute(name="missing")).startswith("Error")


async def test_skill_isolation_between_users(db_factory, stores):
    skills = SkillStore(db_factory)
    alice = await stores["users"].get_or_create_by_email("alice@x.y")
    bob = await stores["users"].get_or_create_by_email("bob@x.y")
    await skills.upsert(alice.id, "private", description="", content="alice only")

    tool = ReadSkillTool(skills, bob.id)
    assert (await tool.execute(name="private")).startswith("Error")


# ---------------------------------------------------------------- schedule computation

def test_compute_next_run_interval():
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    result = compute_next_run("", 3600, now)
    assert result == now + timedelta(hours=1)


def test_compute_next_run_cron():
    now = datetime(2026, 7, 4, 12, 30, tzinfo=timezone.utc)
    result = compute_next_run("0 9 * * *", 0, now)  # daily 09:00
    assert (result.hour, result.minute) == (9, 0)
    assert result > now


def test_compute_next_run_invalid_cron_raises():
    with pytest.raises(ValueError):
        compute_next_run("not a cron", 0)


def test_compute_next_run_one_shot_returns_none():
    assert compute_next_run("", 0) is None


# ---------------------------------------------------------------- scheduler firing

async def test_scheduler_fires_due_job_and_reschedules(db_factory, stores):
    schedules = ScheduleStore(db_factory)
    user = await stores["users"].get_or_create_by_email("cron@x.y")
    fired: list[tuple[str, str]] = []

    async def handler(user_id: str, session_id: str, prompt: str) -> str:
        fired.append((user_id, prompt))
        return "done"

    await schedules.create(
        user.id,
        name="tick",
        prompt="check my tasks",
        interval_seconds=3600,
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    service = SchedulerService(schedules, stores["sessions"], handler)
    service.start()
    try:
        for _ in range(50):
            if fired:
                break
            await asyncio.sleep(0.05)
    finally:
        await service.stop()

    assert fired == [(user.id, "check my tasks")]
    job = (await schedules.list_for_user(user.id))[0]
    assert job.last_status == "ok"
    assert job.next_run_at is not None and as_utc(job.next_run_at) > datetime.now(timezone.utc)


async def test_scheduler_disables_completed_one_shot(db_factory, stores):
    schedules = ScheduleStore(db_factory)
    user = await stores["users"].get_or_create_by_email("once@x.y")

    async def handler(user_id: str, session_id: str, prompt: str) -> str:
        return "done"

    await schedules.create(
        user.id,
        name="once",
        prompt="one time thing",
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    service = SchedulerService(schedules, stores["sessions"], handler)
    service.start()
    try:
        for _ in range(50):
            job = (await schedules.list_for_user(user.id))[0]
            if job.last_run_at is not None:
                break
            await asyncio.sleep(0.05)
    finally:
        await service.stop()

    job = (await schedules.list_for_user(user.id))[0]
    assert job.enabled is False
    assert job.next_run_at is None


async def test_scheduler_creates_session_when_none_given(db_factory, stores):
    schedules = ScheduleStore(db_factory)
    user = await stores["users"].get_or_create_by_email("nosess@x.y")
    seen_sessions: list[str] = []

    async def handler(user_id: str, session_id: str, prompt: str) -> str:
        seen_sessions.append(session_id)
        return "ok"

    await schedules.create(
        user.id, name="autosess", prompt="hi",
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    service = SchedulerService(schedules, stores["sessions"], handler)
    service.start()
    try:
        for _ in range(50):
            if seen_sessions:
                break
            await asyncio.sleep(0.05)
    finally:
        await service.stop()

    assert seen_sessions
    created = await stores["sessions"].list_for_user(user.id)
    # A scheduled run tags its session with channel="schedule" (for the UI's
    # alarm-clock marker) and uses the job name as the clean title (no emoji).
    sess = next(s for s in created if s.id == seen_sessions[0])
    assert sess.channel == "schedule"
    assert sess.title == "autosess"


async def test_scheduler_creates_a_fresh_session_each_run(db_factory, stores):
    schedules = ScheduleStore(db_factory)
    user = await stores["users"].get_or_create_by_email("recur@x.y")
    seen: list[str] = []

    async def handler(user_id: str, session_id: str, prompt: str) -> str:
        seen.append(session_id)
        return "ok"

    job = await schedules.create(
        user.id, name="daily", prompt="hi", interval_seconds=3600,
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    service = SchedulerService(schedules, stores["sessions"], handler)
    # Fire the same job twice directly — each run must open its own session,
    # never reuse one (job.session_id stays None across runs).
    await service._fire(job)
    job = (await schedules.list_for_user(user.id))[0]
    await service._fire(job)

    assert len(seen) == 2 and seen[0] != seen[1]
    created = await stores["sessions"].list_for_user(user.id)
    assert len([s for s in created if s.channel == "schedule"]) == 2


# ---------------------------------------------------------------- connectors store

async def test_connector_store_crud(db_factory, stores):
    connectors = ConnectorStore(db_factory)
    user = await stores["users"].get_or_create_by_email("mcp@x.y")

    row = await connectors.upsert(
        user.id, "tavily", transport="http", url="https://mcp.tavily.com/mcp", env={}, enabled=True
    )
    assert row.transport == "http"

    row = await connectors.upsert(user.id, "tavily", enabled=False)
    assert row.enabled is False
    assert (await connectors.enabled_for_user(user.id)) == []

    assert await connectors.delete(user.id, row.id) is True
    assert await connectors.list_for_user(user.id) == []
