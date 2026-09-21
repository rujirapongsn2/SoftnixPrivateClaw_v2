"""Opt-in PostgreSQL acceptance; each test owns a fresh, disposable schema.

DURABLE_TEST_DATABASE_URL must reference an isolated test database. These fixtures
never use application settings or migrate/drop the public schema.
"""
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from claw.db.models import Base
import claw.jobs.models  # noqa: F401
import sbot.db.models  # noqa: F401
from claw.db.stores import AuditStore, MemoryStore, MessageStore, SessionStore, UserStore


@pytest.fixture
async def db_factory():
    url = os.environ.get('DURABLE_TEST_DATABASE_URL')
    if not url:
        pytest.skip('isolated PostgreSQL URL not provided')
    if not url.startswith('postgresql+asyncpg://'):
        pytest.fail('PostgreSQL asyncpg test URL required')
    schema = 'durable_test_' + uuid.uuid4().hex
    admin = create_async_engine(url)
    engine = None
    try:
        async with admin.begin() as db:
            await db.execute(text(f'CREATE SCHEMA {schema}'))
        engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if engine:
            await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
        await admin.dispose()


@pytest.fixture
async def stores(db_factory):
    return {'users': UserStore(db_factory), 'sessions': SessionStore(db_factory, is_postgres=True),
            'messages': MessageStore(db_factory, is_postgres=True), 'memories': MemoryStore(db_factory),
            'audit': AuditStore(db_factory, is_postgres=True)}
