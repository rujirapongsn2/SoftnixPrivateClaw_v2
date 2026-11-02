"""Every scheduled run opens its own chat, and only the newest run chats per
schedule are kept, so a recurring task never grows one endless history."""

import importlib.util
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from claw.core.scheduler import SchedulerService
from claw.db.models import Base, ChatSession
from claw.db.stores import ScheduleStore


def _due():
    return datetime.now(timezone.utc) - timedelta(seconds=1)


async def _setup(db_factory, stores, *, keep=7, name="cyber news"):
    schedules = ScheduleStore(db_factory)
    user = await stores["users"].get_or_create_by_email(f"{name.replace(' ', '')}@x.y")
    seen: list[str] = []

    async def handler(user_id: str, session_id: str, prompt: str) -> str:
        seen.append(session_id)
        await stores["messages"].append(session_id, [{"role": "user", "content": prompt}])
        return "ok"

    service = SchedulerService(schedules, stores["sessions"], handler, keep_run_chats=keep)
    job = await schedules.create(user.id, name=name, prompt="summarise", interval_seconds=3600, next_run_at=_due())
    return schedules, service, user, job, seen


async def _run(schedules, service, user, job, times):
    for _ in range(times):
        await schedules.update(user.id, job.id, next_run_at=_due())
        await service._fire(await schedules.get(job.id))


async def _run_chats(stores, user):
    return [s for s in await stores["sessions"].list_for_user(user.id) if s.channel == "schedule"]


async def test_each_run_opens_its_own_chat(db_factory, stores):
    schedules, service, user, job, seen = await _setup(db_factory, stores)
    await _run(schedules, service, user, job, 3)

    assert len(seen) == 3 and len(set(seen)) == 3
    chats = await _run_chats(stores, user)
    assert {c.id for c in chats} == set(seen)
    assert all(c.schedule_id == job.id and c.title.startswith("cyber news (") for c in chats)
    assert (await schedules.get(job.id)).session_id is None
    for sid in seen:
        assert len(await stores["messages"].recent(sid)) == 1


async def test_only_the_newest_run_chats_are_kept(db_factory, stores):
    schedules, service, user, job, seen = await _setup(db_factory, stores, keep=2)
    await _run(schedules, service, user, job, 4)

    chats = await _run_chats(stores, user)
    assert {c.id for c in chats} == set(seen[-2:])
    for gone in seen[:2]:
        assert await stores["sessions"].get(gone) is None
        assert await stores["messages"].recent(gone) == []


async def test_pruning_spares_pinned_chats_other_schedules_and_other_chats(db_factory, stores):
    schedules, service, user, job, seen = await _setup(db_factory, stores, keep=1)
    other = await schedules.create(user.id, name="other", prompt="x", interval_seconds=3600, next_run_at=_due())
    await service._fire(await schedules.get(other.id))
    web = await stores["sessions"].create(user.id, title="my chat")

    await _run(schedules, service, user, job, 1)
    async with db_factory() as db:
        (await db.get(ChatSession, seen[-1])).pinned = True
        await db.commit()
    await _run(schedules, service, user, job, 2)

    remaining = {s.id for s in await stores["sessions"].list_for_user(user.id)}
    assert seen[1] in remaining  # pinned run chat of this schedule
    assert seen[0] in remaining  # the other schedule's only run chat
    assert web.id in remaining
    assert seen[2] not in remaining and seen[3] in remaining


async def test_zero_keeps_every_run(db_factory, stores):
    schedules, service, user, job, seen = await _setup(db_factory, stores, keep=0)
    await _run(schedules, service, user, job, 3)
    assert len(await _run_chats(stores, user)) == 3


async def test_a_chat_the_user_is_still_using_is_kept(db_factory, stores):
    schedules, service, user, job, seen = await _setup(db_factory, stores, keep=2)
    await _run(schedules, service, user, job, 2)
    # The user asks a follow-up in the older run chat.
    await stores["messages"].append(seen[0], [{"role": "user", "content": "and then?"}])
    async with db_factory() as db:
        (await db.get(ChatSession, seen[0])).updated_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        await db.commit()

    await _run(schedules, service, user, job, 1)

    remaining = {c.id for c in await _run_chats(stores, user)}
    assert remaining == {seen[0], seen[2]}


async def test_a_chat_with_a_turn_in_flight_is_kept(db_factory, stores):
    schedules, service, user, job, seen = await _setup(db_factory, stores, keep=1)
    await _run(schedules, service, user, job, 1)
    service.busy_sessions = lambda: {seen[0]}

    await _run(schedules, service, user, job, 1)

    assert {c.id for c in await _run_chats(stores, user)} == set(seen)


async def test_a_failed_run_does_not_push_out_earlier_reports(db_factory, stores):
    schedules, service, user, job, seen = await _setup(db_factory, stores, keep=1)
    await _run(schedules, service, user, job, 1)
    good = seen[0]

    async def down(user_id: str, session_id: str, prompt: str) -> str:
        seen.append(session_id)
        raise RuntimeError("provider down")

    service.handler = down
    await _run(schedules, service, user, job, 2)

    assert (await schedules.get(job.id)).last_status.startswith("error:")
    assert await stores["sessions"].get(good) is not None


async def test_a_chat_the_user_chose_is_still_the_target(db_factory, stores):
    schedules, service, user, job, seen = await _setup(db_factory, stores)
    chosen = await stores["sessions"].create(user.id, title="my chat")
    await schedules.update(user.id, job.id, session_id=chosen.id)

    await _run(schedules, service, user, job, 2)

    assert seen == [chosen.id, chosen.id]
    assert await _run_chats(stores, user) == []


async def test_a_failed_prune_does_not_fail_the_run(db_factory, stores, monkeypatch):
    schedules, service, user, job, seen = await _setup(db_factory, stores, keep=1)

    async def broken(*args, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(stores["sessions"], "prune_schedule_runs", broken)
    await _run(schedules, service, user, job, 1)

    assert len(seen) == 1 and (await schedules.get(job.id)).last_status == "ok"


def test_migration_adds_the_column_and_clears_legacy_reuse_pointers(tmp_path):
    spec = importlib.util.spec_from_file_location("m", "migrations/versions/e3b7c9d1f4a2_session_schedule_id.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(f"sqlite:///{tmp_path}/m.db")
    legacy = sa.MetaData()
    sa.Table("users", legacy, sa.Column("id", sa.String(32), primary_key=True))
    sa.Table(
        "schedules", legacy,
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("session_id", sa.String(32)),
    )
    sessions = ChatSession.__table__.to_metadata(legacy)
    sessions._columns.remove(sessions.c.schedule_id)
    for index in list(sessions.indexes):
        if "schedule_id" in index.columns:
            sessions.indexes.remove(index)
    legacy.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id) VALUES ('u')"))
        conn.execute(
            sa.text(
                "INSERT INTO sessions (id, user_id, title, channel, created_at, updated_at, "
                "last_consolidated_seq, consolidation_failures, pinned) VALUES "
                "('reused', 'u', 'news', 'schedule', '2026-01-01', '2026-01-01', 0, 0, 0), "
                "('chosen', 'u', 'mine', 'web', '2026-01-01', '2026-01-01', 0, 0, 0)"
            )
        )
        conn.execute(
            sa.text("INSERT INTO schedules (id, session_id) VALUES ('a', 'reused'), ('b', 'chosen'), ('c', NULL)")
        )

    def run(step):
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                step()

    run(migration.upgrade)
    inspector = sa.inspect(engine)
    assert "schedule_id" in {c["name"] for c in inspector.get_columns("sessions")}
    expected = {i.name for i in Base.metadata.tables["sessions"].indexes}
    assert expected <= {i["name"] for i in inspector.get_indexes("sessions")}
    with engine.connect() as conn:
        pointers = dict(conn.execute(sa.text("SELECT id, session_id FROM schedules")).all())
        kept = {r[0] for r in conn.execute(sa.text("SELECT id FROM sessions"))}
    assert pointers == {"a": None, "b": "chosen", "c": None}
    assert kept == {"reused", "chosen"}
    run(migration.downgrade)
    assert "schedule_id" not in {c["name"] for c in sa.inspect(engine).get_columns("sessions")}
