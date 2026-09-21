"""Async engine/session factory."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from claw.db.models import Base
import sbot.db.models  # noqa: F401 — register isolated mode tables
import claw.jobs.models  # noqa: F401 — register shared durable-job tables


def create_engine_and_factory(database_url: str):
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
