"""Shared FastAPI app builder for API-level tests (auth + admin + manage routers)."""

import httpx
from fastapi import FastAPI

from claw.api.admin import router as admin_router
from claw.api.auth import router as auth_router
from claw.api.deps import AppState
from claw.api.manage import router as manage_router
from claw.api.routes import router as core_router
from claw.api.telegram import router as telegram_router
from claw.channels.link import LinkCodeService
from claw.config import Settings
from claw.core.connectors import ConnectorManager
from claw.db.stores import (
    AuditStore,
    ConnectorStore,
    FeedbackStore,
    MemoryStore,
    MessageStore,
    ScheduleStore,
    SessionStore,
    SkillStore,
    UsageStore,
    UserStore,
)
from claw.security.policy import PolicyEngine


def build_api_app(db_factory, **settings_kwargs) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(manage_router)
    app.include_router(telegram_router)
    app.include_router(core_router)
    app.state.claw = AppState(
        # _env_file=None keeps tests hermetic — never read the developer's .env.
        settings=Settings(dev_token="t", secret_key="test-secret", _env_file=None, **settings_kwargs),
        runtime=None,
        bus=None,
        users=UserStore(db_factory),
        sessions=SessionStore(db_factory),
        messages=MessageStore(db_factory),
        skills=SkillStore(db_factory),
        memories=MemoryStore(db_factory),
        connectors=ConnectorStore(db_factory),
        connectors_mgr=ConnectorManager(ConnectorStore(db_factory)),
        schedules=ScheduleStore(db_factory),
        scheduler=None,
        policy=PolicyEngine(),
        telegram_link=LinkCodeService(),
        usage=UsageStore(db_factory),
        feedback=FeedbackStore(db_factory),
        telegram=None,
    )
    return app


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
