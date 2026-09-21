"""Scheduled tasks in Bot Mode: which bot runs them, and editing a one-shot."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from sbot.api.deps import current_user, get_state
from sbot.api.manage import router
from sbot.core.scheduler import (
    _MAX_CONCURRENT_FIRES,
    _MAX_DEFER_SECONDS,
    SchedulerService,
)
from sbot.db.stores import ScheduleStore
from sbot.tools.schedule import ScheduleTool


@pytest.fixture
async def schedules(db_factory):
    return ScheduleStore(db_factory)


def _service(schedules, stores, handler, **kwargs):
    return SchedulerService(
        schedules, stores["sessions"], handler, bots=stores["bots"], **kwargs
    )


def _utc(dt):
    """SQLite drops the offset Postgres keeps; both store UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def test_fire_runs_in_the_owning_bots_own_thread(schedules, stores):
    """A task created by a specialist must run as that specialist, in the same
    thread the user talks to it in — not as a bot-less turn the sidebar files
    under "Other"."""
    user = await stores["users"].get_or_create_by_email("owner@sbot.test")
    bot = await stores["bots"].create(owner_id=user.id, name="Analyst")
    seen: list[str] = []

    async def handler(user_id, session_id, prompt):
        seen.append(session_id)
        return "done"

    job = await schedules.create(
        user.id,
        name="daily report",
        interval_seconds=3600,
        prompt="report",
        bot_id=bot.id,
        next_run_at=datetime.now(timezone.utc),
    )
    await _service(schedules, stores, handler)._fire(job)

    thread = await stores["sessions"].thread_for_bot(user.id, bot.id, title=bot.name)
    assert seen == [thread.id]
    assert (await stores["sessions"].get(seen[0])).bot_id == bot.id
    assert (await schedules.get(job.id)).last_status == "ok"


async def test_fire_falls_back_when_the_bot_or_session_is_gone(schedules, stores):
    """Archiving a bot or deleting its chat must not wedge the task: the turn
    still happens, so the user sees output instead of silence."""
    user = await stores["users"].get_or_create_by_email("gone@sbot.test")
    bot = await stores["bots"].create(owner_id=user.id, name="Retired")
    stale = await stores["sessions"].create(user.id, title="deleted later")
    await stores["sessions"].delete(stale.id)
    await stores["bots"].archive(bot.id, user.id)
    seen: list[str] = []

    async def handler(user_id, session_id, prompt):
        seen.append(session_id)
        return "done"

    job = await schedules.create(
        user.id,
        name="orphan",
        interval_seconds=3600,
        prompt="report",
        bot_id=bot.id,
        session_id=stale.id,
        next_run_at=datetime.now(timezone.utc),
    )
    await _service(schedules, stores, handler)._fire(job)

    assert len(seen) == 1 and seen[0] != stale.id
    assert (await schedules.get(job.id)).last_status == "ok"


async def test_due_is_ordered_and_bounded(schedules, stores):
    """The scheduler reads this once a minute for every tenant at once, so a
    backlog has to arrive earliest-first and in slices."""
    user = await stores["users"].get_or_create_by_email("many@sbot.test")
    now = datetime.now(timezone.utc)
    for offset in range(5, 0, -1):
        await schedules.create(
            user.id,
            name=f"job {offset}",
            interval_seconds=60,
            prompt="x",
            next_run_at=now - timedelta(minutes=offset),
        )
    await schedules.create(
        user.id, name="later", interval_seconds=60, prompt="x", next_run_at=now + timedelta(hours=1)
    )

    batch = await schedules.due(now, limit=3)
    assert [s.name for s in batch] == ["job 5", "job 4", "job 3"]
    assert len(await schedules.due(now)) == 5  # the future job is not due


async def _recurring(schedules, stores, email, **fields):
    user = await stores["users"].get_or_create_by_email(email)
    job = await schedules.create(
        user.id,
        name="job",
        prompt="x",
        next_run_at=datetime.now(timezone.utc),
        **fields,
    )
    return user, job


async def test_a_run_queued_before_a_pause_does_not_happen(schedules, stores):
    """The row can be edited between the tick that queues a job and the moment
    it runs. Acting on the copy from then both ignored the pause and, via
    mark_ran, put the schedule back to where it was."""
    user, job = await _recurring(schedules, stores, "paused@sbot.test", cron="0 9 * * *")
    await schedules.update(user.id, job.id, enabled=False)
    ran: list[str] = []

    async def handler(user_id, session_id, prompt):
        ran.append(session_id)
        return "done"

    await _service(schedules, stores, handler)._fire(job)  # the stale pre-pause copy

    row = await schedules.get(job.id)
    assert ran == []
    assert row.enabled is False and not row.last_status


async def test_a_retime_during_a_run_survives_it(schedules, stores):
    """Rescheduling has to read the row back, or an edit made while the turn was
    in flight is overwritten the moment it finishes."""
    user, job = await _recurring(schedules, stores, "retimed@sbot.test", interval_seconds=86400)

    async def handler(user_id, session_id, prompt):
        await schedules.update(user.id, job.id, interval_seconds=60)
        return "done"

    await _service(schedules, stores, handler)._fire(job)

    row = await schedules.get(job.id)
    assert row.interval_seconds == 60
    assert _utc(row.next_run_at) < datetime.now(timezone.utc) + timedelta(minutes=5)


async def test_rescheduling_a_one_shot_during_its_run_preserves_the_new_deadline(
    schedules, stores
):
    """A completed one-shot normally clears its deadline and disables itself.
    That settlement must not erase a new deadline saved while the old run was
    still in flight."""
    user, job = await _recurring(schedules, stores, "once-retimed@sbot.test")
    future = datetime.now(timezone.utc) + timedelta(hours=2)

    async def handler(user_id, session_id, prompt):
        await schedules.update(user.id, job.id, next_run_at=future, enabled=True)
        return "done"

    await _service(schedules, stores, handler)._fire(job)

    row = await schedules.get(job.id)
    assert row.last_status == "ok"
    assert row.enabled is True
    assert _utc(row.next_run_at) == future


async def test_the_job_is_released_only_once_it_is_rescheduled(schedules, stores):
    """Dropping it from `_firing` any earlier leaves a window where a CRUD-woken
    tick sees it neither running nor yet rescheduled, and starts it twice."""
    user, job = await _recurring(schedules, stores, "twice@sbot.test", interval_seconds=3600)

    async def handler(user_id, session_id, prompt):
        return "done"

    service = _service(schedules, stores, handler)
    still_held: list[bool] = []
    original = schedules.settle_run

    async def spy(schedule_id, **kwargs):
        still_held.append(schedule_id in service._firing)
        return await original(schedule_id, **kwargs)

    schedules.settle_run = spy
    service._firing.add(job.id)
    await service._fire(job)

    assert still_held == [True]
    assert job.id not in service._firing


async def test_the_loop_starts_no_more_runs_than_the_limit(schedules, stores):
    """The ceiling has to bite before the task exists. Gating inside the run
    only parked the overflow, each copy holding a stale row for up to an hour."""
    user = await stores["users"].get_or_create_by_email("burst@sbot.test")
    now = datetime.now(timezone.utc)
    for i in range(_MAX_CONCURRENT_FIRES + 4):
        await schedules.create(
            user.id,
            name=f"job {i}",
            interval_seconds=60,
            prompt="x",
            next_run_at=now - timedelta(minutes=i + 1),
        )
    gate = asyncio.Event()

    async def handler(user_id, session_id, prompt):
        await gate.wait()
        return "done"

    service = _service(schedules, stores, handler)
    service.start()
    try:
        for _ in range(200):
            if len(service._firing) >= _MAX_CONCURRENT_FIRES:
                break
            await asyncio.sleep(0.01)
        assert len(service._firing) == _MAX_CONCURRENT_FIRES
    finally:
        gate.set()
        # stop() also reaps the runs it started — left behind, they outlive the
        # test's event loop and its database connections with them.
        await service.stop()
        assert not service._fires


async def test_a_run_stands_aside_for_a_live_conversation(schedules, stores):
    """A run lands in the bot's own chat, and a turn there takes one lock. Going
    ahead while the user is talking would hang their reply for the whole run."""
    user = await stores["users"].get_or_create_by_email("busy@sbot.test")
    bot = await stores["bots"].create(owner_id=user.id, name="Analyst")
    thread = await stores["sessions"].thread_for_bot(user.id, bot.id, title=bot.name)
    job = await schedules.create(
        user.id,
        name="brief",
        interval_seconds=3600,
        prompt="x",
        bot_id=bot.id,
        next_run_at=datetime.now(timezone.utc),
    )
    ran: list[str] = []

    async def handler(user_id, session_id, prompt):
        ran.append(session_id)
        return "done"

    service = _service(schedules, stores, handler, busy=lambda: {thread.id})
    await service._fire(job)
    assert ran == []

    # The grace period is wall-clock, not a pass count: schedule CRUD wakes the
    # loop immediately, so a count could be spent in seconds by an unrelated
    # edit. Rewind the clock this job has been waiting on instead of sleeping.
    service._defers[job.id] -= _MAX_DEFER_SECONDS
    await service._fire(job)
    assert ran == [thread.id]
    # ...and the budget is handed back, or a task in a chat nobody leaves would
    # never stand aside again.
    assert job.id not in service._defers


async def test_a_failure_before_the_turn_is_still_recorded(schedules, stores):
    """Resolving the target chat is I/O too. A failure there that returned
    without marking the job left next_run_at in the past, so the loop restarted
    it on every tick and the owner never saw why."""
    user, job = await _recurring(schedules, stores, "broken@sbot.test", interval_seconds=3600)
    ran: list[str] = []

    async def handler(user_id, session_id, prompt):
        ran.append(session_id)
        return "done"

    async def boom(*args, **kwargs):
        raise RuntimeError("database is away")

    sessions = stores["sessions"]
    original = sessions.create
    sessions.create = boom
    try:
        await _service(schedules, stores, handler)._fire(job)
    finally:
        sessions.create = original

    row = await schedules.get(job.id)
    assert ran == []
    assert row.last_status.startswith("error")
    assert _utc(row.next_run_at) > datetime.now(timezone.utc)


async def test_the_fallback_chat_is_reused_between_runs(schedules, stores):
    """A task with no bot and no chat used to get a brand-new one every single
    run — ~144 orphan sessions a day on a ten-minute job, and nothing collects
    them."""
    user, job = await _recurring(schedules, stores, "reuse@sbot.test", interval_seconds=3600)
    seen: list[str] = []

    async def handler(user_id, session_id, prompt):
        seen.append(session_id)
        return "done"

    service = _service(schedules, stores, handler)
    await service._fire(job)
    await schedules.update(user.id, job.id, next_run_at=datetime.now(timezone.utc))
    await service._fire(await schedules.get(job.id))

    assert len(seen) == 2 and seen[0] == seen[1]
    assert (await schedules.get(job.id)).session_id == seen[0]


async def test_a_task_does_not_run_in_another_bots_chat(schedules, stores):
    """The runtime picks the acting bot from the session, so a task pointed at
    another bot's thread would quietly run with that bot's charter and tools
    while still being displayed as its own."""
    user = await stores["users"].get_or_create_by_email("mixed@sbot.test")
    mine = await stores["bots"].create(owner_id=user.id, name="Analyst")
    other = await stores["bots"].create(owner_id=user.id, name="Lead")
    theirs = await stores["sessions"].thread_for_bot(user.id, other.id, title=other.name)
    job = await schedules.create(
        user.id,
        name="brief",
        interval_seconds=3600,
        prompt="x",
        bot_id=mine.id,
        session_id=theirs.id,
        next_run_at=datetime.now(timezone.utc),
    )
    seen: list[str] = []

    async def handler(user_id, session_id, prompt):
        seen.append(session_id)
        return "done"

    await _service(schedules, stores, handler)._fire(job)

    own = await stores["sessions"].thread_for_bot(user.id, mine.id, title=mine.name)
    assert seen == [own.id]


async def test_a_group_task_keeps_delivering_to_the_group(schedules, stores):
    """`create` upserts by name, and that path never wrote the chat — so a task
    set up from a group chat went back to the leader's private thread, out of
    sight of everyone else in the group."""
    user = await stores["users"].get_or_create_by_email("group@sbot.test")
    lead = await stores["bots"].create(owner_id=user.id, name="Lead")
    group_chat = await stores["sessions"].create(user.id, title="group")

    tool = ScheduleTool(schedules, None, user.id, bot_id=lead.id, session_id=group_chat.id)
    await tool.execute("create", name="standup", prompt="go", cron="0 9 * * *")
    row = (await schedules.list_for_user(user.id))[0]
    assert row.session_id == group_chat.id

    await schedules.update(user.id, row.id, session_id=None)
    await tool.execute("create", name="standup", prompt="go", cron="0 7 * * *")
    assert (await schedules.get(row.id)).session_id == group_chat.id


async def test_the_chief_of_staff_does_not_take_over_a_teammates_task(schedules, stores):
    """`create` upserts by name and the Chief of Staff sees every task, so a
    name it picked independently used to rewrite a specialist's in place."""
    user = await stores["users"].get_or_create_by_email("lead@sbot.test")
    cos = await stores["bots"].create(owner_id=user.id, name="Lead", kind="chief_of_staff")
    specialist = await stores["bots"].create(owner_id=user.id, name="Analyst")
    theirs = await schedules.create(
        user.id,
        name="daily report",
        cron="0 9 * * *",
        prompt="theirs",
        bot_id=specialist.id,
        next_run_at=datetime.now(timezone.utc),
    )

    tool = ScheduleTool(schedules, None, user.id, bot_id=cos.id, is_cos=True)
    await tool.execute("create", name="daily report", prompt="mine", cron="0 7 * * *")

    unchanged = await schedules.get(theirs.id)
    assert unchanged.prompt == "theirs" and unchanged.cron == "0 9 * * *"
    rows = await schedules.list_for_user(user.id)
    assert {r.bot_id for r in rows} == {specialist.id, cos.id}
    # Both are named the same; each bot still resolves to its own by name.
    resolved, err = await tool._resolve({"name": "daily report"})
    assert err is None and resolved.bot_id == cos.id


async def test_a_specialist_is_told_about_tasks_it_cannot_manage(schedules, stores):
    """Tasks made in Settings carry no bot, so a specialist cannot see them —
    but they keep arriving, and "you have no scheduled tasks" is a dead end."""
    user = await stores["users"].get_or_create_by_email("scoped@sbot.test")
    specialist = await stores["bots"].create(owner_id=user.id, name="Analyst")
    await schedules.create(
        user.id,
        name="owner task",
        interval_seconds=3600,
        prompt="x",
        next_run_at=datetime.now(timezone.utc),
    )

    tool = ScheduleTool(schedules, None, user.id, bot_id=specialist.id, is_cos=False)
    listed = await tool.execute("list")
    assert "no scheduled tasks" in listed and "team lead" in listed
    _, err = await tool._resolve({"name": "owner task"})
    assert "team lead" in err


@pytest.fixture
async def client(schedules, stores):
    user = await stores["users"].get_or_create_by_email("api@sbot.test")
    state = SimpleNamespace(
        schedules=schedules,
        sessions=stores["sessions"],
        bots=stores["bots"],
        settings=SimpleNamespace(scheduler=SimpleNamespace(timezone="Asia/Bangkok")),
        scheduler=SimpleNamespace(notify_changed=lambda: None),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_state] = lambda: state
    app.dependency_overrides[current_user] = lambda: user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http, user


async def test_editing_a_one_shot_keeps_its_deadline(client):
    """Renaming or re-enabling a one-shot used to 422 (the UI has no `run_at`
    field to echo back) and any accepted edit pulled it to "now"."""
    http, _ = client
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    created = await http.post(
        "/api/schedules", json={"name": "one shot", "prompt": "go", "run_at": when.isoformat()}
    )
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["next_run_at"].startswith(when.isoformat()[:16])

    edited = await http.put(
        f"/api/schedules/{row['id']}",
        json={"name": "one shot", "prompt": "go", "enabled": False},
    )
    assert edited.status_code == 200, edited.text
    # Compared to the second: sqlite hands back a naive datetime where Postgres
    # keeps the offset, and the point here is the instant, not the encoding.
    assert edited.json()["next_run_at"][:19] == row["next_run_at"][:19]
    assert edited.json()["enabled"] is False


async def test_a_partial_edit_leaves_a_recurring_task_recurring(client):
    """`cron` and `interval_seconds` both have defaults, so a PUT that only
    flips `enabled` used to blank them — making a one-shot that the very next
    run disabled for good."""
    http, _ = client
    row = (
        await http.post("/api/schedules", json={"name": "daily", "prompt": "go", "cron": "0 9 * * *"})
    ).json()

    edited = await http.put(
        f"/api/schedules/{row['id']}", json={"name": "daily", "prompt": "go", "enabled": False}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["cron"] == "0 9 * * *"

    # An empty cron sent on purpose still clears it.
    cleared = await http.put(
        f"/api/schedules/{row['id']}",
        json={"name": "daily", "prompt": "go", "cron": "", "interval_seconds": 0},
    )
    assert cleared.json()["cron"] == ""


async def test_a_schedule_stays_editable_when_its_targets_go_away(client, stores):
    """A bot gets archived, a chat gets deleted — and the UI echoes both ids back
    on every save, including a plain on/off toggle. Re-checking pointers the edit
    never changed made those schedules impossible to touch again."""
    http, user = client
    bot = await stores["bots"].create(owner_id=user.id, name="Retired")
    chat = await stores["sessions"].create(user.id, title="temporary")
    body = {
        "name": "task",
        "prompt": "go",
        "interval_seconds": 600,
        "bot_id": bot.id,
        "session_id": chat.id,
    }
    row = (await http.post("/api/schedules", json=body)).json()

    await stores["bots"].archive(bot.id, user.id)
    await stores["sessions"].delete(chat.id)

    toggled = await http.put(f"/api/schedules/{row['id']}", json={**body, "enabled": False})
    assert toggled.status_code == 200, toggled.text
    # ...and the chat that no longer exists can be detached.
    detached = await http.put(f"/api/schedules/{row['id']}", json={**body, "session_id": None})
    assert detached.json()["session_id"] is None


async def test_schedule_targets_are_owner_checked_and_bot_is_sticky(client, stores):
    http, user = client
    intruder = await stores["users"].get_or_create_by_email("intruder@sbot.test")
    foreign_bot = await stores["bots"].create(owner_id=intruder.id, name="Theirs")
    foreign_session = await stores["sessions"].create(intruder.id, title="theirs")
    mine = await stores["bots"].create(owner_id=user.id, name="Mine")
    body = {"name": "task", "prompt": "go", "interval_seconds": 600}

    for bad in [{"bot_id": foreign_bot.id}, {"session_id": foreign_session.id}]:
        assert (await http.post("/api/schedules", json={**body, **bad})).status_code == 404

    row = (await http.post("/api/schedules", json={**body, "bot_id": mine.id})).json()
    assert row["bot_id"] == mine.id
    # A client that doesn't know about bots must not hand the task back to the
    # Chief of Staff just by saving the fields it does know.
    assert (await http.put(f"/api/schedules/{row['id']}", json=body)).json()["bot_id"] == mine.id
    # ...but an explicit null does reassign it.
    unassigned = await http.put(f"/api/schedules/{row['id']}", json={**body, "bot_id": None})
    assert unassigned.json()["bot_id"] is None


async def test_an_edit_that_never_mentions_enabled_leaves_it_paused(client):
    """`enabled` defaults to True, so a PUT that only renames a task used to
    resume one the owner had deliberately paused — and hand it a live deadline
    on the way out."""
    http, _ = client
    body = {"name": "daily", "prompt": "go", "cron": "0 9 * * *"}
    row = (await http.post("/api/schedules", json=body)).json()
    await http.put(f"/api/schedules/{row['id']}", json={**body, "enabled": False})

    renamed = await http.put(f"/api/schedules/{row['id']}", json={**body, "name": "renamed"})
    assert renamed.json()["enabled"] is False
    # ...and saying so explicitly still resumes it.
    resumed = await http.put(f"/api/schedules/{row['id']}", json={**body, "enabled": True})
    assert resumed.json()["enabled"] is True


async def test_an_edit_that_keeps_the_timing_keeps_the_deadline(client):
    """Recomputing from "now" on every save skipped the occurrence a daily task
    was seconds away from, and — since the UI echoes every field back when you
    flip the on/off switch — pushed an hourly task a fresh hour out each time."""
    http, _ = client
    body = {"name": "hourly", "prompt": "go", "interval_seconds": 3600}
    row = (await http.post("/api/schedules", json=body)).json()

    untouched = await http.put(f"/api/schedules/{row['id']}", json={**body, "name": "renamed"})
    assert untouched.json()["next_run_at"][:19] == row["next_run_at"][:19]
    # A real retime does move it.
    retimed = await http.put(f"/api/schedules/{row['id']}", json={**body, "interval_seconds": 60})
    assert retimed.json()["next_run_at"][:19] != row["next_run_at"][:19]


async def test_an_unparseable_cron_is_rejected(client):
    """compute_next_run is the only validator there is, and `run_at` skipped
    past it — so a bad cron was stored with a 200 and then left the task enabled
    but permanently dormant."""
    http, _ = client
    when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    bad = await http.post(
        "/api/schedules", json={"name": "x", "prompt": "go", "cron": "every day", "run_at": when}
    )
    assert bad.status_code == 422

    blank = await http.post(
        "/api/schedules", json={"name": "x", "prompt": "go", "cron": "   ", "run_at": when}
    )
    assert blank.status_code == 200 and blank.json()["cron"] == ""


async def test_a_finished_one_shot_is_not_rearmed_by_a_cosmetic_edit(client, schedules):
    """A one-shot that has run has no deadline left. Handing it "now" on an
    unrelated save put a bogus next run on a task the owner considered done."""
    http, _ = client
    when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    row = (
        await http.post("/api/schedules", json={"name": "once", "prompt": "go", "run_at": when})
    ).json()
    await schedules.mark_ran(row["id"], next_run_at=None, status="ok")

    edited = await http.put(f"/api/schedules/{row['id']}", json={"name": "renamed", "prompt": "go"})
    assert edited.json()["next_run_at"] is None and edited.json()["enabled"] is False
    # ...but switching it back on deliberately does give it a fresh one.
    resumed = await http.put(
        f"/api/schedules/{row['id']}", json={"name": "renamed", "prompt": "go", "enabled": True}
    )
    assert resumed.json()["next_run_at"] is not None
