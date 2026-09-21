"""Durability acceptance tests use real transactions, fake clocks and fake adapters."""
import asyncio

import pytest
from sqlalchemy import select

from claw.jobs.contracts import StepOutcome
from claw.jobs.models import Attempt, Delivery, Job, ResourceCall, Step
from claw.jobs.store import BudgetExhausted, JobStore, LeaseLost
from claw.jobs.worker import Executor, Worker
from claw.security.crypto import SecretBox


@pytest.fixture
async def jobs(db_factory, stores):
    user = await stores['users'].get_or_create_by_email('jobs@example.test')
    clock = [1000.0]
    store = JobStore(db_factory, SecretBox('test-key'), clock=lambda: clock[0], jitter=lambda a,b: 1)
    await store.initialize()
    store.test_owner, store.test_clock = user.id, clock
    return store


async def submit(store, name='job', **overrides):
    step = {'id': 'read', 'executor': 'fake', 'acceptance': {'kind': 'test'}, **overrides}
    await store.submit(name, store.test_owner, 'session', 'privateclaw', [step])
    return name


async def allow(*args):
    return True


async def yes(context, result):
    return result.evidence.get('verified') is True


async def test_claim_race_fencing_and_restart(jobs):
    await submit(jobs)
    claims = await asyncio.gather(*(jobs.claim(f'worker-{i}', {'fake'}) for i in range(2)))
    old = next(c for c in claims if c)
    assert sum(c is not None for c in claims) == 1
    await jobs.checkpoint(old, {'cursor': 42})
    jobs.test_clock[0] += 61
    new = await jobs.claim('new', {'fake'})
    assert new.fence == old.fence + 1 and new.checkpoint == {'cursor': 42}
    with pytest.raises(LeaseLost):
        await jobs.settle(old, StepOutcome('completed'), validated=True)
    await jobs.settle(new, StepOutcome('completed', evidence={'verified': True}), validated=True)
    assert (await jobs.snapshot(jobs.test_owner, 'job'))['status'] == 'completed'


async def test_unsafe_effect_is_not_replayed(jobs):
    await submit(jobs, effect='external')
    old = await jobs.claim('old', {'fake'})
    jobs.test_clock[0] += 61
    assert await jobs.claim('new', {'fake'}) is None
    snap = await jobs.snapshot(jobs.test_owner, 'job')
    assert snap['status'] == 'paused' and snap['reason'] == 'uncertain_effect'
    assert not await jobs.control('job', jobs.test_owner, 'resume')
    with pytest.raises(LeaseLost):
        await jobs.checkpoint(old, {'receipt': 'late'})


async def test_resource_reservation_survives_restart(jobs):
    await jobs.submit('job', jobs.test_owner, 'session', 'sbot', [{'id': 'a', 'executor': 'fake'}],
                      policy={'max_tokens': 100})
    lease = await jobs.claim('first', {'fake'})
    await jobs.reserve(lease, 'call1', 'model', 80)
    jobs.test_clock[0] += 61
    lease2 = await jobs.claim('second', {'fake'})
    with pytest.raises(BudgetExhausted):
        await jobs.reserve(lease2, 'call2', 'model', 21)
    await jobs.reconcile('call1', 50, job_id='job')
    await jobs.reconcile('call1', 50, job_id='job')
    await jobs.reserve(lease2, 'call3', 'model', 50)
    with pytest.raises(ValueError):
        await jobs.reserve(lease2, 'call3', 'model', 50)
    async with jobs.factory() as db:
        job = await db.get(Job, 'job')
        assert job.tokens == 50 and job.reserved_tokens == 50


async def test_cancel_fences_pending_delivery_and_owner(jobs):
    await submit(jobs)
    lease = await jobs.claim('worker', {'fake'})
    assert not await jobs.control('job', 'another-owner', 'cancel')
    assert await jobs.snapshot('another-owner', 'job') is None
    assert await jobs.events('another-owner', 'job') is None
    assert await jobs.control('job', jobs.test_owner, 'cancel')
    with pytest.raises(LeaseLost):
        await jobs.settle(lease, StepOutcome('completed', delivery={'artifact': 'x'}), validated=True)
    async with jobs.factory() as db:
        assert list((await db.scalars(select(Delivery))).all()) == []


async def test_completion_requires_validation_and_delivery_once(jobs):
    await submit(jobs)
    lease = await jobs.claim('worker', {'fake'})
    result = StepOutcome('completed', delivery={'artifact': 'x'})
    with pytest.raises(ValueError):
        await jobs.settle(lease, result)
    await jobs.settle(lease, result, validated=True)
    with pytest.raises(LeaseLost):
        await jobs.settle(lease, result, validated=True)
    async with jobs.factory() as db:
        assert len(list((await db.scalars(select(Delivery))).all())) == 1
    events = await jobs.events(jobs.test_owner, 'job')
    assert [e['sequence'] for e in events] == [1, 2, 3]
    assert await jobs.events(jobs.test_owner, 'job', 2) == events[2:]


async def test_dependency_wait_releases_slot_and_expires(jobs):
    await submit(jobs)
    lease = await jobs.claim('worker', {'fake'})
    await jobs.settle(lease, StepOutcome('dependency', dependency='sandbox', checkpoint={'cursor': 4}))
    await submit(jobs, 'other')
    assert await jobs.claim('other-worker', {'fake'}, total=1) is not None
    jobs.test_clock[0] += 30
    assert len(await jobs.due_dependencies()) == 1
    await jobs.dependency_result('job', 'read', lease.fence, False)
    assert not await jobs.due_dependencies()
    jobs.test_clock[0] += 86400
    await jobs.dependency_result('job', 'read', lease.fence, True)
    snap = await jobs.snapshot(jobs.test_owner, 'job')
    assert snap['status'] == 'paused' and snap['reason'] == 'dependency_wait_expired'
    assert await jobs.control('job', jobs.test_owner, 'resume')


async def test_dependency_recovers_without_repeating_previous_step(jobs):
    await jobs.submit('job', jobs.test_owner, 'session', 'sbot', [
        {'id': 'a', 'executor': 'fake'}, {'id': 'b', 'executor': 'fake', 'depends_on': ['a']}])
    first = await jobs.claim('worker', {'fake'})
    await jobs.settle(first, StepOutcome('completed'), validated=True)
    second = await jobs.claim('worker', {'fake'})
    assert second.step_id == 'b'
    await jobs.settle(second, StepOutcome('dependency', dependency='network', checkpoint={'read': True}))
    await jobs.dependency_result('job', 'b', second.fence, True)
    resumed = await jobs.claim('worker', {'fake'})
    assert resumed.step_id == 'b' and resumed.checkpoint['read']


async def test_idempotent_submit_and_encrypted_payload(jobs):
    await submit(jobs, inputs={'document': 'private source'})
    await submit(jobs, inputs={'document': 'private source'})
    with pytest.raises(ValueError):
        await submit(jobs, inputs={'document': 'different'})
    async with jobs.factory() as db:
        step = await db.get(Step, ('job', 'read'))
        assert 'private source' not in step.spec and step.spec.startswith('enc::')


async def test_worker_validation_and_provider_slow(jobs):
    await submit(jobs)

    async def slow(ctx):
        await ctx.checkpoint({'phase': 'parsed'})
        await asyncio.sleep(1)
        return StepOutcome('completed', evidence={'verified': True})

    worker = Worker(jobs, {'fake': Executor(slow, yes)}, allow, slice_seconds=.02)
    assert await worker.run_once()
    assert (await jobs.snapshot(jobs.test_owner, 'job'))['status'] == 'queued'

    async def finish(ctx):
        assert ctx.lease.checkpoint == {'phase': 'parsed'}
        return StepOutcome('completed', evidence={'verified': False})

    worker.executors['fake'] = Executor(finish, yes)
    await worker.run_once()
    assert (await jobs.snapshot(jobs.test_owner, 'job'))['reason'] == 'validation_failed'


async def test_retry_exhaustion_and_permission_revoked(jobs):
    await submit(jobs)

    async def broken(ctx):
        return StepOutcome('retry', reason='transient')

    worker = Worker(jobs, {'fake': Executor(broken, yes)}, allow)
    for _ in range(3):
        await worker.run_once()
        jobs.test_clock[0] += 40
    assert (await jobs.snapshot(jobs.test_owner, 'job'))['reason'] == 'recovery_exhausted'
    await submit(jobs, 'revoked')

    async def denied(*args):
        return False

    worker.authorize = denied
    await worker.run_once()
    assert (await jobs.snapshot(jobs.test_owner, 'revoked'))['reason'] == 'permission_denied'


async def test_concurrency_shared_between_modes(jobs):
    for i in range(3):
        await jobs.submit(f'job{i}', jobs.test_owner, 'session', 'sbot' if i else 'privateclaw',
                          [{'id': 'a', 'executor': 'fake'}])
    leases = await asyncio.gather(*(jobs.claim(str(i), {'fake'}, total=4, per_owner=2) for i in range(3)))
    assert sum(x is not None for x in leases) == 2


async def test_heartbeat_accounts_time_and_preserves_unknown_usage(jobs):
    await jobs.submit('job', jobs.test_owner, 'session', 'privateclaw', [{'id': 'a', 'executor': 'fake'}],
                      policy={'max_seconds': 10})
    lease = await jobs.claim('worker', {'fake'})
    await jobs.reserve(lease, 'unknown-call', 'model', 10)
    jobs.test_clock[0] += 11
    assert not await jobs.heartbeat(lease)
    async with jobs.factory() as db:
        assert (await db.get(Job, 'job')).active_seconds == 11
        assert (await db.get(ResourceCall, 'unknown-call')).actual is None
    assert not await jobs.control('job', jobs.test_owner, 'resume')


async def test_completed_step_not_replayed_on_worker_restart(jobs):
    await jobs.submit('job', jobs.test_owner, 'session', 'sbot', [
        {'id': 'a', 'executor': 'fake'}, {'id': 'b', 'executor': 'fake', 'depends_on': ['a']}])
    counts = []

    async def execute(ctx):
        counts.append(ctx.lease.step_id)
        return StepOutcome('completed', evidence={'verified': True})

    await Worker(jobs, {'fake': Executor(execute, yes)}, allow).run_once()
    await Worker(jobs, {'fake': Executor(execute, yes)}, allow).run_once()
    assert counts == ['a', 'b']
    async with jobs.factory() as db:
        assert len(list((await db.scalars(select(Attempt))).all())) == 2


async def test_provider_attempts_and_unknown_usage_are_charged(jobs):
    from claw.jobs.provider import AccountedProvider, current_execution
    from claw.jobs.worker import ExecutionContext
    from claw.providers.base import ChatResult
    from tests.conftest import FakeProvider

    await submit(jobs)
    lease = await jobs.claim('worker', {'fake'})
    provider = AccountedProvider(FakeProvider([
        [ChatResult(content='ok', usage={'prompt_tokens': 12, 'completion_tokens': 8})],
        [ChatResult(content='no usage')],
    ]))
    token = current_execution.set(ExecutionContext(jobs, lease))
    try:
        for model in ['primary', 'fallback']:
            async for _ in provider.stream_chat([{'role': 'user', 'content': 'test'}], model=model, max_tokens=10):
                pass
    finally:
        current_execution.reset(token)
    async with jobs.factory() as db:
        job = await db.get(Job, 'job')
        assert job.tokens == 20 and job.reserved_tokens > 0
        calls = list((await db.scalars(select(ResourceCall))).all())
        assert len(calls) == 2 and {c.model for c in calls} == {'primary', 'fallback'}


async def test_cancel_running_worker_cancels_tool(jobs):
    await submit(jobs)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def execute(ctx):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()
        return StepOutcome('completed', evidence={'verified': True})

    worker = Worker(jobs, {'fake': Executor(execute, yes)}, allow, heartbeat_seconds=.01)
    task = asyncio.create_task(worker.run_once())
    await started.wait()
    await jobs.control('job', jobs.test_owner, 'cancel')
    await asyncio.wait_for(task, 1)
    assert stopped.is_set()


async def test_revoked_dependency_probe_never_uses_connector(jobs):
    await submit(jobs)
    lease = await jobs.claim('worker', {'fake'})
    await jobs.settle(lease, StepOutcome('dependency', dependency='connector'))
    jobs.test_clock[0] += 31
    calls = []

    async def probe(*args):
        calls.append(args)
        return True

    async def deny(*args):
        return False

    worker = Worker(jobs, {}, deny, probes={('privateclaw', 'connector'): probe})
    await worker.probe_once()
    assert calls == []
    assert (await jobs.snapshot(jobs.test_owner, 'job'))['reason'] == 'permission_denied'


def test_validators_reject_missing_source_and_validate_workbook(tmp_path):
    import hashlib
    from openpyxl import Workbook
    from claw.jobs.validators import source_coverage, validate_workbook, validate_research, validate_receipt

    source = {'tor': {'sha256': 'abc', 'length': 100}}
    assert not source_coverage({'tor': {'sha256': 'abc', 'ranges': [[0, 40], [60, 100]]}}, source)
    assert source_coverage({'tor': {'sha256': 'abc', 'ranges': [[0, 40], [40, 100]]}}, source)
    book = Workbook()
    sheet = book.active
    sheet.title = 'Data'
    sheet.append(['Item', 'Qty'])
    sheet.append(['Example', 2])
    path = tmp_path / 'output.xlsx'
    book.save(path)
    ref = {'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    assert validate_workbook(tmp_path, ref, {'Data': ['Item', 'Qty']})
    assert not validate_workbook(tmp_path, ref, {'Missing': ['Item']})
    assert not validate_research([{'question_id': 'q', 'answer': 'x', 'citations': ['fake']}], {'q'}, {})
    assert validate_research([{'question_id': 'q', 'answer': 'x', 'citations': ['s']}], {'q'}, {'s': {'sha256': 'abc'}})
    assert not validate_receipt({'remote_id': '123', 'status': 'confirmed'}, 'expected')
    assert validate_receipt({'remote_id': '123', 'status': 'confirmed', 'idempotency_key': 'expected'}, 'expected')


def test_additive_migration_upgrade_downgrade():
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = 'migrations/versions/e6b8d1c03a72_shared_durable_jobs.py'
    spec = importlib.util.spec_from_file_location('durable_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE users (id VARCHAR(32) PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE legacy_record (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('INSERT INTO legacy_record (id) VALUES (7)')
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            assert 'agent_resource_calls' in inspect(conn).get_table_names()
            assert conn.exec_driver_sql('SELECT version FROM agent_queue_locks').scalar() == 0
            module.downgrade()
            assert 'agent_jobs' not in inspect(conn).get_table_names()
            assert conn.exec_driver_sql('SELECT id FROM legacy_record').scalar() == 7
    engine.dispose()


def test_durable_migration_refuses_to_drop_job_history():
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = 'migrations/versions/e6b8d1c03a72_shared_durable_jobs.py'
    spec = importlib.util.spec_from_file_location('durable_migration_guard', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE users (id VARCHAR(32) PRIMARY KEY)')
        conn.exec_driver_sql("INSERT INTO users (id) VALUES ('owner')")
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            conn.exec_driver_sql(
                "INSERT INTO agent_jobs "
                "(id, owner_id, mode, session_id, locale, status, reason, spec_hash, policy, "
                "tokens, reserved_tokens, active_seconds, sequence, created_at, updated_at, finished_at) "
                "VALUES ('job', 'owner', 'privateclaw', 'room', 'en', 'paused', '', 'hash', '{}', "
                "0, 0, 0, 0, 1, 1, NULL)"
            )
            with pytest.raises(RuntimeError, match='Retain durable job history'):
                module.downgrade()
            assert 'agent_jobs' in inspect(conn).get_table_names()
            assert conn.exec_driver_sql('SELECT id FROM agent_jobs').scalar() == 'job'
    engine.dispose()


async def test_outbox_delivers_to_normal_chat_exactly_once(jobs, stores):
    from claw.db.models import Message
    session = await stores['sessions'].create(jobs.test_owner)
    await jobs.submit('job', jobs.test_owner, session.id, 'privateclaw', [{'id': 'a', 'executor': 'fake'}])
    lease = await jobs.claim('worker', {'fake'})
    await jobs.settle(lease, StepOutcome('completed', delivery={'content': 'verified result'}), validated=True)
    assert await jobs.publish_deliveries() == 1
    assert await jobs.publish_deliveries() == 0
    async with jobs.factory() as db:
        rows = list((await db.scalars(select(Message).where(Message.session_id == session.id))).all())
        assert len(rows) == 1 and rows[0].content == 'verified result'
        assert rows[0].meta['job_id'] == 'job'


async def test_retention_preserves_artifact_files(jobs, tmp_path):
    file = tmp_path / 'delivered.txt'
    file.write_text('keep')
    await submit(jobs, inputs={'source': 'secret'})
    lease = await jobs.claim('worker', {'fake'})
    await jobs.settle(lease, StepOutcome('paused', checkpoint={'source': 'secret'}, reason='blocked'))
    jobs.test_clock[0] += 8 * 86400
    await jobs.prune()
    async with jobs.factory() as db:
        step = await db.get(Step, ('job', 'read'))
        assert step.checkpoint == '' and jobs.unpack(step.spec)['inputs'] == {}
    assert file.read_text() == 'keep'
    assert (await jobs.snapshot(jobs.test_owner, 'job'))['reason'] == 'checkpoint_expired'
    jobs.test_clock[0] += 31 * 86400
    assert await jobs.prune() == 1


async def test_yield_without_validated_progress_is_bounded(jobs):
    await submit(jobs)
    for i in range(3):
        lease = await jobs.claim('worker', {'fake'})
        await jobs.settle(lease, StepOutcome('yielded', checkpoint={'created_file': f'noise{i}'}))
    assert (await jobs.snapshot(jobs.test_owner, 'job'))['reason'] == 'no_progress'


async def test_verified_progress_allows_more_slices(jobs):
    await submit(jobs)
    for i in range(5):
        lease = await jobs.claim('worker', {'fake'})
        await jobs.settle(lease, StepOutcome('yielded', evidence={'validated_coverage': i + 1}), validated=True)
    assert (await jobs.snapshot(jobs.test_owner, 'job'))['status'] == 'queued'


async def test_job_api_ownership_replay_and_control(jobs):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from claw.api.deps import current_user, get_state
    from claw.jobs.api import router

    await submit(jobs)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_state] = lambda: SimpleNamespace(jobs=jobs)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id='someone-else')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/api/jobs/job')).status_code == 404
        assert (await client.get('/api/jobs/job/events')).status_code == 404
        assert (await client.post('/api/jobs/job/cancel')).status_code == 404
        assert (await client.get('/api/jobs')).json() == []
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=jobs.test_owner)
        assert (await client.get('/api/jobs/job')).json()['status'] == 'queued'
        assert len((await client.get('/api/jobs/job/events?after=0')).json()) == 1
        assert (await client.get('/api/jobs/job/events?after=-1')).status_code == 422
        assert (await client.post('/api/jobs/job/cancel')).json()['status'] == 'cancelled'
        assert (await client.post('/api/jobs/job/resume')).status_code == 409


async def test_outbox_routes_specialist_only_to_own_room(jobs):
    from sbot.db.models import Bot, ChatSession, Message
    async with jobs.factory() as db:
        bot = Bot(owner_id=jobs.test_owner, name='Researcher')
        db.add(bot)
        await db.flush()
        room = ChatSession(user_id=jobs.test_owner, bot_id=bot.id)
        wrong_room = ChatSession(user_id=jobs.test_owner)
        db.add_all([room, wrong_room])
        await db.commit()
    for name, target in [('good', room.id), ('bad', wrong_room.id)]:
        await jobs.submit(name, jobs.test_owner, wrong_room.id, 'sbot',
                          [{'id': 'a', 'executor': 'fake', 'actor': bot.id}])
        lease = await jobs.claim('worker', {'fake'})
        await jobs.settle(lease, StepOutcome('completed', delivery={'content': name, 'session_id': target}), validated=True)
    assert await jobs.publish_deliveries() == 1
    async with jobs.factory() as db:
        messages = list((await db.scalars(select(Message))).all())
        assert len(messages) == 1
        assert messages[0].session_id == room.id and messages[0].speaker_bot_id == bot.id


async def test_killed_process_recovers_durable_checkpoint(jobs, tmp_path):
    import sys
    import os
    from sqlalchemy import text
    from asyncio.subprocess import DEVNULL

    await submit(jobs)
    marker = tmp_path / 'checkpoint-ready'
    script = '''
import asyncio, sys, os
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from claw.jobs.store import JobStore
from claw.jobs.worker import Worker, Executor
from claw.security.crypto import SecretBox
async def main():
    schema = os.environ.get('DURABLE_CHILD_SCHEMA')
    engine = create_async_engine(os.environ['DURABLE_CHILD_URL'],
        connect_args={'server_settings': {'search_path': schema}} if schema else {})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = JobStore(factory, SecretBox('test-key'), clock=lambda: 1000.0)
    async def authorize(*args): return True
    async def validate(*args): return False
    async def execute(ctx):
        await ctx.checkpoint({'phase': 'source_read', 'cursor': 120})
        Path(sys.argv[1]).write_text('ready')
        await asyncio.sleep(120)
    await Worker(store, {'fake': Executor(execute, validate)}, authorize).run_once()
asyncio.run(main())
'''
    url = jobs.factory.kw['bind'].url
    child_env = {**os.environ, 'DURABLE_CHILD_URL': url.render_as_string(hide_password=False)}
    if url.get_backend_name() == 'postgresql':
        async with jobs.factory() as db:
            child_env['DURABLE_CHILD_SCHEMA'] = await db.scalar(text('SELECT current_schema()'))
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', script, str(marker), env=child_env, stdout=DEVNULL, stderr=DEVNULL)
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                assert process.returncode is None
                await asyncio.sleep(.02)
        process.kill()
        await process.wait()
        jobs.test_clock[0] += 61
        recovered = await jobs.claim('replacement', {'fake'})
        assert recovered.checkpoint == {'phase': 'source_read', 'cursor': 120}
        assert recovered.fence == 2
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_chat_and_outbox_sequence_allocation_is_shared(jobs, stores):
    from claw.db.models import Message
    session = await stores['sessions'].create(jobs.test_owner)
    await jobs.submit('job', jobs.test_owner, session.id, 'privateclaw', [{'id': 'a', 'executor': 'fake'}])
    lease = await jobs.claim('worker', {'fake'})
    await jobs.settle(lease, StepOutcome('completed', delivery={'content': 'result'}), validated=True)
    await asyncio.gather(jobs.publish_deliveries(), *(stores['messages'].append(
        session.id, [{'role': 'user', 'content': str(i)}]) for i in range(8)))
    async with jobs.factory() as db:
        seqs = list((await db.scalars(select(Message.seq).where(Message.session_id == session.id)
                                      .order_by(Message.seq))).all())
    assert seqs == list(range(1, 10))


async def test_parallel_specialists_share_root_budget_and_join(jobs):
    await jobs.submit('team', jobs.test_owner, 'session', 'sbot', [
        {'id': 'research', 'executor': 'fake'}, {'id': 'review', 'executor': 'fake'},
        {'id': 'summary', 'executor': 'fake', 'depends_on': ['research', 'review']}], policy={'max_tokens': 100})
    a, b = await asyncio.gather(jobs.claim('a', {'fake'}), jobs.claim('b', {'fake'}))
    assert a and b and a.step_id != b.step_id
    await jobs.reserve(a, 'research-call', 'model', 60)
    with pytest.raises(BudgetExhausted):
        await jobs.reserve(b, 'review-call', 'model', 41)
    await jobs.settle(a, StepOutcome('completed'), validated=True)
    assert (await jobs.snapshot(jobs.test_owner, 'team'))['status'] == 'running'
    await jobs.settle(b, StepOutcome('completed'), validated=True)
    join = await jobs.claim('join', {'fake'})
    assert join.step_id == 'summary'


async def test_unknown_checkpoint_version_is_not_executed(jobs):
    await submit(jobs)
    async with jobs.factory() as db:
        step = await db.get(Step, ('job', 'read'))
        step.checkpoint_version = 999
        await db.commit()
    assert await jobs.claim('worker', {'fake'}) is None
    assert (await jobs.snapshot(jobs.test_owner, 'job'))['reason'] == 'unsupported_checkpoint'


@pytest.mark.parametrize('status', ['paused', 'awaiting_input', 'failed'])
async def test_suspension_fences_parallel_worker_and_preserves_checkpoint(jobs, status):
    await jobs.submit('parallel', jobs.test_owner, 'session', 'privateclaw', [
        {'id': 'a', 'executor': 'fake'}, {'id': 'b', 'executor': 'fake'}])
    first = await jobs.claim('first', {'fake'})
    second = await jobs.claim('second', {'fake'})
    await jobs.checkpoint(second, {'validated_cursor': 7})
    jobs.test_clock[0] += 3
    await jobs.settle(first, StepOutcome(status, reason='test_suspend'))
    with pytest.raises(LeaseLost):
        await jobs.checkpoint(second, {'late': True})
    with pytest.raises(LeaseLost):
        await jobs.settle(second, StepOutcome('completed'), validated=True)
    async with jobs.factory() as db:
        step = await db.get(Step, ('parallel', second.step_id))
        assert step.status == 'queued' and not step.worker_id
        assert jobs.unpack(step.checkpoint) == {'validated_cursor': 7}
        attempt = await db.get(Attempt, ('parallel', second.step_id, second.fence))
        assert attempt.status == 'interrupted'
        assert (await db.get(Job, 'parallel')).active_seconds == 6
    assert await jobs.claim('third', {'fake'}) is None


async def test_suspension_never_requeues_unprotected_external_sibling(jobs):
    await jobs.submit('parallel', jobs.test_owner, 'session', 'sbot', [
        {'id': 'a', 'executor': 'fake'}, {'id': 'b', 'executor': 'fake', 'effect': 'external'}])
    first = await jobs.claim('first', {'fake'})
    second = await jobs.claim('second', {'fake'})
    await jobs.settle(first, StepOutcome('paused', reason='permission_denied'))
    async with jobs.factory() as db:
        step = await db.get(Step, ('parallel', second.step_id))
        assert step.status == 'paused' and step.reason == 'uncertain_effect'


async def test_worker_recovers_database_disconnect_without_exit(jobs, monkeypatch):
    from sqlalchemy.exc import OperationalError
    worker = Worker(jobs, {}, allow, total=1)
    calls = []
    async def run_once():
        calls.append(True)
        if len(calls) == 1:
            raise OperationalError('connection lost', None, Exception('offline'))
        worker.stopping.set()
        return True
    monkeypatch.setattr(worker, 'run_once', run_once)
    await asyncio.wait_for(worker.serve(), timeout=4)
    assert len(calls) == 2


async def test_time_limit_fences_all_parallel_attempts(jobs):
    await jobs.submit('parallel', jobs.test_owner, 'session', 'sbot', [
        {'id': 'a', 'executor': 'fake'}, {'id': 'b', 'executor': 'fake'}], policy={'max_seconds': 1})
    first = await jobs.claim('first', {'fake'})
    second = await jobs.claim('second', {'fake'})
    jobs.test_clock[0] += 2
    assert not await jobs.heartbeat(first)
    with pytest.raises(LeaseLost):
        await jobs.reserve(second, 'late-call', 'model', 1)
    async with jobs.factory() as db:
        live = list((await db.scalars(select(Step).where(Step.status == 'running'))).all())
        assert not live


async def test_dependency_expiry_fences_parallel_worker(jobs):
    await jobs.submit('parallel', jobs.test_owner, 'session', 'sbot', [
        {'id': 'a', 'executor': 'fake'}, {'id': 'b', 'executor': 'fake'}],
        policy={'dependency_wait_seconds': 10})
    first = await jobs.claim('first', {'fake'})
    second = await jobs.claim('second', {'fake'})
    await jobs.settle(first, StepOutcome('dependency', dependency='sandbox'))
    jobs.test_clock[0] += 11
    await jobs.dependency_result('parallel', first.step_id, first.fence, False)
    with pytest.raises(LeaseLost):
        await jobs.checkpoint(second, {})
    assert (await jobs.snapshot(jobs.test_owner, 'parallel'))['reason'] == 'dependency_wait_expired'


async def test_foreground_adoption_is_atomic_idempotent_and_preserves_limits(jobs, stores):
    from claw.db.models import UsageRecord
    session = await stores['sessions'].create(jobs.test_owner)
    adoption = {'active_seconds': 12.5, 'checkpoint': {'runtime': {'written': ['existing.csv']}},
                'calls': [{'id': 'known', 'model': 'model', 'reserved': 50, 'actual': 30,
                           'usage': {'prompt_tokens': 20, 'completion_tokens': 10}},
                          {'id': 'unknown', 'model': 'model', 'reserved': 80, 'actual': None}]}
    steps = [{'id': 'task', 'executor': 'fake'}]
    for _ in range(2):
        await jobs.submit('adopted', jobs.test_owner, session.id, 'privateclaw', steps,
                          policy={'max_tokens': 100}, adoption=adoption)
    # Unknown usage is retained even when it already exceeds the available budget.
    assert await jobs.claim('worker', {'fake'}) is None
    async with jobs.factory() as db:
        job = await db.get(Job, 'adopted')
        assert (job.tokens, job.reserved_tokens, job.active_seconds) == (30, 80, 12.5)
        assert job.status == 'paused' and job.reason == 'resource_limit'
        assert len(list((await db.scalars(select(ResourceCall))).all())) == 2
        assert len(list((await db.scalars(select(UsageRecord))).all())) == 1
        step = await db.get(Step, ('adopted', 'task'))
        assert jobs.unpack(step.checkpoint) == adoption['checkpoint']
    assert not await jobs.control('adopted', jobs.test_owner, 'resume')
    # Reconcile, then resume; neither operation resets the original spend.
    await jobs.reconcile('unknown', 10, job_id='adopted')
    assert await jobs.control('adopted', jobs.test_owner, 'resume')
    lease = await jobs.claim('replacement', {'fake'})
    assert lease.checkpoint == adoption['checkpoint']
    with pytest.raises(BudgetExhausted):
        await jobs.reserve(lease, 'new', 'model', 61)


async def test_foreground_adoption_rejects_wrong_owner_without_partial_ledger(jobs, stores):
    other = await stores['users'].get_or_create_by_email('other-adoption@example.test')
    session = await stores['sessions'].create(other.id)
    with pytest.raises(PermissionError):
        await jobs.submit('wrong', jobs.test_owner, session.id, 'privateclaw',
            [{'id': 'task', 'executor': 'fake'}], adoption={
                'active_seconds': 1, 'checkpoint': {},
                'calls': [{'id': 'attempted', 'model': 'model', 'reserved': 20, 'actual': None}]})
    async with jobs.factory() as db:
        assert await db.get(Job, 'wrong') is None
        assert await db.get(ResourceCall, 'attempted') is None

def test_worker_process_contract_is_structural_across_module_identities():
    from types import SimpleNamespace
    from claw.jobs.worker import valid_worker_process_contract

    candidate = SimpleNamespace(
        serve=lambda: None,
        run_once=lambda: None,
        stopping=asyncio.Event(),
    )
    assert valid_worker_process_contract(candidate)
    assert not valid_worker_process_contract(SimpleNamespace(serve=lambda: None))
