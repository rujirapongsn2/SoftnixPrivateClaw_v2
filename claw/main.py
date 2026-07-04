"""Application entry point: wire settings → stores → runtime → API."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from claw.api.admin import router as admin_router
from claw.api.auth import router as auth_router
from claw.api.deps import AppState
from claw.api.manage import router as manage_router
from claw.api.routes import router
from claw.api.telegram import router as telegram_router
from claw.config import Settings, load_settings
from claw.channels.link import LinkCodeService
from claw.channels.telegram import HttpTelegramTransport, TelegramChannel
from claw.core.bus import EventBus
from claw.core.connectors import ConnectorManager
from claw.core.heartbeat import HeartbeatService
from claw.core.memory import MemoryService
from claw.core.runtime import AgentRuntime
from claw.core.scheduler import SchedulerService
from claw.security.policy import PolicyEngine
from claw.db.engine import create_engine_and_factory, init_db
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
from claw.providers.litellm_provider import LiteLLMProvider
from claw.security.crypto import SecretBox


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine, factory = create_engine_and_factory(settings.database_url)

    provider = LiteLLMProvider(
        api_key=settings.llm.api_key,
        api_base=settings.llm.api_base,
        default_model=settings.llm.model,
    )
    bus = EventBus()
    users = UserStore(factory)
    sessions = SessionStore(factory)
    messages = MessageStore(factory)
    memories = MemoryStore(factory)
    audit = AuditStore(factory)
    usage = UsageStore(factory)
    feedback = FeedbackStore(factory)
    skills = SkillStore(factory)
    secret_box = SecretBox(settings.secret_key)
    connectors = ConnectorStore(factory, secret_box=secret_box)
    schedules = ScheduleStore(factory)
    connectors_mgr = ConnectorManager(connectors)
    policy = PolicyEngine(monitor_only=not settings.policy_enforce)

    browser_mgr = None
    if settings.browser.enabled:
        from claw.browser.manager import BrowserManager
        from claw.browser.playwright_backend import PlaywrightBrowser

        browser_mgr = BrowserManager(
            PlaywrightBrowser(settings.browser), settings.browser, settings.workspaces_root
        )
    memory_service = MemoryService(memories, messages, sessions, provider, model=settings.llm.model)
    runtime = AgentRuntime(
        settings=settings,
        provider=provider,
        bus=bus,
        users=users,
        sessions=sessions,
        messages=messages,
        memory=memory_service,
        audit=audit,
        skills=skills,
        connectors=connectors_mgr,
        policy=policy,
        browser=browser_mgr,
        usage=usage,
        schedules=schedules,
    )

    async def _scheduled_turn(user_id: str, session_id: str, prompt: str) -> str | None:
        return await runtime.handle_message(user_id, session_id, prompt, channel="schedule")

    scheduler = SchedulerService(schedules, sessions, _scheduled_turn)
    # The schedule tool needs the scheduler to wake it on changes; wire it back now
    # that both exist (scheduler depends on the runtime's turn handler).
    runtime.scheduler = scheduler

    async def _heartbeat_turn(user_id: str, session_id: str, prompt: str) -> str | None:
        return await runtime.handle_message(user_id, session_id, prompt, channel="heartbeat")

    heartbeat = HeartbeatService(users, memories, sessions, provider, _heartbeat_turn,
                                 model=settings.llm.model)

    telegram_link = LinkCodeService()
    telegram: TelegramChannel | None = None
    if settings.telegram_bot_token:
        telegram = TelegramChannel(
            runtime, users, sessions,
            HttpTelegramTransport(settings.telegram_bot_token), telegram_link,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_migrate:
            import asyncio

            from claw.db.migrate import run_migrations

            try:
                await asyncio.to_thread(run_migrations)
            except Exception:
                logger.exception("Alembic migration failed; falling back to create_all")
                await init_db(engine)
        else:
            await init_db(engine)
        scheduler.start()
        heartbeat.start()
        if telegram is not None:
            telegram.start_task()
            logger.info("Telegram channel enabled")
        logger.info(
            "Softnix PrivateClaw up — model={}, sandbox={}, browser={}",
            settings.llm.model, runtime.sandbox.describe(),
            "enabled" if browser_mgr is not None else "disabled",
        )
        yield
        # Stop intake, then let in-flight turns finish before tearing down.
        await scheduler.stop()
        await heartbeat.stop()
        if telegram is not None:
            await telegram.stop()
        await runtime.drain()
        if browser_mgr is not None:
            await browser_mgr.close()
        await engine.dispose()

    app = FastAPI(title="Softnix PrivateClaw", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.claw = AppState(
        settings=settings,
        runtime=runtime,
        bus=bus,
        users=users,
        sessions=sessions,
        messages=messages,
        skills=skills,
        memories=memories,
        connectors=connectors,
        connectors_mgr=connectors_mgr,
        schedules=schedules,
        scheduler=scheduler,
        policy=policy,
        telegram_link=telegram_link,
        usage=usage,
        feedback=feedback,
        telegram=telegram,
    )
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(router)
    app.include_router(manage_router)
    app.include_router(telegram_router)

    # Serve the built web frontend from the same origin when present (prod image).
    # Mounted last so /api and /ws routes always take precedence.
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    web_dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="web")
        logger.info("Serving web frontend from {}", web_dist)

    return app


def run() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
