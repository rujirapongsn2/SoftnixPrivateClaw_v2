"""Durable connector boundaries. A missing response is not proof of no effect."""
from claw.jobs.contracts import StepOutcome, ToolResult, digest
from claw.jobs.provider import current_execution
from claw.jobs.runtime_bridge import Handoff
from claw.jobs.tool_results import ToolText


async def authorize(connector):
    ctx = current_execution.get()
    if ctx is None:
        return
    check = getattr(ctx, 'connector_authorize', None)
    if check is None or not await check(ctx.lease, connector):
        raise Handoff(StepOutcome('paused', checkpoint=ctx.state, reason='permission_denied'))


async def unavailable(connector):
    ctx = current_execution.get()
    if ctx is None:
        return
    # No RPC was sent. Clear only the pending marker; messages/results survive.
    state = {**ctx.state, 'pending_tool': None, 'waiting_connector': connector,
             'runtime': {**ctx.state.get('runtime', {}), 'pending_call': None}}
    await ctx.checkpoint(state)
    raise Handoff(StepOutcome('dependency', checkpoint=state,
        dependency='connector:' + digest(connector)[:40], reason='dependency_unavailable'))


async def uncertain(connector):
    ctx = current_execution.get()
    if ctx is None:
        return
    # Preserve the pending call for inspection. The model never receives a retry
    # hint for an RPC which could have committed before its response was lost.
    await ctx.checkpoint({**ctx.state, 'uncertain_connector': connector})
    raise Handoff(StepOutcome('paused', checkpoint=ctx.state, reason='uncertain_effect'))


def rendered(text, connector, *, error=False):
    if current_execution.get() is None:
        return text
    return ToolText(ToolResult(status='error' if error else 'unknown', text=text,
        error_code='connector_execution_error' if error else '', dependency='connector:' + connector))
