"""Bot mission DAG adapter; the shared worker is the sole executor for these jobs."""
from dataclasses import asdict

from claw.jobs.contracts import JobPolicy, StepOutcome, digest
from claw.jobs.models import Step
from claw.jobs.runtime_bridge import Handoff, file_evidence


def prepared_graph(mission, nodes):
    fields = ('id', 'bot_id', 'kind', 'title', 'instruction', 'depends_on', 'budget', 'max_attempts')
    return digest([mission.id, mission.owner_id, mission.session_id, mission.goal,
                   [{key: getattr(node, key) for key in fields} for node in sorted(nodes, key=lambda n: n.id)]])


async def enqueue(service, mission, *, adoption=None, orphan_before=None):
    nodes = await service.missions.get_nodes(mission.id)
    session = await service.sessions.get(mission.session_id) if mission.session_id else None
    if not session or session.user_id != mission.owner_id:
        raise PermissionError('mission session ownership mismatch')
    limits = service.settings.team_work
    steps = [dict(id=n.id, executor='sbot.node', actor=n.bot_id,
                  depends_on=n.depends_on or [], effect='local', replay_safe=True,
                  inputs={'mission_id': mission.id}, acceptance={'kind': 'task_result'}) for n in nodes]
    from claw.i18n import locale_for_text
    from claw.jobs.provider import foreground_accounting
    tape = foreground_accounting.get()
    locale = (adoption or {}).get('checkpoint', {}).get('request', {}).get('locale')
    if not locale and tape is not None:
        locale = tape.state.get('request', {}).get('locale')
    graph = prepared_graph(mission, nodes)
    if adoption is None and tape is not None and tape.calls:
        if tape.adopted_job_id is not None:
            raise ValueError('foreground calls already belong to a root job')
        await tape.checkpoint({**tape.state, 'bot_admission': {'mission_id': mission.id, 'graph': graph}})
        adoption = {**tape.snapshot(), 'checkpoint': {}}
    if orphan_before is not None:
        expected = adoption['checkpoint']['bot_admission']['graph']
        if expected != graph:
            raise ValueError('prepared mission changed before recovery')
        adoption = {**adoption, 'checkpoint': {}}
    await service.durable_jobs.submit(mission.id, mission.owner_id, mission.session_id, 'sbot', steps,
        policy=JobPolicy(max_tokens=limits.max_job_tokens, max_seconds=limits.max_job_seconds,
                         max_recoveries=limits.max_step_recoveries).model_dump(),
        locale=locale or locale_for_text('en', mission.goal), adoption=adoption,
        orphan_before=orphan_before, prepared_mission=graph)
    if adoption is not None and tape is not None:
        tape.adopted_job_id = mission.id


async def execute(service, ctx):
    from sbot.core.mission_engine import NodeContext
    from sbot.db.models import Mission, MissionNode
    if ctx.state.get('candidate'):
        return StepOutcome(**ctx.state['candidate'])
    if ctx.state.get('pending_tool'):
        return StepOutcome('paused', checkpoint=ctx.state, reason='uncertain_effect')
    async with ctx.store.transaction() as db:
        await ctx.store._guard(db, ctx.lease)
        mission = await db.get(Mission, ctx.lease.job_id)
        node = await db.get(MissionNode, {'mission_id': ctx.lease.job_id, 'id': ctx.lease.step_id})
        if not mission or not node or mission.owner_id != ctx.lease.owner_id or mission.status == 'cancelled':
            raise PermissionError('mission unavailable')
        mission.status = 'running'
        node.status, node.attempts = 'running', ctx.lease.fence
    parents = {}
    async with ctx.store.factory() as db:
        for parent in ctx.lease.spec.depends_on:
            step = await db.get(Step, (ctx.lease.job_id, parent))
            parents[parent] = ctx.store.unpack(step.evidence).get('output', '')
    context = NodeContext(await service.missions.blackboard_manifest(mission.id), parents, ctx.lease.fence)
    try:
        result = await service._executor_for(mission)(node, context)
    except Handoff as signal:
        return signal.outcome
    contract = result.task_result or {}
    root = service.settings.workspaces_root / ctx.lease.owner_id
    if result.status != 'done':
        status = 'yielded' if contract.get('failure_reason') in {'timeout', 'output_limit', 'iteration_limit'} else 'paused'
        evidence = {}
        if status == 'yielded':
            from claw.jobs.runtime_bridge import progress_evidence
            evidence = progress_evidence(root, ctx.state)
        return StepOutcome(status, checkpoint=ctx.state, evidence=evidence,
                           reason=contract.get('failure_reason') or 'validation_failed')
    evidence = {**file_evidence(root, result.artifacts), 'contract': contract, 'output': result.output}
    bot = await service.bots.get(node.bot_id, ctx.lease.owner_id)
    session = await service.sessions.thread_for_bot(ctx.lease.owner_id, bot.id, title=bot.name)
    if node.id == '__summary':
        session = await service.sessions.get(mission.session_id)
    outcome = StepOutcome('completed', checkpoint=ctx.state, evidence=evidence,
        delivery={'session_id': session.id, 'content': result.output,
                  'meta': {'artifacts': result.artifacts, 'validation_scope': evidence['validation_scope']}})
    await ctx.checkpoint({**ctx.state, 'candidate': asdict(outcome)})
    return outcome


async def validate(service, ctx, result):
    root = service.settings.workspaces_root / ctx.lease.owner_id
    if result.status == 'yielded':
        from claw.jobs.runtime_bridge import validate_progress
        return validate_progress(root, ctx.state, result.evidence)
    if result.status != 'completed':
        return False
    contract = result.evidence.get('contract', {})
    if contract.get('status') != 'completed' or contract.get('verification_status') == 'failed':
        return False
    structural = bool(result.evidence.get('files'))
    if ctx.lease.step_id == '__summary' and ctx.lease.spec.depends_on:
        async with ctx.store.factory() as db:
            parents = [await db.get(Step, (ctx.lease.job_id, parent)) for parent in ctx.lease.spec.depends_on]
            structural = all(parent and parent.status == 'completed' for parent in parents)
    if contract.get('verification_status') != 'passed' and not structural:
        return False
    try:
        return file_evidence(root, [r['path'] for r in result.evidence['files']])['files'] == result.evidence['files']
    except Exception:
        return False
