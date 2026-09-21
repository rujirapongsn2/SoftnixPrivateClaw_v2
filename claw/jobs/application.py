"""Application worker factory. Never starts HTTP/cron/Telegram lifespans."""

from claw.jobs.models import Job
from claw.jobs.runtime_bridge import normal_execute, file_evidence
from claw.jobs.worker import Worker, Executor


async def create_worker(app=None):
    from claw.main import create_app
    from claw.config import load_settings
    settings = app.state.claw.settings if app is not None else load_settings()
    if not (settings.durable_jobs_privateclaw or settings.durable_jobs_sbot):
        raise RuntimeError('durable jobs are disabled; enable the intended dev mode explicitly')
    app = app or create_app(settings)
    state = app.state.claw
    store = state.jobs
    # Schema migration belongs to the operator/API startup, not racing workers.
    await store.initialize()

    async def authorize(owner, mode, job_id):
        user = await state.users.get(owner)
        if not user or not user.is_active:
            return False
        async with store.factory() as db:
            job = await db.get(Job, job_id)
        if not job or job.owner_id != owner or job.mode != mode:
            return False
        selected = state if mode == 'privateclaw' else getattr(app.state, 'sbot', None)
        if selected is None:
            return False
        session = await selected.sessions.get(job.session_id)
        if not session or session.user_id != owner:
            return False
        if mode == 'sbot':
            service = selected.runtime.missions
            members = await service._current_members(owner, job.session_id)
            snapshot = await store.snapshot(owner, job_id)
            for step in snapshot['steps']:
                if step['status'] == 'completed':
                    continue
                bot = await selected.bots.get(step['actor'], owner)
                if bot is None or bot.is_archived or members is not None and bot.id not in members:
                    return False
        # Each execution reloads the current organization policy. No stale policy
        # snapshot is silently used after an administrator changes access.
        from claw.security.policy import rule_from_row, DEFAULT_TOOL_ARGS_EXEMPT
        rules = await state.guardrails.list_rules()
        state.policy.reload([rule_from_row(r) for r in rules],
            monitor_only=await state.guardrails.get_monitor_only(default=not settings.policy_enforce),
            tool_args_exempt=await state.guardrails.get_tool_args_exempt(default=list(DEFAULT_TOOL_ARGS_EXEMPT)))
        return True

    async def validate(ctx, result):
        if result.status == 'yielded':
            from claw.jobs.runtime_bridge import validate_progress
            root = state.settings.workspaces_root / ctx.lease.owner_id
            return validate_progress(root, ctx.state, result.evidence)
        if result.status != 'completed':
            return False
        from claw.jobs.runtime_bridge import sources_unchanged
        root = state.settings.workspaces_root / ctx.lease.owner_id
        if not sources_unchanged(root, ctx.state):
            return False
        if ctx.lease.spec.acceptance.get('kind') == 'research':
            from claw.jobs.research import research_evidence
            return bool(result.delivery) and research_evidence(ctx.state, result.delivery.get('content', '')) == result.evidence
        try:
            return bool(result.evidence.get('files')) and file_evidence(root,
                [r['path'] for r in result.evidence['files']]) == result.evidence
        except Exception:
            return False

    async def probe(owner, job_id):
        root = state.runtime.get_agent(owner).workspace
        result = await state.runtime.sandbox.run('true', root)
        return result.exit_code == 0 and not result.timed_out

    async def execute(ctx):
        return await normal_execute(state.runtime, ctx)

    executors = {}
    if settings.durable_jobs_privateclaw:
        executors['privateclaw.turn'] = Executor(execute, validate)
    if settings.durable_jobs_sbot:
        from claw.jobs import bot_bridge
        from functools import partial
        await app.state.sbot.project_containers.load()
        service = app.state.sbot.runtime.missions
        executors['sbot.node'] = Executor(partial(bot_bridge.execute, service), partial(bot_bridge.validate, service))
    async def connector_authorize(lease, name):
        if not await authorize(lease.owner_id, lease.mode, lease.job_id):
            return False
        selected = state if lease.mode == 'privateclaw' else app.state.sbot
        manager = selected.runtime.connectors
        if manager is None:
            return False
        return any(c.name == name for c in await manager.store.enabled_accessible(lease.owner_id))

    worker = Worker(store, executors, authorize, probes={('privateclaw', 'sandbox'): probe},
                    connector_authorize=connector_authorize)
    async def connector_probe(mode, owner, job_id, key):
        from sqlalchemy import select
        from claw.jobs.models import Step
        from claw.jobs.contracts import digest
        async with store.factory() as db:
            rows = (await db.scalars(select(Step).where(Step.job_id == job_id,
                Step.dependency == 'connector:' + key, Step.status == 'waiting_dependency'))).all()
            names = {store.unpack(step.checkpoint).get('waiting_connector') for step in rows}
        names = {name for name in names if isinstance(name, str) and digest(name)[:40] == key}
        if len(names) != 1:
            return False
        name = names.pop()
        selected = state if mode == 'privateclaw' else app.state.sbot
        manager = selected.runtime.connectors
        if manager is None:
            return False
        if not any(c.name == name for c in await manager.store.enabled_accessible(owner)):
            return False
        # Reconnect using current stored credentials, never checkpointed secrets.
        from claw.tools.registry import ToolRegistry
        registry = ToolRegistry()
        await manager.sync_tools(owner, registry)
        return any(getattr(registry.get(tool), '_connector_name', None) == name
                   for tool in registry.tool_names)
    from functools import partial
    worker.probes[('privateclaw', 'connector')] = partial(connector_probe, 'privateclaw')
    # Keep application resources alive for worker lifetime without starting a
    # second mission scheduler or chat/API service.
    if settings.durable_jobs_sbot:
        async def bot_probe(owner, job_id):
            result = await app.state.sbot.runtime.sandbox.run('true', app.state.sbot.runtime.get_agent(owner).workspace)
            return result.exit_code == 0 and not result.timed_out
        worker.probes[('sbot', 'sandbox')] = bot_probe
        worker.probes[('sbot', 'connector')] = partial(connector_probe, 'sbot')
    from claw.jobs.recovery import recover_once
    runtimes = {}
    if settings.durable_jobs_privateclaw:
        runtimes['privateclaw'] = state.runtime
    if settings.durable_jobs_sbot:
        runtimes['sbot'] = app.state.sbot.runtime
    worker.recover_foreground = partial(recover_once, store, runtimes)
    worker.application = app
    return worker
