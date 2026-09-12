"""A background job must close out the plan steps it took on.

Background work outlives the turn that submitted it, and until now the working
plan was only ever written during a live turn. So a job that finished minutes
later left the Execution panel reading "Incomplete 2/4" directly above the
message announcing its result — the user's own report of the bug.
"""

from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from sbot.api.routes import list_messages
from sbot.core.bus import EventBus
from tests.sbot_mode.conftest import FakeProvider
from tests.sbot_mode.test_missions import _two_specialists, make_service

_PLAN = [
    {"step": "รวบรวมข้อมูล", "status": "done"},
    {"step": "ร่าง TOR", "status": "in_progress"},
    {"step": "ตรวจทาน", "status": "pending"},
]


async def _submitted(stores, tmp_path, email, plan=None, status="completed"):
    """A session with a plan, and a job that claimed its outstanding steps."""
    user, bot, _ = await _two_specialists(stores, email)
    session = await stores["sessions"].create(user.id)
    await stores["sessions"].set_plan(session.id, "จัดทำ TOR", [dict(s) for s in (plan or _PLAN)])
    service = make_service(stores, FakeProvider([]), tmp_path)
    service.sessions, service.messages, service.bus = stores["sessions"], stores["messages"], EventBus()
    mission = await service.plan(user.id, "ร่าง TOR", [{"id": "n", "bot_id": bot.id}], session_id=session.id)
    await stores["missions"].update_mission(
        mission.id, status=status, plan_link=await service._plan_link(session.id)
    )
    return user, session, service, await stores["missions"].get_mission(mission.id, user.id)


async def _steps(stores, session_id):
    plan, _ = await stores["sessions"].plan_snapshot(session_id)
    return plan["steps"]


async def test_a_job_finishing_after_its_turn_completes_the_steps_it_claimed(stores, tmp_path):
    _, session, service, mission = await _submitted(stores, tmp_path, "settle@sbot.ai")
    async with service.bus.subscribe(session.id) as queue:
        await service._report(mission, "completed")
        events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [s["status"] for s in await _steps(stores, session.id)] == ["done", "done", "pending"]
    plan_events = [e for e in events if e.type == "plan_updated"]
    assert len(plan_events) == 1 and plan_events[0].steps[1]["status"] == "done"


async def test_a_job_takes_no_credit_for_steps_the_agent_never_started(stores, tmp_path):
    # The plan and the job's node graph share no identifiers, so which steps a
    # job covers is guesswork. Guessing wide and writing "done" onto untouched
    # work would have the panel credit work nobody did — worse than the stale
    # plan this whole mechanism exists to fix. Only `in_progress` is claimed.
    _, _, _, mission = await _submitted(stores, tmp_path, "overclaim@sbot.ai")
    assert [s["index"] for s in mission.plan_link["steps"]] == [1]


async def test_a_failed_job_leaves_its_steps_visibly_unfinished_with_a_reason(stores, tmp_path):
    _, session, service, mission = await _submitted(stores, tmp_path, "failed@sbot.ai", status="failed")
    await service._report(mission, "failed")
    steps = await _steps(stores, session.id)
    # Not "pending": the panel draws that exactly like work nobody has started,
    # which is the one thing a failed job must not look like.
    assert [s["status"] for s in steps] == ["done", "blocked", "pending"]
    assert steps[1]["reason"]


async def test_a_paused_job_settles_rather_than_contradicting_its_own_report(stores, tmp_path):
    # `paused` means the budget ran out. It is reportable, so the user gets a
    # message either way; without a settlement the panel sits above that message
    # still reading "in progress" — the originally reported bug.
    _, session, service, mission = await _submitted(stores, tmp_path, "paused@sbot.ai", status="paused")
    await service._report(mission, "paused")
    steps = await _steps(stores, session.id)
    assert [s["status"] for s in steps] == ["done", "blocked", "pending"]
    assert steps[1]["reason"]


async def test_a_cancelled_job_settles_even_though_it_sends_no_report(stores, tmp_path):
    # `cancelled` is deliberately absent from _REPORTED_STATUSES — the user did
    # the cancelling and does not need telling. The plan still has to move, or
    # it stays "in progress" for a job that will never run again.
    _, session, service, mission = await _submitted(stores, tmp_path, "cancel@sbot.ai", status="cancelled")
    await service._report(mission, "cancelled")
    assert [s["status"] for s in await _steps(stores, session.id)] == ["done", "blocked", "pending"]
    assert await stores["messages"].recent(session.id) == []


async def test_cancelling_a_queued_job_settles_it_though_no_worker_ever_ran(stores, tmp_path):
    # Cancel before pickup returns without ever entering `_run`, so the report
    # path never fires — and `cancelled` is not reportable, so the minutely
    # reconciliation sweep will not pick it up either. Nothing else is coming.
    user, session, service, mission = await _submitted(stores, tmp_path, "qcancel@sbot.ai", status="queued")
    assert await service.cancel(mission.id, user.id)
    assert [s["status"] for s in await _steps(stores, session.id)] == ["done", "blocked", "pending"]


async def test_settlement_is_refused_once_the_agent_has_written_a_newer_plan(stores, tmp_path):
    _, session, service, mission = await _submitted(stores, tmp_path, "revised@sbot.ai")
    # The user asked for something else while the job ran, so step 1 is now a
    # different piece of work that this job never did.
    await stores["sessions"].set_plan(
        session.id, "งานใหม่", [{"step": "เริ่มงานใหม่", "status": "pending"}]
    )
    await service._report(mission, "completed")
    assert await _steps(stores, session.id) == [{"step": "เริ่มงานใหม่", "status": "pending"}]


async def test_a_plan_authored_mid_settlement_is_refused_not_overwritten(stores, tmp_path, monkeypatch):
    # The revision is read several awaits before the write, and the write is a
    # whole-column overwrite. Without the revision in the UPDATE's WHERE clause
    # a plan the agent authored inside that window is silently thrown away.
    _, session, service, mission = await _submitted(stores, tmp_path, "race@sbot.ai")
    fresh = [{"step": "เริ่มงานใหม่", "status": "pending"}]
    original = AsyncSession.execute

    async def racing(self, statement, *args, **kwargs):
        monkeypatch.undo()
        await stores["sessions"].set_plan(session.id, "งานใหม่", fresh)
        return await original(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", racing)
    assert await stores["sessions"].settle_plan_steps(
        session.id, mission.plan_link, "completed"
    ) is None
    assert await _steps(stores, session.id) == fresh


async def test_the_agent_rewriting_its_plan_keeps_the_reason_a_job_left_behind(stores, tmp_path):
    # The agent is told to re-send its complete plan every call, and it cannot
    # author a `reason` — only a settling job writes one. A plain full replace
    # therefore erases why a step is blocked, and the model, seeing work it
    # believes it delegated blocked for no stated cause, delegates it again.
    _, session, service, mission = await _submitted(stores, tmp_path, "reason@sbot.ai", status="failed")
    await service._report(mission, "failed")
    settled = await _steps(stores, session.id)
    verbatim = [{"step": s["step"], "status": s["status"]} for s in settled]
    await stores["sessions"].set_plan(session.id, "จัดทำ TOR", verbatim)
    assert (await _steps(stores, session.id))[1]["reason"] == "Background work failed."
    # Moving the step on drops it: the note is about a state it has left.
    verbatim[1]["status"] = "in_progress"
    await stores["sessions"].set_plan(session.id, "จัดทำ TOR", verbatim)
    assert "reason" not in (await _steps(stores, session.id))[1]


async def test_a_step_whose_text_changed_under_the_same_revision_is_left_alone(stores, tmp_path):
    _, session, service, mission = await _submitted(stores, tmp_path, "moved@sbot.ai")
    plan, revision = await stores["sessions"].plan_snapshot(session.id)
    # Same revision on the wire, different step at the claimed index: the index
    # alone is not an identity, which is why the text is snapshotted with it.
    link = dict(mission.plan_link)
    link["steps"] = [{"index": 1, "text": "งานคนละอย่าง"}, {"index": 2, "text": "ตรวจทาน"}]
    await stores["missions"].update_mission(mission.id, plan_link=link)
    assert link["revision"] == revision
    mission = await stores["missions"].get_mission_unchecked(mission.id)
    await service._report(mission, "completed")
    assert [s["status"] for s in await _steps(stores, session.id)] == ["done", "in_progress", "done"]


async def test_reconciliation_replaying_a_settled_job_neither_rewrites_nor_re_announces(stores, tmp_path):
    _, session, service, mission = await _submitted(stores, tmp_path, "replay@sbot.ai")
    await service.reconcile_reports()
    before = await _steps(stores, session.id)
    async with service.bus.subscribe(session.id) as queue:
        await service.reconcile_reports()
        await service.reconcile_reports()
        assert queue.empty()
    assert await _steps(stores, session.id) == before


async def test_accepting_a_job_marks_nothing_done(stores, tmp_path):
    _, session, service, _ = await _submitted(stores, tmp_path, "queued@sbot.ai", status="queued")
    # A receipt is not a result. The claim is recorded, the plan is untouched.
    assert [s["status"] for s in await _steps(stores, session.id)] == ["done", "in_progress", "pending"]


async def test_a_plan_with_nothing_outstanding_is_not_claimed(stores, tmp_path):
    finished = [{"step": "เสร็จแล้ว", "status": "done"}, {"step": "รอผู้ใช้", "status": "waiting_for_user"}]
    _, session, service, mission = await _submitted(stores, tmp_path, "nothing@sbot.ai", plan=finished)
    assert mission.plan_link is None
    await service._report(mission, "completed")
    # In particular, a step waiting on the user is not this job's to complete.
    assert [s["status"] for s in await _steps(stores, session.id)] == ["done", "waiting_for_user"]


async def test_the_transcript_endpoint_carries_the_plan_so_a_reload_restores_the_panel(stores, tmp_path):
    user, session, service, mission = await _submitted(stores, tmp_path, "reload@sbot.ai")
    await service._report(mission, "completed")
    state = SimpleNamespace(sessions=stores["sessions"], messages=stores["messages"])
    first = await list_messages(session.id, before_seq=None, limit=100, user=user, state=state)
    assert [s["status"] for s in first["plan"]["steps"]] == ["done", "done", "pending"]
    # Session state, not transcript: paging back does not resend it.
    older = await list_messages(session.id, before_seq=1, limit=100, user=user, state=state)
    assert older["plan"] is None
