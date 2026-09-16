"""Mission observation is durable UI state, never an extra conversation turn."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from sbot.api.routes import mission_activity
from sbot.core.events import TextDeltaEvent, ThinkingDeltaEvent, ToolStarted, ToolFinished
from sbot.core.mission_activity import MissionActivity
from tests.sbot_mode.conftest import FakeProvider, text_turn
from tests.sbot_mode.test_missions import make_service, _two_specialists


async def setup(stores, tmp_path):
    user, bot, leader = await _two_specialists(stores, 'activity@sbot.ai')
    origin = await stores['sessions'].thread_for_bot(user.id, leader.id, title=leader.name)
    provider = FakeProvider([text_turn('Verified result')])
    service = make_service(stores, provider, tmp_path)
    service.sessions = stores['sessions']
    service.messages = stores['messages']
    mission = await service.plan(user.id, 'Research', [dict(id='research', title='Research',
        bot_id=bot.id, instruction='Find primary sources')], session_id=origin.id)
    node = SimpleNamespace(id='research', bot_id=bot.id, attempts=1, title='Research',
        instruction='Find primary sources', budget={}, depends_on=[])
    return user, bot, origin, service, mission, node, provider


async def start_activity(service, mission, node):
    activity = MissionActivity(service, mission, node)
    activity.start()
    await asyncio.wait_for(activity.ready.wait(), 1)
    return activity


@pytest.mark.asyncio
async def test_live_activity_is_durable_separate_and_excluded_from_context(stores, tmp_path):
    user, bot, origin, service, mission, node, _ = await setup(stores, tmp_path)
    activity = await start_activity(service, mission, node)
    try:
        activity.running()
        activity.emit(TextDeltaEvent('turn', 'Checking sources'))
        activity.emit(ThinkingDeltaEvent('turn', 'private reasoning'))
        activity.emit(ToolStarted('turn', 'web_search', 'SECRET'))
        activity.emit(ToolFinished('turn', 'web_search', 'SECRET', False))
        await asyncio.sleep(.65)
        page = await stores['messages'].page_for_display(activity.session_id)
        snapshot = page[0][0]['meta']['mission_activity']
        assert snapshot['status'] == 'running'
        assert snapshot['text'] == 'Checking sources'
        assert snapshot['steps'][0]['status'] == 'done'
        assert 'SECRET' not in str(snapshot) and 'private reasoning' not in str(snapshot)
        assert await stores['messages'].recent(activity.session_id) == []
        assert await stores['messages'].oldest_for_consolidation(activity.session_id,
            after_seq=0, through_seq=100, limit=100) == []
        handoffs = await stores['messages'].mission_activity(origin.id)
        assert handoffs[0]['meta']['mission_handoff']['session_id'] == activity.session_id
        assert 'mission_activity' not in handoffs[0]['meta']
        assert await stores['messages'].recent(origin.id) == []
    finally:
        await activity.close(SimpleNamespace(status='done', output='Verified', artifacts=[]))
    rows = await stores['messages'].mission_activity(activity.session_id)
    assert len(rows) == 1
    assert rows[0]['meta']['mission_activity']['result'] == 'Verified'


@pytest.mark.asyncio
async def test_attempt_identity_and_ownership(stores, tmp_path):
    user, bot, origin, service, mission, node, _ = await setup(stores, tmp_path)
    for attempt in (1, 1, 2):
        node.attempts = attempt
        activity = await start_activity(service, mission, node)
        await activity.close()
    rows = await stores['messages'].mission_activity(activity.session_id)
    assert len(rows) == 2
    assert {r['meta']['mission_activity']['attempt'] for r in rows} == {1, 2}
    outsider = await stores['users'].get_or_create_by_email('outsider@sbot.ai')
    with pytest.raises(HTTPException) as exc:
        await mission_activity(activity.session_id, user=outsider, state=service)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_mission_still_executes_with_no_extra_model_calls(stores, tmp_path):
    user, bot, origin, service, mission, node, provider = await setup(stores, tmp_path)
    assert await service.run_to_completion(mission.id, user.id) == 'completed'
    thread = await stores['sessions'].thread_for_bot(user.id, bot.id, title=bot.name)
    rows = await stores['messages'].mission_activity(thread.id)
    assert len(rows) == 1
    assert rows[0]['meta']['mission_activity']['status'] == 'done'
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_observation_failure_does_not_fail_work(stores, tmp_path, monkeypatch):
    user, bot, origin, service, mission, node, provider = await setup(stores, tmp_path)
    monkeypatch.setattr(service.messages, 'save_mission_activity', AsyncMock(side_effect=RuntimeError('offline')))
    assert await service.run_to_completion(mission.id, user.id) == 'completed'
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_slow_observation_never_delays_mission_work(stores, tmp_path, monkeypatch):
    user, bot, origin, service, mission, node, provider = await setup(stores, tmp_path)
    gate = asyncio.Event()
    original = service.messages.save_mission_activity

    async def delayed(*args, **kwargs):
        await gate.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(service.messages, 'save_mission_activity', delayed)
    assert await asyncio.wait_for(service.run_to_completion(mission.id, user.id), .8) == 'completed'
    assert len(provider.calls) == 1
    gate.set()
    await asyncio.sleep(.05)


@pytest.mark.asyncio
async def test_stale_running_snapshot_reconciles_to_scheduler(stores, tmp_path):
    from sqlalchemy import update
    from sbot.db.models import MissionNode
    user, bot, origin, service, mission, node, _ = await setup(stores, tmp_path)
    activity = await start_activity(service, mission, node)
    await activity.close()
    # Simulate a process disappearing after its last running checkpoint.
    activity.data['status'] = 'running'
    await activity.save()
    async with stores['missions'].factory() as db:
        await db.execute(update(MissionNode).where(MissionNode.mission_id == mission.id)
                         .values(attempts=1, status='done', output='Recovered result'))
        await db.commit()
    rows = await stores['messages'].mission_activity(activity.session_id)
    assert rows[0]['meta']['mission_activity']['status'] == 'done'
    assert rows[0]['meta']['mission_activity']['result'] == 'Recovered result'
    async with stores['missions'].factory() as db:
        await db.execute(update(MissionNode).where(MissionNode.mission_id == mission.id)
                         .values(attempts=2, status='running'))
        await db.commit()
    rows = await stores['messages'].mission_activity(activity.session_id)
    assert rows[0]['meta']['mission_activity']['status'] == 'interrupted'


@pytest.mark.asyncio
async def test_finishing_snapshot_uses_scheduler_terminal_status(stores, tmp_path):
    from sqlalchemy import update
    from sbot.db.models import MissionNode

    user, bot, origin, service, mission, node, _ = await setup(stores, tmp_path)
    activity = await start_activity(service, mission, node)
    await activity.close(SimpleNamespace(status='done', output='Executor said done', artifacts=[]))
    async with stores['missions'].factory() as db:
        await db.execute(update(MissionNode).where(MissionNode.mission_id == mission.id)
                         .values(attempts=1, status='error', output='Commit failed'))
        await db.commit()
    rows = await stores['messages'].mission_activity(activity.session_id)
    snapshot = rows[0]['meta']['mission_activity']
    assert snapshot['status'] == 'error'
    assert snapshot['result'] == 'Commit failed'


@pytest.mark.asyncio
async def test_removed_group_member_receives_no_assignment(stores, tmp_path, monkeypatch):
    user, bot, origin, service, mission, node, _ = await setup(stores, tmp_path)
    monkeypatch.setattr(service, '_current_members', AsyncMock(return_value=frozenset()))
    activity = await start_activity(service, mission, node)
    assert activity.session_id is None
    assert await stores['messages'].mission_activity(origin.id) == []


@pytest.mark.asyncio
async def test_activity_storage_is_bounded_and_cancelled_tools_close(stores, tmp_path):
    user, bot, origin, service, mission, node, _ = await setup(stores, tmp_path)
    activity = await start_activity(service, mission, node)
    activity.emit(TextDeltaEvent('turn', 'x' * 20000))
    for i in range(110):
        activity.emit(ToolStarted('turn', f'tool_{i}', ''))
    await activity.close(SimpleNamespace(status='done', output='r' * 30000, artifacts=[]))
    rows = await stores['messages'].mission_activity(activity.session_id)
    snapshot = rows[0]['meta']['mission_activity']
    assert len(snapshot['text']) == 12000
    assert len(snapshot['steps']) == 100
    assert all(step['status'] == 'interrupted' for step in snapshot['steps'])
    assert len(snapshot['result']) == 20000
    assert snapshot['result_truncated'] is True


@pytest.mark.asyncio
async def test_observations_do_not_change_memory_window(stores):
    user = await stores['users'].get_or_create_by_email('memory-window@sbot.ai')
    session = await stores['sessions'].create(user.id, title='Memory')
    await stores['messages'].append(session.id, [
        {'role': 'user', 'content': 'one'},
        {'role': 'observation', 'content': 'activity', 'meta': {}},
        {'role': 'assistant', 'content': 'two'},
        {'role': 'observation', 'content': 'activity', 'meta': {}},
        {'role': 'user', 'content': 'three'},
    ])
    total, batch = await stores['messages'].consolidation_batch(
        session.id, after_seq=0, keep=1, limit=100
    )
    assert total == 3
    assert [row['content'] for row in batch] == ['one', 'two']
