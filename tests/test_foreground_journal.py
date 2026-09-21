"""Durable pre-admission accounting, using real encrypted DB transactions."""
import asyncio
import os
import sys

import pytest

from claw.jobs.foreground import recoverable, save
from claw.jobs.models import ForegroundJournal, Job
from claw.jobs.provider import ForegroundAccounting, AccountedProvider, foreground_accounting
from claw.jobs.store import JobStore, LeaseLost
from claw.security.crypto import SecretBox
from tests.conftest import FakeProvider
from claw.providers.base import ChatResult


async def setup(stores, db_factory):
    store = JobStore(db_factory, SecretBox('journal-test'))
    await store.initialize()
    user = await stores['users'].get_or_create_by_email('journal@example.test')
    session = await stores['sessions'].create(user.id)
    return store, user.id, session.id


async def test_reservation_commits_before_provider_and_usage_before_result(stores, db_factory):
    store, owner, session = await setup(stores, db_factory)
    tape = ForegroundAccounting(store, owner, session)

    class InspectProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            rows = await recoverable(store, owner)
            assert len(rows[0]['payload']['calls']) == 1
            assert rows[0]['payload']['calls'][0]['actual'] is None
            yield ChatResult(content='ok', usage={'prompt_tokens': 8, 'completion_tokens': 2})
    token = foreground_accounting.set(tape)
    try:
        async for _ in AccountedProvider(InspectProvider([])).stream_chat([{'role': 'user', 'content': 'hi'}]):
            rows = await recoverable(store, owner)
            assert rows[0]['payload']['calls'][0]['actual'] == 10
    finally:
        foreground_accounting.reset(token)
    await tape.close()
    assert not await recoverable(store, owner)


async def test_process_death_preserves_unknown_reservation(stores, db_factory):
    store, owner, session = await setup(stores, db_factory)
    script = '''
import asyncio, os, sys
from claw.db.engine import create_engine_and_factory
from claw.jobs.provider import ForegroundAccounting
from claw.jobs.store import JobStore
from claw.security.crypto import SecretBox
async def main():
    engine, factory = create_engine_and_factory(sys.argv[1])
    tape = ForegroundAccounting(JobStore(factory, SecretBox('journal-test')), sys.argv[2], sys.argv[3])
    await tape.reserve_call('model', 700)
    os._exit(23)
asyncio.run(main())
'''
    url = str(db_factory.kw['bind'].url)
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', script, url, owner, session,
                                                env=os.environ.copy())
    assert await child.wait() == 23
    replacement = JobStore(db_factory, SecretBox('journal-test'))
    row = (await recoverable(replacement, owner))[0]
    assert row['payload']['calls'][0]['actual'] is None
    assert row['payload']['calls'][0]['reserved'] == 700
    assert await recoverable(replacement, 'other') == []
    adoption = {**row['payload'], 'journal_revision': row['revision']}
    await replacement.submit('recovered', owner, session, 'privateclaw',
        [{'id': 'task', 'executor': 'fake'}], adoption=adoption)
    async with db_factory() as db:
        root = await db.get(Job, 'recovered')
        assert root.reserved_tokens == 700 and root.tokens == 0
        journal = await db.get(ForegroundJournal, row['id'])
        assert journal.status == 'adopted'
    with pytest.raises(LeaseLost):
        await save(store, row['id'], owner, session, 'privateclaw', row['revision'], row['payload'])
    with pytest.raises(LeaseLost):
        await replacement.submit('duplicate-root', owner, session, 'privateclaw',
            [{'id': 'task', 'executor': 'fake'}], adoption=adoption)


async def test_stale_or_reduced_accounting_cannot_be_adopted(stores, db_factory):
    store, owner, session = await setup(stores, db_factory)
    tape = ForegroundAccounting(store, owner, session)
    await tape.reserve_call('model', 100)
    adoption = {**tape.snapshot(), 'checkpoint': {}}
    with pytest.raises(ValueError):
        await store.submit('lost-spend', owner, session, 'privateclaw',
            [{'id': 'task', 'executor': 'fake'}], adoption={**adoption, 'calls': []})
    await tape.checkpoint({'pending_tool': {'name': 'connector_write'}})
    with pytest.raises(LeaseLost):
        await store.submit('stale', owner, session, 'privateclaw',
            [{'id': 'task', 'executor': 'fake'}], adoption=adoption)
    assert not await store.list_jobs(owner)
    async with db_factory() as db:
        row = await db.get(ForegroundJournal, tape.journal_id)
        assert 'connector_write' not in row.payload  # encrypted at rest


async def test_journal_retention_preserves_accounting_until_audit_expiry(stores, db_factory):
    store, owner, session = await setup(stores, db_factory)
    clock = [1000.0]
    store.clock = lambda: clock[0]
    tape = ForegroundAccounting(store, owner, session)
    await tape.reserve_call('model', 90)
    await tape.checkpoint({'source': 'sensitive document'})
    clock[0] += 8 * 86400
    await store.prune()
    async with db_factory() as db:
        row = await db.get(ForegroundJournal, tape.journal_id)
        assert row.status == 'expired'
        assert store.unpack(row.payload)['checkpoint'] == {}
        assert store.unpack(row.payload)['calls'][0]['reserved'] == 90
    clock[0] += 23 * 86400
    await store.prune()
    async with db_factory() as db:
        assert await db.get(ForegroundJournal, tape.journal_id) is None


async def test_two_recovery_claims_cannot_create_two_roots(stores, db_factory):
    store, owner, session = await setup(stores, db_factory)
    tape = ForegroundAccounting(store, owner, session)
    await tape.reserve_call('model', 80)
    adoption = {**tape.snapshot(), 'checkpoint': {}}
    results = await asyncio.gather(*(store.submit(name, owner, session, 'privateclaw',
        [{'id': 'task', 'executor': 'fake'}], adoption=adoption)
        for name in ['recovery-a', 'recovery-b']), return_exceptions=True)
    assert sum(isinstance(result, LeaseLost) for result in results) == 1
    assert len(await store.list_jobs(owner)) == 1


@pytest.mark.parametrize('mode', ['privateclaw', 'sbot'])
async def test_terminal_delivery_atomic_retry_and_unknown_usage(stores, db_factory, mode):
    from sqlalchemy import select
    from claw.db.models import UsageRecord, UsageDaily
    from claw.jobs.foreground import deliver
    store, owner, session = await setup(stores, db_factory)
    if mode == 'sbot':
        from sbot.db.models import ChatSession, Message
        async with db_factory() as db, db.begin():
            room = ChatSession(user_id=owner, title='Bot')
            db.add(room)
            await db.flush()
            session = room.id
    else:
        from claw.db.models import Message
    tape = ForegroundAccounting(store, owner, session, mode)
    tape.count_plan_turn = True
    call = await tape.reserve_call('test', 100)
    await tape.record_usage(call, 10, usage={'prompt_tokens': 8, 'completion_tokens': 2})
    await tape.reserve_call('unknown', 200)
    await tape.checkpoint({'foreground_terminal': True, 'terminal_delivery': {
        'version': 1, 'entries': [{'role': 'assistant', 'content': 'Final', 'meta': {'artifacts': ['result.csv']}}]}})
    # Finally/close must not erase an undelivered terminal checkpoint.
    await tape.close()
    assert len(await recoverable(store, owner)) == 1
    results = await asyncio.gather(*(deliver(store, tape.journal_id, owner, tape.revision) for _ in range(2)))
    assert sorted(results) == [False, True]
    assert not await deliver(store, tape.journal_id, owner, tape.revision)
    async with db_factory() as db:
        messages = list((await db.scalars(select(Message).where(Message.session_id == session))).all())
        assert len(messages) == 1 and messages[0].content == 'Final'
        records = list((await db.scalars(select(UsageRecord).where(UsageRecord.user_id == owner))).all())
        assert len(records) == 1 and records[0].prompt_tokens == 8
        daily = (await db.scalars(select(UsageDaily).where(UsageDaily.user_id == owner))).one()
        assert daily.turns == 1 and daily.plan_turns == 1
        journal = await db.get(ForegroundJournal, tape.journal_id)
        assert store.unpack(journal.payload)['calls'][1]['actual'] is None
        assert store.unpack(journal.payload)['calls'][1]['reserved'] == 200
    await tape.close()


async def test_terminal_delivery_rollback_preserves_retry(stores, db_factory, monkeypatch):
    from sqlalchemy import select
    from claw.db.models import Message, UsageRecord
    from claw.jobs.foreground import deliver
    store, owner, session = await setup(stores, db_factory)
    tape = ForegroundAccounting(store, owner, session)
    call = await tape.reserve_call('test', 100)
    await tape.record_usage(call, 10, usage={'prompt_tokens': 8, 'completion_tokens': 2})
    await tape.checkpoint({'foreground_terminal': True, 'terminal_delivery': {
        'version': 1, 'entries': [{'role': 'assistant', 'content': 'Final'}]}})
    original = store._record_usage

    async def crash(*args):
        await original(*args)
        raise RuntimeError('before commit')
    monkeypatch.setattr(store, '_record_usage', crash)
    with pytest.raises(RuntimeError, match='before commit'):
        await deliver(store, tape.journal_id, owner, tape.revision)
    async with db_factory() as db:
        assert not list((await db.scalars(select(Message).where(Message.session_id == session))).all())
        assert not list((await db.scalars(select(UsageRecord).where(UsageRecord.user_id == owner))).all())
    monkeypatch.setattr(store, '_record_usage', original)
    assert await deliver(store, tape.journal_id, owner, tape.revision)
    await tape.close()


async def test_terminal_delivery_rechecks_owner_and_active_user(stores, db_factory):
    from claw.jobs.foreground import deliver
    from claw.db.models import User, ChatSession
    store, owner, session = await setup(stores, db_factory)
    tape = ForegroundAccounting(store, owner, session)
    await tape.checkpoint({'foreground_terminal': True, 'terminal_delivery': {
        'version': 1, 'entries': [{'role': 'assistant', 'content': 'Private result'}]}})
    with pytest.raises(PermissionError):
        await deliver(store, tape.journal_id, 'another-owner', tape.revision)
    async with db_factory() as db, db.begin():
        user = await db.get(User, owner)
        user.is_active = False
    with pytest.raises(PermissionError):
        await deliver(store, tape.journal_id, owner, tape.revision)
    other = await stores['users'].get_or_create_by_email('other-terminal@test.dev')
    async with db_factory() as db, db.begin():
        user = await db.get(User, owner)
        user.is_active = True
        room = await db.get(ChatSession, session)
        room.user_id = other.id
    with pytest.raises(PermissionError):
        await deliver(store, tape.journal_id, owner, tape.revision)
    await tape.close()
