"""Application entry point: wire settings → stores → runtime → API."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sbot.api.blueprints import router as blueprints_router
from sbot.api.bot_groups import router as bot_groups_router
from sbot.api.deps import AppState
from sbot.api.manage import router as manage_router
from sbot.api.routes import router
from sbot.config import Settings, load_settings
from sbot.core.bus import EventBus
from sbot.core.heartbeat import HeartbeatService
from sbot.core.limits import RateLimiter
from sbot.core.memory import MemoryService
from sbot.core.missions import MissionService
from sbot.core.project_access import ProjectAccessPolicy
from sbot.core.runtime import AgentRuntime
from sbot.core.scheduler import SchedulerService
from sbot.db.stores import (
    BlueprintStore,
    BotStore,
    FeedbackStore,
    MemoryStore,
    MessageStore,
    MissionStore,
    ScheduleStore,
    SessionStore,
    ShareStore,
)
from sbot.logging_setup import configure_logging


def create_app(settings: Settings | None = None, *, shared=None) -> FastAPI:
    if shared is None:
        raise RuntimeError("Sbot mode must be started through claw.main:create_app")
    settings = settings or load_settings()
    # Before anything that can raise: an unconfigured loguru dumps local
    # variables (database URL, prompts, message text) into every traceback.
    configure_logging(settings.log)
    factory = shared.users.factory
    provider = shared.runtime.provider
    bus = EventBus()
    is_postgres = "postgresql" in settings.database_url
    bots = BotStore(factory)
    missions = MissionStore(factory)
    sessions = SessionStore(factory, is_postgres=is_postgres)
    messages = MessageStore(factory, is_postgres=is_postgres)
    memories = MemoryStore(factory)
    feedback = FeedbackStore(factory)
    schedules = ScheduleStore(factory)
    blueprints = BlueprintStore(factory)
    shares = ShareStore(factory)
    users = shared.users
    groups = shared.groups
    audit = shared.audit
    usage = shared.usage
    skills = shared.skills
    connectors = shared.connectors
    connectors_mgr = shared.connectors_mgr
    guardrails = shared.guardrails
    llm_config = shared.llm_config
    oauth_apps = shared.oauth_apps
    smtp_config = shared.smtp_config
    knowledge = shared.knowledge
    knowledge_service = shared.knowledge_service
    plans = shared.plans
    branding = shared.branding
    policy = shared.policy
    browser_broker = shared.browser_broker
    project_access = ProjectAccessPolicy(users)
    browser_mgr = None
    if settings.browser.enabled:
        from sbot.browser.manager import BrowserManager
        from sbot.browser.playwright_backend import PlaywrightBrowser

        browser_mgr = BrowserManager(
            PlaywrightBrowser(settings.browser), settings.browser, settings.workspaces_root
        )

    memory_service = MemoryService(
        memories,
        messages,
        sessions,
        provider,
        model=settings.llm.model,
        window=settings.memory.window,
        keep=settings.memory.keep,
        is_postgres=is_postgres,
        usage=usage,
        policy=policy,
    )
    runtime = AgentRuntime(
        settings=settings,
        provider=provider,
        bus=bus,
        users=users,
        sessions=sessions,
        messages=messages,
        memory=memory_service,
        audit=audit,
        bots=bots,
        skills=skills,
        connectors=connectors_mgr,
        policy=policy,
        browser=browser_mgr,
        usage=usage,
        schedules=schedules,
        llm_config=llm_config,
        plans=plans,
        browser_broker=browser_broker,
        knowledge=knowledge,
        project_access=project_access,
        blueprints=blueprints,
    )
    from sbot.local_workspaces import LocalWorkspaces
    runtime.local_workspaces = LocalWorkspaces(settings.workspaces_root / '_local_workspaces')

    if shared is not None:
        runtime._rate_limiter = shared.runtime._rate_limiter

    async def _scheduled_turn(user_id: str, session_id: str, prompt: str) -> str | None:
        return await runtime.handle_message(user_id, session_id, prompt, channel="schedule")

    scheduler = SchedulerService(schedules, sessions, _scheduled_turn, timezone=settings.scheduler.timezone)
    # The schedule tool needs the scheduler to wake it on changes; wire it back now
    # that both exist (scheduler depends on the runtime's turn handler).
    runtime.scheduler = scheduler

    # Same shape: mission nodes run specialists in the runtime's sandbox, so the
    # service is built after the runtime and wired back before any turn can
    # construct an agent.
    async def _mission_report(user_id: str, session_id: str, prompt: str) -> str | None:
        return await runtime.handle_message(user_id, session_id, prompt, channel="mission")

    mission_service = MissionService(
        missions,
        bots,
        local_broker=runtime.local_workspaces,
        provider=provider,
        sandbox=runtime.sandbox,
        settings=settings,
        llm_config=llm_config,
        skills=skills,
        memory=memory_service,
        notifier=_mission_report,
        max_parallel_nodes=settings.team_work.max_parallel_total,
        messages=messages,
        bus=runtime.bus,
        sessions=sessions,
        arg_guard_for_owner=lambda owner: runtime.get_agent(owner)._guard_tool_args,
        connectors=runtime.connectors,
        project_access=project_access,
    )
    runtime.missions = mission_service

    async def _heartbeat_turn(user_id: str, session_id: str, prompt: str) -> str | None:
        return await runtime.handle_message(user_id, session_id, prompt, channel="heartbeat")

    from claw.modes import SbotHeartbeatUsers

    heartbeat = HeartbeatService(
        SbotHeartbeatUsers(users), memories, sessions, provider, _heartbeat_turn, model=settings.llm.model
    )

    telegram_link = shared.telegram_link
    telegram_config = shared.telegram_config
    telegram_mgr = shared.telegram_mgr

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.blueprints_root.mkdir(parents=True, exist_ok=True)
        scheduler.start()
        heartbeat.start()
        maintenance = None
        try:
            await mission_service.resume_interrupted()
            maintenance = asyncio.create_task(mission_service.maintenance())
            yield
        finally:
            await heartbeat.stop()
            await scheduler.stop()
            if maintenance is not None:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
            await mission_service.stop()
            await runtime.drain()
            if browser_mgr is not None:
                await browser_mgr.close()

    app = FastAPI(title="Softnix Sbot", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.sbot = AppState(
        settings=settings,
        runtime=runtime,
        bus=bus,
        users=users,
        bots=bots,
        groups=groups,
        sessions=sessions,
        messages=messages,
        skills=skills,
        memories=memories,
        missions=missions,
        mission_service=mission_service,
        connectors=connectors,
        connectors_mgr=connectors_mgr,
        schedules=schedules,
        scheduler=scheduler,
        policy=policy,
        telegram_link=telegram_link,
        usage=usage,
        feedback=feedback,
        telegram=None,
        telegram_config=telegram_config,
        telegram_mgr=telegram_mgr,
        smtp_config=smtp_config,
        plans=plans,
        branding=branding,
        image_rate_limiter=shared.image_rate_limiter if shared else RateLimiter(settings.image.per_minute),
        tts_rate_limiter=shared.tts_rate_limiter if shared else RateLimiter(settings.tts.per_minute),
        guardrails=guardrails,
        llm_config=llm_config,
        audit=audit,
        oauth_apps=oauth_apps,
        browser_broker=browser_broker,
        knowledge=knowledge,
        knowledge_service=knowledge_service,
        blueprints=blueprints,
        shares=shares,
        project_access=project_access,
    )
    app.include_router(router)
    app.include_router(manage_router)
    app.include_router(blueprints_router)
    app.include_router(bot_groups_router)
    from sbot.api.local_workspaces import router as local_workspaces_router
    app.include_router(local_workspaces_router)

    return app


def run() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
