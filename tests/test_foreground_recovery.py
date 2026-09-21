"""Orphan watcher: real runtime/store, scripted providers, no duplicate execution."""
import asyncio
import json

from claw.jobs.foreground import heartbeat, recoverable
from claw.jobs.provider import ForegroundAccounting
from claw.jobs.recovery import recover_once
from claw.jobs.models import ForegroundJournal, Job
from claw.providers.base import ChatResult, ToolCall
from tests.test_job_runtime_bridge import prepare


async def abandoned(runtime, jobs, user, session, *, pending=None, terminal=False):
    tape = ForegroundAccounting(jobs, user.id, session.id)
    call = await tape.reserve_call('model', 300)
    await tape.record_usage(call, 100, usage={'prompt_tokens': 80, 'completion_tokens': 20})
    args = {'goal': 'Create a CSV file', 'steps': [
        {'step': 'Write output', 'status': 'pending'}, {'step': 'Validate', 'status': 'pending'}]}
    await tape.checkpoint({'request': dict(content='Process the analysis', locale='th', media=[],
        model=None, permission_mode='auto', session_id=session.id), 'pending_tool': pending,
        'foreground_terminal': terminal, 'runtime': {'user_message_persisted': True,
            'checkpoint_messages': [
                {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'plan', 'type': 'function', 'function': {'name': 'update_plan', 'arguments': json.dumps(args)}}]},
                {'role': 'tool', 'name': 'update_plan', 'tool_call_id': 'plan', 'content': 'Plan saved'}],
            'tool_results': {}, 'written': []}})
    # Simulate API process loss: pulse stops, but close is never executed.
    tape.pulse.cancel()
    await asyncio.gather(tape.pulse, return_exceptions=True)
    return tape


async def test_orphan_resumes_automatically_once_with_two_watchers(stores, db_factory, tmp_path):
    runtime, jobs, worker, user, session, provider = await prepare(stores, db_factory, tmp_path, [
        [ChatResult(content=None, tool_calls=[ToolCall('out', 'write_file', {'path': 'result.csv', 'content': 'a,b\n1,2'})])],
        [ChatResult(content='Done')]])
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    await abandoned(runtime, jobs, user, session)
    assert await recover_once(jobs, {'privateclaw': runtime}) == 0
    clock[0] += 61
    await asyncio.gather(*(recover_once(jobs, {'privateclaw': runtime}) for _ in range(2)))
    rows = await jobs.list_jobs(user.id)
    assert len(rows) == 1 and rows[0]['status'] == 'queued'
    async with db_factory() as db:
        assert (await db.get(Job, rows[0]['job_id'])).tokens == 100
    assert await worker.run_once()
    assert (await jobs.snapshot(user.id, rows[0]['job_id']))['status'] == 'completed'
    assert len(provider.calls) == 2
    assert await jobs.publish_deliveries() == 1
    assert await jobs.publish_deliveries() == 0
    assert not await recoverable(jobs, user.id)


async def test_slow_live_provider_heartbeat_prevents_recovery(stores, db_factory, tmp_path):
    runtime, jobs, _, user, session, provider = await prepare(stores, db_factory, tmp_path, [])
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    tape = await abandoned(runtime, jobs, user, session)
    clock[0] += 55
    assert await heartbeat(jobs, tape.journal_id, user.id)
    clock[0] += 20
    assert await recover_once(jobs, {'privateclaw': runtime}) == 0
    assert not provider.calls


async def test_unknown_tool_outcome_becomes_visible_paused_job(stores, db_factory, tmp_path):
    runtime, jobs, worker, user, session, provider = await prepare(stores, db_factory, tmp_path, [])
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    await abandoned(runtime, jobs, user, session, pending={'name': 'send_email'})
    clock[0] += 61
    assert await recover_once(jobs, {'privateclaw': runtime}) == 1
    job = (await jobs.list_jobs(user.id, session.id))[0]
    assert job['status'] == 'paused' and job['reason'] == 'uncertain_effect'
    assert not await worker.run_once()
    assert not await jobs.control(job['job_id'], user.id, 'resume')
    assert not provider.calls
    assert await jobs.control(job['job_id'], user.id, 'cancel')


async def test_terminal_delivery_window_does_not_replay_or_rebill(stores, db_factory, tmp_path):
    runtime, jobs, _, user, session, provider = await prepare(stores, db_factory, tmp_path, [])
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    tape = await abandoned(runtime, jobs, user, session, terminal=True)
    clock[0] += 61
    assert await recover_once(jobs, {'privateclaw': runtime}) == 0
    assert not await jobs.list_jobs(user.id)
    async with db_factory() as db:
        row = await db.get(ForegroundJournal, tape.journal_id)
        assert row.status == 'closed'
        assert jobs.unpack(row.payload)['recovery_reason'] == 'delivery_verification_required'
    assert not provider.calls


async def test_revoked_user_is_paused_without_calling_provider(stores, db_factory, tmp_path):
    from claw.db.models import User
    runtime, jobs, worker, user, session, provider = await prepare(stores, db_factory, tmp_path, [])
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    await abandoned(runtime, jobs, user, session)
    async with db_factory() as db:
        record = await db.get(User, user.id)
        record.is_active = False
        await db.commit()
    clock[0] += 61
    assert await recover_once(jobs, {'privateclaw': runtime}) == 1
    job = (await jobs.list_jobs(user.id))[0]
    assert job['reason'] == 'permission_denied' and job['status'] == 'paused'
    assert not await worker.run_once()
    assert not provider.calls


async def test_unplanned_short_chat_is_not_guessed_into_a_workflow(stores, db_factory, tmp_path):
    runtime, jobs, worker, user, session, provider = await prepare(stores, db_factory, tmp_path, [])
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    tape = ForegroundAccounting(jobs, user.id, session.id)
    await tape.reserve_call('model', 90)
    tape.pulse.cancel()
    await asyncio.gather(tape.pulse, return_exceptions=True)
    clock[0] += 61
    await recover_once(jobs, {'privateclaw': runtime})
    job = (await jobs.list_jobs(user.id))[0]
    assert job['reason'] == 'recovery_contract_missing'
    assert not await worker.run_once()
    assert not provider.calls


async def test_watcher_runs_foreground_recovery(stores, db_factory, tmp_path):
    runtime, jobs, worker, user, session, _ = await prepare(stores, db_factory, tmp_path, [])
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    await abandoned(runtime, jobs, user, session, pending={'name': 'send_email'})
    clock[0] += 61

    async def recovery():
        await recover_once(jobs, {'privateclaw': runtime})
        worker.stopping.set()
    worker.recover_foreground = recovery
    await asyncio.wait_for(worker.serve(), timeout=5)
    assert (await jobs.list_jobs(user.id))[0]['reason'] == 'uncertain_effect'


async def test_heartbeat_after_scan_fences_recovery_admission(stores, db_factory, tmp_path):
    from unittest.mock import AsyncMock
    runtime, jobs, _, user, session, provider = await prepare(stores, db_factory, tmp_path, [])
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    tape = await abandoned(runtime, jobs, user, session)
    clock[0] += 61

    async def refresh():
        assert await heartbeat(jobs, tape.journal_id, user.id)
        return {}
    runtime.artifact_jobs.resource_policy = AsyncMock(side_effect=refresh)
    assert await recover_once(jobs, {'privateclaw': runtime}) == 0
    assert not await jobs.list_jobs(user.id)
    assert not provider.calls


async def test_terminal_watcher_delivers_without_provider_or_double_billing(stores, db_factory, tmp_path):
    from sqlalchemy import select
    from claw.db.models import Message, UsageRecord
    runtime, jobs, _, user, session, provider = await prepare(stores, db_factory, tmp_path, [])
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    tape = await abandoned(runtime, jobs, user, session)
    await tape.checkpoint({**tape.state, 'foreground_terminal': True, 'terminal_delivery': {
        'version': 1, 'entries': [{'role': 'assistant', 'content': 'Recovered final'}]}})
    clock[0] += 61
    await asyncio.gather(*(recover_once(jobs, {'privateclaw': runtime}) for _ in range(2)))
    assert not provider.calls
    async with db_factory() as db:
        messages = list((await db.scalars(select(Message).where(Message.session_id == session.id))).all())
        assert len(messages) == 1 and messages[0].content == 'Recovered final'
        usage = list((await db.scalars(select(UsageRecord).where(UsageRecord.user_id == user.id))).all())
        assert sum(r.prompt_tokens + r.completion_tokens for r in usage) == 100
    assert not await jobs.list_jobs(user.id)


async def test_live_short_turn_uses_atomic_delivery(stores, db_factory, tmp_path):
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from claw.db.models import Message, UsageRecord
    runtime, jobs, _, user, session, _ = await prepare(stores, db_factory, tmp_path, [
        [ChatResult(content='Hello', usage={'prompt_tokens': 8, 'completion_tokens': 2})]])
    runtime.usage = AsyncMock()
    assert await runtime.handle_message(user.id, session.id, 'hi') == 'Hello'
    runtime.usage.record.assert_not_called()
    async with db_factory() as db:
        messages = list((await db.scalars(select(Message).where(Message.session_id == session.id))).all())
        assert sum(m.role == 'assistant' for m in messages) == 1
        usage = list((await db.scalars(select(UsageRecord).where(UsageRecord.user_id == user.id))).all())
        assert len(usage) == 1 and usage[0].iterations == 1
