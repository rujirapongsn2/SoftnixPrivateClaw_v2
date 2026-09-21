"""Shared DAG admission, projection and room delivery with a deterministic executor."""
from functools import partial

import pytest

from claw.jobs.bot_bridge import execute, validate
from claw.jobs.store import JobStore
from claw.jobs.worker import Worker, Executor
from claw.security.crypto import SecretBox
from sbot.core.mission_engine import NodeResult
from tests.sbot_mode.test_missions import make_service, _two_specialists
from tests.sbot_mode.conftest import FakeProvider


async def test_mission_shared_worker_owns_dag_and_delivers_in_actor_rooms(stores, tmp_path):
    user, first, second = await _two_specialists(stores, 'durable-bots@test.dev')
    service = make_service(stores, FakeProvider([]), tmp_path)
    service.sessions, service.messages = stores['sessions'], stores['messages']
    service.durable_jobs = JobStore(stores['users'].factory, SecretBox('test'))
    service.durable_enabled = True
    await service.durable_jobs.initialize()
    origin = await stores['sessions'].thread_for_bot(user.id, second.id, title=second.name)
    calls = []

    def executor(mission):
        async def run(node, context):
            calls.append(node.id)
            if node.id == 'write':
                assert context.parent_outputs['read'] == 'result read'
                contract = await service.missions.blackboard_read(mission.id, 'contract:read')
                assert contract['verification_status'] == 'passed'
            return NodeResult(status='done', output='result ' + node.id,
                task_result={'status': 'completed', 'verification_status': 'passed', 'artifacts': []})
        return run
    service._executor_for = executor
    service._spawn = lambda _: (_ for _ in ()).throw(AssertionError('legacy executor must not start'))
    mission = await service.submit_work(owner_id=user.id, goal='Research and summarize', mission_id='test-shared-mission', nodes=[
        {'id': 'read', 'bot_id': first.id, 'title': 'Research', 'instruction': 'Research'},
        {'id': 'write', 'bot_id': second.id, 'title': 'Write', 'instruction': 'Summarize', 'depends_on': ['read']},
    ], session_id=origin.id, coordinator_id=second.id)

    async def allow(*args):
        return True
    worker = Worker(service.durable_jobs, {'sbot.node': Executor(partial(execute, service), partial(validate, service))}, allow)
    assert await service.resume_interrupted() == 0
    for _ in range(3):
        assert await worker.run_once()
    job = await service.durable_jobs.snapshot(user.id, mission.id)
    assert job['status'] == 'completed', job
    assert calls == ['read', 'write', '__summary']
    assert await service.durable_jobs.publish_deliveries() == 3
    assert await service.durable_jobs.publish_deliveries() == 0
    nodes = await service.missions.get_nodes(mission.id)
    assert all(n.status == 'done' for n in nodes)
    first_room = await stores['sessions'].thread_for_bot(user.id, first.id, title=first.name)
    assert (await service.durable_jobs.list_jobs(user.id, first_room.id))[0]['job_id'] == mission.id
    await service.reconcile_reports()  # must not publish a second legacy report


async def test_specialist_room_filter_does_not_decrypt_unrelated_job_specs(stores, tmp_path):
    """Room polling must stay SQL-filtered as an owner's job history grows."""
    from claw.jobs.models import Step

    user, first, second = await _two_specialists(stores, 'room-filter@test.dev')
    service = make_service(stores, FakeProvider([]), tmp_path)
    service.sessions, service.messages = stores['sessions'], stores['messages']
    service.durable_jobs = JobStore(stores['users'].factory, SecretBox('test'))
    service.durable_enabled = True
    await service.durable_jobs.initialize()
    origin = await stores['sessions'].thread_for_bot(user.id, second.id, title=second.name)
    await service.submit_work(owner_id=user.id, session_id=origin.id, mission_id='visible-job',
        goal='Visible', nodes=[{'id': 'visible', 'bot_id': first.id, 'title': 'Visible',
                               'instruction': 'Work'}], coordinator_id=second.id)
    await service.submit_work(owner_id=user.id, session_id=origin.id, mission_id='unrelated-job',
        goal='Other', nodes=[{'id': 'other', 'bot_id': second.id, 'title': 'Other',
                             'instruction': 'Work'}], coordinator_id=second.id)
    # If list_jobs still loads/decrypts every candidate spec, this unrelated
    # sentinel makes the request fail before SQL room filtering can apply.
    async with service.durable_jobs.factory() as db, db.begin():
        unrelated = await db.get(Step, ('unrelated-job', 'other'))
        unrelated.spec = 'must-not-be-decrypted-for-the-first-bot-room'
    room = await stores['sessions'].thread_for_bot(user.id, first.id, title=first.name)
    rows = await service.durable_jobs.list_jobs(user.id, room.id)
    assert [row['job_id'] for row in rows] == ['visible-job']


async def test_specialist_verified_file_progress_can_cross_multiple_slices(stores, tmp_path):
    from claw.jobs.provider import current_execution

    user, bot, _ = await _two_specialists(stores, 'slice-progress@test.dev')
    service = make_service(stores, FakeProvider([]), tmp_path)
    service.sessions, service.messages = stores['sessions'], stores['messages']
    service.durable_jobs = JobStore(stores['users'].factory, SecretBox('test'))
    service.durable_enabled = True
    await service.durable_jobs.initialize()
    room = await stores['sessions'].thread_for_bot(user.id, bot.id, title=bot.name)
    mission = await service.submit_work(owner_id=user.id, session_id=room.id,
        mission_id='slice-progress', goal='Build a report', nodes=[{
            'id': 'draft', 'bot_id': bot.id, 'title': 'Draft', 'instruction': 'Build iteratively',
            'required_files': ['draft.txt'],
        }], coordinator_id=bot.id)
    root = service.settings.workspaces_root / user.id
    root.mkdir(parents=True, exist_ok=True)

    def executor(_mission):
        async def run(_node, context):
            path = root / 'draft.txt'
            path.write_text(f'validated slice {context.attempt}')
            ctx = current_execution.get()
            await ctx.checkpoint({**ctx.state, 'written': ['draft.txt']})
            return NodeResult(status='partial', task_result={
                'status': 'incomplete', 'failure_reason': 'output_limit',
            })
        return run

    service._executor_for = executor

    async def allow(*_args):
        return True

    worker = Worker(service.durable_jobs, {
        'sbot.node': Executor(partial(execute, service), partial(validate, service)),
    }, allow)
    for _ in range(4):
        assert await worker.run_once()
    snapshot = await service.durable_jobs.snapshot(user.id, mission.id)
    assert snapshot['status'] == 'queued'
    assert snapshot['reason'] == 'output_limit'
    assert snapshot['steps'][0]['status'] == 'queued'


async def test_real_specialist_runtime_and_finish_step_delivery(stores, tmp_path):
    from sbot.providers.base import ChatResult, ToolCall
    from sbot.core.bus import EventBus
    from claw.jobs.provider import AccountedProvider
    user, bot, _ = await _two_specialists(stores, 'real-durable-bot@test.dev')
    provider = FakeProvider([
        [ChatResult(content=None, tool_calls=[ToolCall('write', 'write_file', {'path': 'result.txt', 'content': 'VERIFIED_TEST'})])],
        [ChatResult(content=None, tool_calls=[ToolCall('finish', 'finish_step', {
            'status': 'completed', 'summary': 'Created result.txt', 'evidence': 'File contains VERIFIED_TEST', 'files': ['result.txt']})])],
    ])
    service = make_service(stores, AccountedProvider(provider), tmp_path)
    service.sessions, service.messages, service.bus = stores['sessions'], stores['messages'], EventBus()
    service.durable_jobs = JobStore(stores['users'].factory, SecretBox('test'))
    service.durable_enabled = True
    await service.durable_jobs.initialize()
    room = await stores['sessions'].thread_for_bot(user.id, bot.id, title=bot.name)
    mission = await service.submit_work(owner_id=user.id, session_id=room.id, mission_id='real-runtime-mission',
        goal='Create result file', nodes=[{'id': 'file', 'bot_id': bot.id, 'title': 'File',
            'instruction': 'Create result.txt containing VERIFIED_TEST', 'required_files': ['result.txt']}], coordinator_id=bot.id)

    async def allow(*args):
        return True
    worker = Worker(service.durable_jobs, {'sbot.node': Executor(partial(execute, service), partial(validate, service))}, allow)
    await worker.run_once()
    job = await service.durable_jobs.snapshot(user.id, mission.id)
    assert job['status'] == 'completed', job
    assert len(provider.calls) == 2
    assert await service.durable_jobs.publish_deliveries() == 1
    assert await service.durable_jobs.publish_deliveries() == 0


async def test_bot_missing_sandbox_yields_dependency_without_command_retry(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import pytest
    from sbot.config import SandboxSettings
    from sbot.sandbox.ephemeral import EphemeralSandbox
    from sbot.tools.shell import ExecTool
    from claw.jobs.provider import current_execution
    from claw.jobs.runtime_bridge import Handoff

    spawn = AsyncMock(side_effect=FileNotFoundError('docker unavailable'))
    monkeypatch.setattr('claw.sandbox.ephemeral.asyncio.create_subprocess_exec', spawn)
    sandbox = EphemeralSandbox(SandboxSettings(enabled=True))
    ctx = SimpleNamespace(state={'pending_tool': {'name': 'exec'}})

    async def checkpoint(state):
        ctx.state = state
    ctx.checkpoint = checkpoint
    token = current_execution.set(ctx)
    try:
        with pytest.raises(Handoff) as caught:
            await ExecTool(sandbox, tmp_path).execute('python3 generate.py')
        assert caught.value.outcome.status == 'dependency'
        assert caught.value.outcome.dependency == 'sandbox'
        assert ctx.state['pending_tool'] is None
        assert spawn.await_count == 1
    finally:
        current_execution.reset(token)


async def test_legacy_scheduler_cannot_claim_during_shared_admission(stores, tmp_path, monkeypatch):
    from claw.jobs import bot_bridge
    user, first, second = await _two_specialists(stores, 'admission-race@test.dev')
    service = make_service(stores, FakeProvider([]), tmp_path)
    service.sessions, service.messages = stores['sessions'], stores['messages']
    service.durable_jobs = JobStore(stores['users'].factory, SecretBox('test'))
    service.durable_enabled = True
    await service.durable_jobs.initialize()
    origin = await stores['sessions'].thread_for_bot(user.id, second.id, title=second.name)
    original = bot_bridge.enqueue
    async def interleave(service, mission):
        stored = await service.missions.get_mission(mission.id, user.id)
        assert stored.status == 'planned'
        assert await service.resume_interrupted() == 0
        await original(service, mission)
        assert await service.resume_interrupted() == 0
    monkeypatch.setattr(bot_bridge, 'enqueue', interleave)
    service._spawn = lambda _: (_ for _ in ()).throw(AssertionError('legacy execution raced shared admission'))
    await service.submit_work(owner_id=user.id, goal='Research', mission_id='admission-race', nodes=[
        {'id': 'read', 'bot_id': first.id, 'title': 'Research', 'instruction': 'Research'}],
        session_id=origin.id, coordinator_id=second.id)


async def test_specialist_cannot_reset_root_budget_by_spawning_new_job(stores, tmp_path):
    from types import SimpleNamespace
    import pytest
    from claw.jobs.provider import current_execution
    from sbot.core.mission_engine import InvalidGraphError
    service = make_service(stores, FakeProvider([]), tmp_path)
    token = current_execution.set(SimpleNamespace(lease=SimpleNamespace(job_id='existing-root')))
    try:
        with pytest.raises(InvalidGraphError, match='current root job'):
            await service.submit_work('owner', 'room', 'new-budget', 'nested', [], 'lead')
    finally:
        current_execution.reset(token)


async def test_foreground_lead_and_specialist_share_root_usage_without_double_billing(stores, tmp_path):
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from claw.jobs.models import Job, ResourceCall
    from claw.db.models import UsageRecord
    from claw.jobs.provider import AccountedProvider, foreground_accounting
    from sbot.providers.base import ChatResult, ToolCall
    from tests.sbot_mode.test_bot_profile import make_runtime
    user, specialist, _ = await _two_specialists(stores, 'lead-accounting@test.dev')
    leader = await stores['bots'].get_or_create_cos(user.id)
    provider = AccountedProvider(FakeProvider([
        [ChatResult(content=None, tool_calls=[ToolCall('submit', 'team_submit', {
            'goal': 'Create a verified result', 'nodes': [{'id': 'file', 'bot_id': specialist.id,
            'title': 'Write', 'instruction': 'Write result.txt', 'required_files': ['result.txt']}]})],
            usage={'prompt_tokens': 120, 'completion_tokens': 30})],
        [ChatResult(content=None, tool_calls=[ToolCall('write', 'write_file', {'path': 'result.txt', 'content': 'VERIFIED'})],
            usage={'prompt_tokens': 80, 'completion_tokens': 20})],
        [ChatResult(content=None, tool_calls=[ToolCall('finish', 'finish_step', {
            'status': 'completed', 'summary': 'Created result.txt', 'evidence': 'File contains VERIFIED', 'files': ['result.txt']})],
            usage={'prompt_tokens': 60, 'completion_tokens': 10})],
    ]))
    service = make_service(stores, provider, tmp_path)
    service.sessions, service.messages = stores['sessions'], stores['messages']
    service.durable_jobs = JobStore(stores['users'].factory, SecretBox('test'))
    service.durable_enabled = True
    await service.durable_jobs.initialize()
    runtime = make_runtime(stores, provider, tmp_path)
    runtime.missions = service
    runtime.usage = AsyncMock()
    room = await stores['sessions'].thread_for_bot(user.id, leader.id, title=leader.name)
    result = await runtime.handle_message(user.id, room.id, 'Assign a verified result to the specialist', permission_mode='auto')
    assert result and not result.startswith('Error')
    assert foreground_accounting.get() is None
    job = (await service.durable_jobs.list_jobs(user.id, room.id))[0]
    async with service.durable_jobs.factory() as db:
        root = await db.get(Job, job['job_id'])
        assert root.tokens == 150
    runtime.usage.record.assert_not_called()

    async def allow(*args):
        return True
    worker = Worker(service.durable_jobs, {'sbot.node': Executor(partial(execute, service), partial(validate, service))}, allow)
    assert await worker.run_once()
    assert (await service.durable_jobs.snapshot(user.id, job['job_id']))['status'] == 'completed'
    async with service.durable_jobs.factory() as db:
        root = await db.get(Job, job['job_id'])
        assert root.tokens == 320
        receipts = list((await db.scalars(select(ResourceCall).where(ResourceCall.job_id == root.id))).all())
        assert len(receipts) == 3 and sum(r.actual for r in receipts) == 320
        records = list((await db.scalars(select(UsageRecord).where(UsageRecord.user_id == user.id))).all())
        assert sum(r.prompt_tokens + r.completion_tokens for r in records) == 320
        assert sum(r.counts_as_turn for r in records) == 1


async def test_abandoned_bot_foreground_is_visible_without_replaying_delegation(stores, tmp_path):
    import asyncio
    from claw.jobs.provider import ForegroundAccounting
    from claw.jobs.recovery import recover_once
    from tests.sbot_mode.test_bot_profile import make_runtime
    user, bot, _ = await _two_specialists(stores, 'bot-orphan@test.dev')
    runtime = make_runtime(stores, FakeProvider([]), tmp_path)
    jobs = JobStore(stores['users'].factory, SecretBox('test'))
    await jobs.initialize()
    room = await stores['sessions'].thread_for_bot(user.id, bot.id, title=bot.name)
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    tape = ForegroundAccounting(jobs, user.id, room.id, 'sbot')
    await tape.reserve_call('model', 80)
    await tape.checkpoint({'request': {'content': 'Research', 'locale': 'th', 'actor': bot.id},
                           'pending_tool': {'name': 'team_submit'}})
    tape.pulse.cancel()
    await asyncio.gather(tape.pulse, return_exceptions=True)
    clock[0] += 61
    assert await recover_once(jobs, {'sbot': runtime}) == 1
    job = (await jobs.list_jobs(user.id, room.id))[0]
    assert job['mode'] == 'sbot' and job['locale'] == 'th'
    assert job['status'] == 'paused' and job['reason'] == 'uncertain_effect'
    assert job['steps'][0]['actor'] == bot.id
    assert await jobs.control(job['job_id'], user.id, 'cancel')


@pytest.mark.parametrize("mutation", [None, "instruction", "attempts"])
async def test_prepared_bot_admission_recovers_original_dag_and_usage(stores, tmp_path, monkeypatch, mutation):
    import asyncio
    import pytest
    from claw.jobs.provider import ForegroundAccounting, foreground_accounting
    from claw.jobs.recovery import recover_once
    from tests.sbot_mode.test_bot_profile import make_runtime
    user, bot, _ = await _two_specialists(stores, 'prepared-orphan@test.dev')
    service = make_service(stores, FakeProvider([]), tmp_path)
    service.sessions, service.messages = stores['sessions'], stores['messages']
    jobs = JobStore(stores['users'].factory, SecretBox('test'))
    service.durable_jobs, service.durable_enabled = jobs, True
    await jobs.initialize()
    runtime = make_runtime(stores, FakeProvider([]), tmp_path)
    runtime.missions = service
    room = await stores['sessions'].thread_for_bot(user.id, bot.id, title=bot.name)
    clock = [1000.0]
    jobs.clock = lambda: clock[0]
    tape = ForegroundAccounting(jobs, user.id, room.id, 'sbot')
    await tape.reserve_call('model', 80)
    await tape.checkpoint({'request': {'content': 'Research', 'locale': 'th', 'actor': bot.id},
                           'pending_tool': {'name': 'team_submit'}})
    original = jobs.submit

    async def crash(*args, **kwargs):
        raise RuntimeError('crash before queue commit')

    monkeypatch.setattr(jobs, 'submit', crash)
    token = foreground_accounting.set(tape)
    try:
        with pytest.raises(RuntimeError, match='crash before'):
            await service.submit_work(owner_id=user.id, session_id=room.id, mission_id='prepared-orphan',
                goal='Research', nodes=[{'id': 'research', 'bot_id': bot.id, 'title': 'Research',
                                       'instruction': 'Research sources'}], coordinator_id=bot.id)
    finally:
        foreground_accounting.reset(token)
        tape.pulse.cancel()
        await asyncio.gather(tape.pulse, return_exceptions=True)
    monkeypatch.setattr(jobs, 'submit', original)
    assert (await service.missions.get_mission('prepared-orphan', user.id)).status == 'planned'
    if mutation:
        from sbot.db.models import MissionNode
        async with jobs.factory() as db, db.begin():
            node = await db.get(MissionNode, {'mission_id': 'prepared-orphan', 'id': 'research'})
            setattr(node, mutation, 'changed instruction' if mutation == 'instruction' else 1)
    clock[0] += 61
    await asyncio.gather(recover_once(jobs, {'sbot': runtime}), recover_once(jobs, {'sbot': runtime}))
    rows = await jobs.list_jobs(user.id, room.id)
    if mutation:
        assert len(rows) == 1 and rows[0]['status'] == 'paused'
        assert rows[0]['reason'] == 'recovery_contract_missing'
        assert await jobs.snapshot(user.id, 'prepared-orphan') is None
        assert (await service.missions.get_mission('prepared-orphan', user.id)).status == 'planned'
        return
    assert len(rows) == 1 and rows[0]['job_id'] == 'prepared-orphan'
    assert rows[0]['status'] == 'queued' and rows[0]['locale'] == 'th'
    from claw.jobs.models import Job, ResourceCall
    from sqlalchemy import select
    async with jobs.factory() as db:
        root = await db.get(Job, 'prepared-orphan')
        assert root.reserved_tokens == 80
        assert len(list((await db.scalars(select(ResourceCall).where(ResourceCall.job_id == root.id))).all())) == 1
    assert await service.resume_interrupted() == 0
    assert await recover_once(jobs, {'sbot': runtime}) == 0
    executed = []

    def executor(mission):
        async def run(node, context):
            executed.append(node.id)
            return NodeResult(status='done', output='Verified research result',
                task_result={'status': 'completed', 'verification_status': 'passed', 'artifacts': []})
        return run

    async def allow(*args):
        return True

    service._executor_for = executor
    worker = Worker(jobs, {'sbot.node': Executor(partial(execute, service), partial(validate, service))}, allow)
    assert await worker.run_once()
    assert not await worker.run_once()
    assert executed == ['research']
    assert (await jobs.snapshot(user.id, 'prepared-orphan'))['status'] == 'completed'
    assert await jobs.publish_deliveries() == 1
    assert await jobs.publish_deliveries() == 0


async def test_bot_short_turn_terminal_delivery_bills_once(stores, tmp_path):
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from claw.db.models import UsageRecord
    from sbot.db.models import Message
    from claw.jobs.provider import AccountedProvider
    from sbot.providers.base import ChatResult
    from tests.sbot_mode.test_bot_profile import make_runtime
    user, bot, _ = await _two_specialists(stores, 'bot-terminal@test.dev')
    provider = AccountedProvider(FakeProvider([[ChatResult(content='Hello',
        usage={'prompt_tokens': 8, 'completion_tokens': 2})]]))
    service = make_service(stores, provider, tmp_path)
    service.durable_enabled = True
    service.durable_jobs = JobStore(stores['users'].factory, SecretBox('test'))
    await service.durable_jobs.initialize()
    runtime = make_runtime(stores, provider, tmp_path)
    runtime.missions, runtime.usage = service, AsyncMock()
    room = await stores['sessions'].thread_for_bot(user.id, bot.id, title=bot.name)
    assert await runtime.handle_message(user.id, room.id, 'hi') == 'Hello'
    runtime.usage.record.assert_not_called()
    async with stores['users'].factory() as db:
        messages = list((await db.scalars(select(Message).where(Message.session_id == room.id))).all())
        assert sum(m.role == 'assistant' for m in messages) == 1
        records = list((await db.scalars(select(UsageRecord).where(UsageRecord.user_id == user.id))).all())
        assert len(records) == 1 and records[0].iterations == 1
