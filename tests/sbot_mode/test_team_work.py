"""Bounded background jobs through the real runtime, scheduler and result path."""

import asyncio
import json

import pytest

from sbot.core.mission_engine import InvalidGraphError
from sbot.core.missions import MissionService
from sbot.core.specialist import SpecialistOutcome, SpecialistRunner
from sbot.core.task_result import TaskResult
from sbot.core.turn_context import current_session_id, current_turn_id
from sbot.providers.base import ChatResult, ToolCall
from sbot.tools.team_work import TeamSubmitTool, TeamStatusTool, TeamCancelTool
from tests.sbot_mode.conftest import FakeProvider, text_turn
from tests.sbot_mode.test_multi_bot_turn import make_runtime


def call(name, **args):
    return ChatResult(content='', tool_calls=[ToolCall(id=name, name=name, arguments=args)],
                      usage={'prompt_tokens': 10, 'completion_tokens': 5})


def finish(summary):
    return call('finish_step', status='completed', summary=summary, evidence='Checked the requested output.', files=[])


async def setup_team(stores, provider, tmp_path):
    user = await stores['users'].get_or_create_by_email('team@test.local')
    cos = await stores['bots'].get_or_create_cos(user.id)
    a = await stores['bots'].create(owner_id=user.id, name='Analyst', role_title='Researcher', charter='Research')
    b = await stores['bots'].create(owner_id=user.id, name='Writer', role_title='Writer', charter='Write')
    session = await stores['sessions'].create(user.id, bot_id=cos.id, kind='direct')
    runtime = make_runtime(stores, provider, tmp_path)
    service = MissionService(stores['missions'], stores['bots'], provider, None, runtime.settings,
                             messages=stores['messages'], sessions=stores['sessions'], bus=runtime.bus)
    runtime.missions = service
    return user, cos, a, b, session, runtime, service


def node(bot, instruction='Research A', **extra):
    return {'id': 'a', 'title': instruction, 'instruction': instruction, 'bot_id': bot.id, 'required_files': [], **extra}


def test_artifact_rebase_accepts_owner_root_alias_but_rejects_escape(tmp_path):
    from sbot.core.assignment_workspace import rebase_result
    root = tmp_path / 'real'
    workspace = root / '.team-jobs' / 'job' / 'step'
    workspace.mkdir(parents=True)
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    result = rebase_result({'artifacts': ['result.txt']}, workspace.resolve(), alias)
    assert result['artifacts'] == ['.team-jobs/job/step/result.txt']
    with pytest.raises(ValueError, match='escapes assignment'):
        rebase_result({'artifacts': ['../../outside.txt']}, workspace, alias)


@pytest.mark.asyncio
async def test_group_background_job_reports_to_its_own_conversation(stores, tmp_path):
    from sbot.db.bot_groups import BotGroupStore

    provider = FakeProvider([[finish('Research')], [finish('Draft')], [finish('Group result')]])
    user, cos, a, b, session, runtime, service = await setup_team(stores, provider, tmp_path)
    group = await BotGroupStore(stores['sessions'].factory).save(
        user.id, 'Research team', [cos.id, a.id, b.id], cos.id)
    job = await service.submit_work(user.id, group.session_id, 'group-job', 'Research and write',
                                    [node(a), node(b, 'Write from research', id='b', depends_on=['a'])],
                                    cos.id, frozenset([cos.id, a.id, b.id]))
    assert await service._running[job.id] == 'completed'
    assert any('group-job' in str(m.get('content')) for m in await stores['messages'].recent(group.session_id))
    assert not await stores['messages'].recent(session.id)
    await service.stop()


@pytest.mark.asyncio
async def test_chat_accepts_second_job_and_status_while_first_job_is_running(stores, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Provider(FakeProvider):
        async def stream_chat(self, messages, **kwargs):
            self.calls.append(list(messages))
            if '# Mission\n' in messages[0]['content']:
                entered.set()
                await release.wait()
                yield finish('Verified result for ' + messages[0]['content'].split('# Mission\n')[1].split('\n')[0])
            elif messages[-1]['role'] == 'tool':
                yield ChatResult(content=messages[-1]['content'])
            elif messages[-1]['content'].endswith('status'):
                yield call('team_status')
            else:
                task = 'Research A' if 'first job' in messages[-1]['content'] else 'Write B'
                yield call('delegate', bot_name='Analyst' if task == 'Research A' else 'Writer', task=task)

    provider = Provider([])
    user, cos, a, b, session, runtime, service = await setup_team(stores, provider, tmp_path)
    try:
        receipt = await asyncio.wait_for(runtime.handle_message(user.id, session.id, 'first job'), 5)
        assert 'Job ID:' in receipt
        await asyncio.wait_for(entered.wait(), 5)
        receipt_b = await asyncio.wait_for(runtime.handle_message(user.id, session.id, 'second job'), 5)
        assert 'Job ID:' in receipt_b and receipt_b != receipt
        progress = await asyncio.wait_for(runtime.handle_message(user.id, session.id, 'status'), 5)
        assert 'Research A' in progress and 'Write B' in progress
        assert not release.is_set()
        tasks = list(service._running.values())
        release.set()
        assert set(await asyncio.gather(*tasks)) == {'completed'}
        worker_prompts = [m for m in provider.calls if '# Mission\n' in m[0]['content']]
        assert len(worker_prompts) == 2
        for messages in worker_prompts:
            text = str(messages)
            assert not ('Research A' in text and 'Write B' in text)
        history = await stores['messages'].recent(session.id, limit=100)
        reports = [m for m in history if m.get('meta', {}).get('delivery_id')]
        assert len(reports) == 2
        for job in await stores['missions'].list_missions(user.id):
            await service._report(job, 'completed')
        assert len([m for m in await stores['messages'].recent(session.id, limit=100)
                    if m.get('meta', {}).get('delivery_id')]) == 2
    finally:
        release.set()
        await service.stop()
        await runtime.drain()


@pytest.mark.asyncio
async def test_failed_upstream_does_not_run_writer_or_coordinator_or_retry(stores, tmp_path):
    provider = FakeProvider([[ChatResult(content='')], [ChatResult(content='')]])
    user, cos, a, b, session, runtime, service = await setup_team(stores, provider, tmp_path)
    job = await service.submit_work(user.id, session.id, 'fail-job', 'Research then write', [
        node(a), node(b, 'Write post', id='b', depends_on=['a'])], cos.id)
    assert await service._running[job.id] == 'failed'
    result = await service.status(job.id, user.id)
    assert {n['id']: n['status'] for n in result['nodes']} == {'a': 'error', 'b': 'pending', '__summary': 'pending'}
    assert len(provider.calls) == 2  # One result-recording reminder, no action replay.
    assert next(n for n in result['nodes'] if n['id'] == 'a')['attempts'] == 1
    assert provider.offered_tools[-1] == ['finish_step']


@pytest.mark.asyncio
async def test_dependent_steps_receive_result_and_coordinator_combines_once(stores, tmp_path):
    provider = FakeProvider([[finish('Evidence A: 12%')], [finish('Draft B using 12%')], [finish('Final combined answer')]])
    user, cos, a, b, session, runtime, service = await setup_team(stores, provider, tmp_path)
    job = await service.submit_work(user.id, session.id, 'combined', 'Research and post', [
        node(a), node(b, 'Write post', id='b', depends_on=['a'])], cos.id)
    assert await service._running[job.id] == 'completed'
    assert 'Evidence A: 12%' in str(provider.calls[1])
    assert 'Draft B using 12%' in str(provider.calls[2])
    assert all('team_submit' not in names and 'delegate' not in names for names in provider.offered_tools)
    report = [m for m in await stores['messages'].recent(session.id) if m.get('meta', {}).get('delivery_id')][0]
    assert 'Final combined answer' in report['content']
    assert 'Evidence A: 12%' not in report['content']


@pytest.mark.asyncio
async def test_receipt_is_idempotent_and_queue_capacity_is_enforced(stores, tmp_path):
    user, cos, a, b, session, runtime, service = await setup_team(stores, FakeProvider([]), tmp_path)
    service.settings.team_work.max_pending_per_owner = 1
    await service._mission_slots.acquire()
    # Hold all capacity so the test observes persistence before execution.
    for _ in range(3):
        await service._mission_slots.acquire()
    tool = TeamSubmitTool(service, user.id, cos.id)
    s = current_session_id.set(session.id)
    t = current_turn_id.set('turn-1')
    try:
        first = await tool.execute(goal='A', nodes=[node(a)])
        assert 'Job ID:' in first
        assert await tool.execute(goal='A', nodes=[node(a)]) == first
        assert (await tool.execute(goal='B', nodes=[node(b)])).startswith('Error: background queue is full')
        assert len(await stores['missions'].list_missions(user.id)) == 1
        status = json.loads(await TeamStatusTool(service, user.id).execute())
        assert status['jobs'][0]['status'] == 'queued'
        job_id = status['jobs'][0]['id']
        await TeamCancelTool(service, user.id).execute(job_id)
        tasks = list(service._running.values())
        service._mission_slots.release()
        assert await asyncio.gather(*tasks) == ['cancelled']
        assert await service.start(job_id, user.id) == 'cancelled'
        assert not service.provider.calls
    finally:
        current_session_id.reset(s)
        current_turn_id.reset(t)
        await service.stop()


@pytest.mark.asyncio
async def test_same_bot_is_queued_across_jobs_but_other_bot_can_run(stores, tmp_path):
    release, both = asyncio.Event(), asyncio.Event()
    active = set()
    overlaps = []

    class Provider(FakeProvider):
        async def stream_chat(self, messages, **kwargs):
            name = messages[0]['content'].split(',')[0]
            assert name not in active
            active.add(name)
            overlaps.append(len(active))
            if len(active) == 2:
                both.set()
            try:
                await release.wait()
                yield finish(name)
            finally:
                active.remove(name)

    user, cos, a, b, session, runtime, service = await setup_team(stores, Provider([]), tmp_path)
    try:
        for mid, bot in [('a1', a), ('a2', a), ('b1', b)]:
            await service.submit_work(user.id, session.id, mid, mid, [node(bot)], cos.id)
        await asyncio.wait_for(both.wait(), 5)
        phases = [await service.status(mid, user.id) for mid in ('a1', 'a2', 'b1')]
        assert any(s['nodes'][0]['execution_state'] == 'queued' for s in phases)
        tasks = list(service._running.values())
        release.set()
        assert set(await asyncio.gather(*tasks)) == {'completed'}
        assert max(overlaps) == 2
    finally:
        release.set()
        await service.stop()


@pytest.mark.asyncio
async def test_jobs_writing_same_filename_get_distinct_deliverables(stores, tmp_path):
    class Provider(FakeProvider):
        async def stream_chat(self, messages, **kwargs):
            goal = messages[0]['content'].split('# Mission\n')[1].split('\n')[0]
            if messages[-1]['role'] != 'tool':
                yield call('write_file', path='report.txt', content=goal)
            else:
                yield call('finish_step', status='completed', summary=goal, evidence='Checked file', files=['report.txt'])

    user, cos, a, b, session, runtime, service = await setup_team(stores, Provider([]), tmp_path)
    for mid, bot in [('alpha', a), ('beta', b)]:
        await service.submit_work(user.id, session.id, mid, mid, [node(bot, required_files=['report.txt'])], cos.id)
    assert set(await asyncio.gather(*list(service._running.values()))) == {'completed'}
    paths = []
    for mid in ('alpha', 'beta'):
        status = await service.status(mid, user.id)
        path = status['nodes'][0]['artifacts'][0]
        paths.append(path)
        assert (service.settings.workspaces_root / user.id / path).read_text() == mid
    assert paths[0] != paths[1]


@pytest.mark.asyncio
async def test_group_status_and_cancellation_do_not_cross_conversations(stores, tmp_path):
    user, cos, a, b, session, runtime, service = await setup_team(stores, FakeProvider([]), tmp_path)
    other = await stores['sessions'].create(user.id, bot_id=cos.id, kind='direct')
    job = await stores['missions'].create_mission(user.id, 'Private job', session.id, status='queued')
    s = current_session_id.set(other.id)
    try:
        assert json.loads(await TeamStatusTool(service, user.id, group=True).execute())['jobs'] == []
        assert (await TeamCancelTool(service, user.id, group=True).execute(job.id)).startswith('Error')
        assert (await stores['missions'].get_mission(job.id, user.id)).status == 'queued'
        with pytest.raises(InvalidGraphError, match='outside this group'):
            await service.submit_work(user.id, other.id, 'invalid', 'Forbidden', [node(a)], cos.id, frozenset([b.id]))
    finally:
        current_session_id.reset(s)


@pytest.mark.asyncio
async def test_truncated_specialist_reply_keeps_partial_status(stores, tmp_path):
    user, cos, a, b, session, runtime, service = await setup_team(
        stores, FakeProvider([[ChatResult(content='cut off mid-sentence', finish_reason='length')]]), tmp_path)
    runner = SpecialistRunner(service.provider, None, tmp_path)
    outcome = await runner.run(a, 'Research', 'Task', lambda e: None, 'turn')
    result = TaskResult.from_outcome(outcome)
    assert result.status == 'partial' and result.failure_reason == 'output_limit'


@pytest.mark.asyncio
async def test_empty_output_recovery_cannot_repeat_external_action(stores, tmp_path):
    provider = FakeProvider([[ChatResult(content='')], [call('write_file', path='must-not-exist', content='no')]])
    user, cos, a, b, session, runtime, service = await setup_team(stores, provider, tmp_path)
    job = await service.submit_work(user.id, session.id, 'recovery', 'A', [node(a)], cos.id)
    assert await service._running[job.id] == 'failed'
    assert not list((service.settings.workspaces_root / user.id).rglob('must-not-exist'))
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_queued_job_recovers_after_service_restart(stores, tmp_path):
    user, cos, a, b, session, runtime, service = await setup_team(stores, FakeProvider([[finish('Recovered')]]), tmp_path)
    for _ in range(4):
        await service._mission_slots.acquire()
    job = await service.submit_work(user.id, session.id, 'restart', 'A', [node(a)], cos.id)
    await service.stop()
    revived = MissionService(stores['missions'], stores['bots'], service.provider, None, service.settings,
                             messages=stores['messages'], sessions=stores['sessions'])
    assert await revived.resume_interrupted() == 1
    assert await revived._running[job.id] == 'completed'


@pytest.mark.asyncio
async def test_background_feature_can_be_disabled(stores, tmp_path):
    user, cos, a, b, session, runtime, service = await setup_team(stores, FakeProvider([]), tmp_path)
    runtime.settings.team_work.enabled = False
    agent = runtime.get_agent(user.id, cos.id, is_cos=True)
    assert not agent.tools.has('team_submit')
    assert not agent.tools.get('delegate').ends_turn_on_success


@pytest.mark.asyncio
async def test_cancel_prevents_already_claimed_but_waiting_step_from_executing(stores, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    executions = []

    class Provider(FakeProvider):
        async def stream_chat(self, messages, **kwargs):
            executions.append(messages[0]['content'])
            entered.set()
            await release.wait()
            yield finish('First job only')

    user, cos, a, b, session, runtime, service = await setup_team(stores, Provider([]), tmp_path)
    try:
        await service.submit_work(user.id, session.id, 'first', 'First job', [node(a)], cos.id)
        await asyncio.wait_for(entered.wait(), 5)
        await service.submit_work(user.id, session.id, 'second', 'Second job', [node(a)], cos.id)
        await service.cancel('second', user.id)
        tasks = list(service._running.values())
        release.set()
        await asyncio.gather(*tasks)
        assert len(executions) == 1
        assert (await stores['missions'].get_mission('second', user.id)).status == 'cancelled'
        assert not service._owner_slots._entries and not service._bot_slots._entries
    finally:
        release.set()
        await service.stop()


@pytest.mark.asyncio
async def test_coordinator_cannot_perform_new_actions(stores, tmp_path):
    provider = FakeProvider([[finish('A')], [finish('B')],
                             [call('write_file', path='unauthorized.txt', content='no')],
                             [finish('Combined')]])
    user, cos, a, b, session, runtime, service = await setup_team(stores, provider, tmp_path)
    job = await service.submit_work(user.id, session.id, 'summary', 'Combine results', [
        node(a), node(b, 'B', id='b', depends_on=['a'])], cos.id)
    assert await service._running[job.id] == 'completed'
    assert not list((service.settings.workspaces_root / user.id).rglob('unauthorized.txt'))
    assert 'may only read existing results' in str(provider.calls[-1])


@pytest.mark.asyncio
async def test_truncated_tool_call_never_executes(stores, tmp_path):
    partial_call = call('write_file', path='bad.txt', content='incomplete')
    partial_call.finish_reason = 'length'
    user, cos, a, b, session, runtime, service = await setup_team(stores, FakeProvider([[partial_call]]), tmp_path)
    runner = SpecialistRunner(service.provider, None, tmp_path)
    outcome = await runner.run(a, 'A', 'write', lambda e: None, 'truncated')
    assert outcome.output_truncated
    assert not (tmp_path / 'bad.txt').exists()


@pytest.mark.asyncio
async def test_queue_limits_and_job_controls_are_owner_scoped(stores, tmp_path):
    user, cos, a, b, session, runtime, service = await setup_team(stores, FakeProvider([]), tmp_path)
    outsider = await stores['users'].get_or_create_by_email('outside@test.local')
    job = await stores['missions'].create_mission(user.id, 'private', session.id, status='queued')
    assert (await TeamStatusTool(service, outsider.id).execute(job.id)).startswith('Error')
    assert (await TeamCancelTool(service, outsider.id).execute(job.id)).startswith('Error')
    assert json.loads(await TeamStatusTool(service, outsider.id).execute())['jobs'] == []
    service.settings.team_work.max_pending_total = 1
    with pytest.raises(InvalidGraphError, match='queue is full'):
        await service.submit_work(user.id, session.id, 'too-many', 'A', [node(a)], cos.id)


@pytest.mark.asyncio
async def test_ask_mode_gates_background_submission(stores, tmp_path):
    from sbot.core.loop import AgentLoop
    from sbot.tools.registry import ToolRegistry

    user, cos, a, b, session, runtime, service = await setup_team(stores, FakeProvider([]), tmp_path)
    tools = ToolRegistry()
    tools.register(TeamSubmitTool(service, user.id, cos.id))
    provider = FakeProvider([[call('team_submit', goal='A', nodes=[node(a)])], text_turn('Declined')])
    confirmations = []

    async def deny(turn, tool, args):
        confirmations.append(tool)
        return False

    await AgentLoop(provider, tools, model='fake').run_turn(
        'ask', [{'role': 'user', 'content': 'A'}], lambda e: None, permission_mode='ask', confirm=deny)
    assert confirmations == ['team_submit']
    assert not await stores['missions'].list_missions(user.id)
