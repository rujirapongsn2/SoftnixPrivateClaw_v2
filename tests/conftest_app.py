"""Shared FastAPI app builder for API-level tests (auth + admin + manage routers)."""

import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI

from claw.api.admin import router as admin_router
from claw.api.auth import router as auth_router
from claw.api.browser_ext import router as browser_ext_router
from claw.api.deps import AppState
from claw.api.knowledge import router as knowledge_router
from claw.api.manage import router as manage_router
from claw.api.routes import router as core_router
from claw.api.telegram import router as telegram_router
from claw.browser.broker import BrowserBrokerStore
from claw.channels.link import LinkCodeService
from claw.channels.telegram import TelegramManager
from claw.config import Settings
from claw.core.connectors import ConnectorManager
from claw.db.stores import (
    AuditStore,
    ConnectorStore,
    FeedbackStore,
    GroupStore,
    GuardrailStore,
    KnowledgeStore,
    LLMConfigStore,
    MemoryStore,
    MessageStore,
    OAuthAppStore,
    ScheduleStore,
    SessionStore,
    ShareStore,
    SkillStore,
    TelegramConfigStore,
    UsageStore,
    UserStore,
)
from claw.knowledge.service import KnowledgeService
from claw.security.policy import PolicyEngine


def build_api_app(db_factory, **settings_kwargs) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(browser_ext_router)
    app.include_router(manage_router)
    app.include_router(telegram_router)
    app.include_router(core_router)
    app.include_router(knowledge_router)
    # Per-app temp root so browser-broker state never leaks across tests.
    broker_root = Path(tempfile.mkdtemp(prefix="claw-broker-")) / "_browser_broker"
    knowledge_store = KnowledgeStore(db_factory, is_postgres=False)
    knowledge_root = Path(tempfile.mkdtemp(prefix="claw-knowledge-"))
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
        guardrails=GuardrailStore(db_factory),
        llm_config=LLMConfigStore(db_factory),
        audit=AuditStore(db_factory),
        oauth_apps=OAuthAppStore(db_factory),
        browser_broker=BrowserBrokerStore(broker_root),
        knowledge=knowledge_store,
        knowledge_service=KnowledgeService(knowledge_store, knowledge_root),
        telegram_config=TelegramConfigStore(db_factory),
        telegram_mgr=TelegramManager(None, UserStore(db_factory), SessionStore(db_factory), LinkCodeService()),
        telegram=None,
        groups=GroupStore(db_factory),
        shares=ShareStore(db_factory),
    )
    return app


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
