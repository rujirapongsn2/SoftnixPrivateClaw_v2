"""Detect abandoned foreground journals; adoption remains one fenced transaction."""
import json
from sqlalchemy import select

from claw.jobs.contracts import JobPolicy, digest
from claw.jobs.models import ForegroundJournal
from claw.jobs.store import LeaseLost


def planned_kind(state):
    from claw.core.runtime import _is_artifact_task
    from claw.jobs.research import is_multistep_research
    messages = state.get('runtime', {}).get('checkpoint_messages', [])
    calls = {c.get('id'): c for m in messages for c in m.get('tool_calls', [])}
    for message in reversed(messages):
        if message.get('role') != 'tool' or message.get('name') != 'update_plan':
            continue
        if str(message.get('content', '')).startswith('Error'):
            continue
        args = calls.get(message.get('tool_call_id'), {}).get('function', {}).get('arguments', {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return None
        if not isinstance(args, dict):
            return None
        steps = args.get('steps', [])
        if not isinstance(steps, list) or sum(isinstance(s, dict) and s.get('status') != 'done' for s in steps) < 2:
            return None
        goal = str(args.get('goal', ''))
        return 'artifact' if _is_artifact_task(goal) else 'research' if is_multistep_research(goal) else None
    return None


async def recover_once(store, runtimes, grace_seconds=60):
    """No LLM/tools here. Unknown contracts/effects surface as paused shared jobs."""
    cutoff = store.clock() - grace_seconds
    async with store.factory() as db:
        rows = (await db.scalars(select(ForegroundJournal).where(
            ForegroundJournal.status == 'open', ForegroundJournal.updated_at <= cutoff,
            ForegroundJournal.mode.in_(list(runtimes)))
            .order_by(ForegroundJournal.updated_at).limit(100))).all()
    recovered = 0
    for row in rows:
        payload = store.unpack(row.payload)
        state = payload.get('checkpoint', {})
        request = state.get('request') or {}
        runtime = runtimes[row.mode]
        user = await runtime.users.get(row.owner_id)
        session = await runtime.sessions.get(row.session_id)
        allowed = user is not None and user.is_active and session is not None and session.user_id == row.owner_id
        if state.get('terminal_delivery') and allowed:
            from claw.jobs.foreground import deliver
            try:
                await deliver(store, row.id, row.owner_id, row.revision, orphan_before=cutoff)
            except LeaseLost:
                continue
            except PermissionError:
                allowed = False
            else:
                recovered += 1
                continue
        # A terminal marker is written before transcript delivery. That narrow
        # crash window needs delivery reconciliation, never another provider call
        # or a second global usage record. Preserve the journal for inspection.
        if session is None or session.user_id != row.owner_id or state.get('foreground_terminal'):
            async with store.transaction() as db:
                fresh = await db.get(ForegroundJournal, row.id)
                if (fresh and fresh.status == 'open' and fresh.revision == row.revision
                        and fresh.updated_at <= cutoff):
                    fresh.status = 'closed'
                    fresh.revision += 1
                    payload['recovery_reason'] = ('delivery_verification_required'
                        if state.get('foreground_terminal') and allowed else 'permission_denied')
                    fresh.payload = store.pack(payload)
            continue
        reason = ''
        kind = planned_kind(state) if row.mode == 'privateclaw' else None
        if state.get('pending_tool'):
            reason = 'uncertain_effect'
        elif state.get('foreground_terminal'):
            reason = 'delivery_verification_required'
        elif not request or kind is None:
            reason = 'recovery_contract_missing'
        if not allowed:
            reason = 'permission_denied'
        if row.mode == 'sbot':
            from sbot.core.organization_policy import load_policy
            await load_policy(runtime.sessions.factory, runtime.settings)
        configured = runtime.settings.team_work
        saved = await runtime.artifact_jobs.resource_policy() if getattr(runtime, 'artifact_jobs', None) else {}
        policy = JobPolicy(max_tokens=saved.get('max_job_tokens', configured.max_job_tokens),
            max_seconds=saved.get('max_job_seconds', configured.max_job_seconds),
            max_recoveries=saved.get('max_step_recoveries', configured.max_step_recoveries))
        # Normalize media before the worker uses the same normal runtime contract.
        args = {**request, 'session_id': row.session_id}
        if row.mode == 'privateclaw' and not reason:
            from pathlib import Path
            root = runtime.get_agent(row.owner_id).workspace.resolve()
            try:
                args['media'] = [str(Path(p).resolve().relative_to(root)) if Path(p).is_absolute() else p
                                 for p in args.get('media', [])]
            except ValueError:
                reason = 'permission_denied'
        job_id = digest('foreground:' + row.id)[:32]
        adoption = {**payload, 'journal_revision': row.revision, 'checkpoint': state,
            'active_seconds': payload['active_seconds'] + max(0, row.updated_at - payload.get('saved_at', row.updated_at))}
        if row.mode == 'sbot' and allowed and state.get('bot_admission'):
            from claw.jobs.bot_bridge import enqueue
            service = getattr(runtime, 'missions', None)
            prepared = state['bot_admission']
            mission = await service.missions.get_mission(prepared['mission_id'], row.owner_id) if service else None
            if mission and mission.session_id == row.session_id and mission.status == 'planned':
                members = await service._current_members(row.owner_id, row.session_id)
                nodes = await service.missions.get_nodes(mission.id)
                permitted = bool(nodes)
                for node in nodes:
                    if ((members is not None and node.bot_id not in members)
                            or await service.bots.get(node.bot_id, row.owner_id) is None):
                        permitted = False
                if permitted:
                    try:
                        await enqueue(service, mission, adoption=adoption, orphan_before=cutoff)
                    except LeaseLost:
                        continue
                    except ValueError:
                        reason = 'recovery_contract_missing'
                    else:
                        recovered += 1
                        continue
                else:
                    reason = 'permission_denied'
        try:
            await store.submit(job_id, row.owner_id, row.session_id, row.mode, [dict(
                id='task', executor='privateclaw.turn' if row.mode == 'privateclaw' else 'sbot.recovery',
                actor=request.get('actor') or '', inputs=args, acceptance={'kind': kind or 'unclassified'},
                effect='local', replay_safe=True)], policy=policy.model_dump(),
                locale=request.get('locale', 'en'), adoption=adoption,
                orphan_before=cutoff, pause_reason=reason)
        except LeaseLost:
            continue  # an API heartbeat or another worker won; never duplicate work
        recovered += 1
    return recovered
