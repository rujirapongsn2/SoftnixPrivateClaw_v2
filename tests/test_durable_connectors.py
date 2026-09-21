"""Simulated remote effects exercise both actual MCP proxy implementations."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from claw.jobs.contracts import StepOutcome
from claw.jobs.runtime_bridge import Handoff
from claw.jobs.worker import Executor, Worker
from tests.test_durable_jobs import jobs as shared_jobs_fixture, allow

jobs = shared_jobs_fixture


@pytest.fixture(params=['privateclaw', 'sbot'])
def proxy_type(request):
    if request.param == 'sbot':
        from sbot.core.connectors import McpToolProxy
    else:
        from claw.core.connectors import McpToolProxy
    return McpToolProxy


async def make_worker(jobs, proxy, check=allow):
    async def execute(ctx):
        await ctx.checkpoint({**ctx.state, 'pending_tool': {'name': proxy.name}})
        try:
            text = await proxy.execute(value='synthetic')
        except Handoff as signal:
            return signal.outcome
        await ctx.checkpoint({**ctx.state, 'pending_tool': None})
        return StepOutcome('completed', checkpoint=ctx.state, evidence={'response': str(text)})
    async def validate(ctx, result):
        return bool(result.evidence.get('response'))
    await jobs.submit('connector-job', jobs.test_owner, 'session', 'privateclaw', [
        {'id': 'send', 'executor': 'connector-test', 'effect': 'local', 'replay_safe': True}])
    return Worker(jobs, {'connector-test': Executor(execute, validate)}, allow, connector_authorize=check)


async def test_committed_remote_action_with_lost_response_is_never_sent_twice(jobs, proxy_type):
    committed = []
    async def call(*args, **kwargs):
        committed.append('remote-record')
        raise TimeoutError('response lost after commit')
    proxy = proxy_type(SimpleNamespace(call_tool=call), 'fixture', 'send', '', {})
    worker = await make_worker(jobs, proxy)
    await worker.run_once()
    snap = await jobs.snapshot(jobs.test_owner, 'connector-job')
    assert snap['status'] == 'paused' and snap['reason'] == 'uncertain_effect'
    assert not await jobs.control('connector-job', jobs.test_owner, 'resume')
    assert not await worker.run_once()
    assert committed == ['remote-record']


async def test_missing_connection_recovers_without_rpc_or_llm_retry_loop(jobs, proxy_type):
    live = [None]
    call = AsyncMock(return_value=SimpleNamespace(isError=False, content=[SimpleNamespace(text='receipt')]))
    proxy = proxy_type(None, 'fixture', 'send', '', {}, session_ref=lambda: live[0])
    worker = await make_worker(jobs, proxy)
    await worker.run_once()
    assert (await jobs.snapshot(jobs.test_owner, 'connector-job'))['status'] == 'waiting_dependency'
    assert call.await_count == 0
    live[0] = SimpleNamespace(call_tool=call)
    async def probe(owner, job_id, name):
        from claw.jobs.contracts import digest
        assert name == digest('fixture')[:40]
        return live[0] is not None
    worker.probes[('privateclaw', 'connector')] = probe
    jobs.test_clock[0] += 31
    await worker.probe_once()
    await worker.run_once()
    assert (await jobs.snapshot(jobs.test_owner, 'connector-job'))['status'] == 'completed'
    assert call.await_count == 1


async def test_connector_permission_revoked_before_rpc(jobs, proxy_type):
    call = AsyncMock()
    proxy = proxy_type(SimpleNamespace(call_tool=call), 'fixture', 'send', '', {})
    async def denied(*args):
        return False
    worker = await make_worker(jobs, proxy, denied)
    await worker.run_once()
    assert (await jobs.snapshot(jobs.test_owner, 'connector-job'))['reason'] == 'permission_denied'
    assert call.await_count == 0


async def test_error_response_does_not_prove_no_side_effect(jobs, proxy_type):
    call = AsyncMock(return_value=SimpleNamespace(isError=True, content=[SimpleNamespace(text='partially committed')]))
    proxy = proxy_type(SimpleNamespace(call_tool=call), 'fixture', 'send', '', {})
    worker = await make_worker(jobs, proxy)
    await worker.run_once()
    assert (await jobs.snapshot(jobs.test_owner, 'connector-job'))['reason'] == 'uncertain_effect'
    assert not await worker.run_once()
    assert call.await_count == 1


@pytest.mark.parametrize('mode', ['privateclaw', 'sbot'])
async def test_rest_post_response_lost_never_repeats_write(jobs, mode):
    if mode == 'sbot':
        from sbot.tools.api import GenericApiTool
    else:
        from claw.tools.api import GenericApiTool
    connector = SimpleNamespace(name='fixture', env={}, url='https://fixture.test')
    tool = GenericApiTool(connector, {'name': 'create', 'method': 'POST', 'path': '/records'}, timeout_seconds=1)
    committed = []
    async def send(*args):
        committed.append('saved')
        raise TimeoutError('response lost')
    tool._send = send
    worker = await make_worker(jobs, tool)
    await worker.run_once()
    snap = await jobs.snapshot(jobs.test_owner, 'connector-job')
    assert snap['status'] == 'paused' and snap['reason'] == 'uncertain_effect'
    assert not await worker.run_once()
    assert committed == ['saved']
