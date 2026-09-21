"""Typed metadata for migrated tools; legacy text explicitly remains unknown."""
from claw.jobs.contracts import ToolResult, StepOutcome
from claw.jobs.provider import current_execution


class ToolText(str):
    def __new__(cls, result: ToolResult):
        if isinstance(result, str):
            result = ToolResult.legacy(result)
        value = super().__new__(cls, result.text)
        value.result = result
        return value


async def observe(value):
    ctx = current_execution.get()
    if ctx is None:
        return
    result = value.result if isinstance(value, ToolText) else ToolResult.legacy(value)
    record = {'status': result.status, 'error_code': result.error_code,
              'dependency': result.dependency, 'retryable': result.retryable}
    state = {**ctx.state, 'last_tool_result': record}
    if result.status == 'error':
        errors = dict(state.get('error_fingerprints', {}))
        errors[result.fingerprint] = errors.get(result.fingerprint, 0) + 1
        state['error_fingerprints'] = errors
        await ctx.checkpoint(state)
        if errors[result.fingerprint] > 2:
            from claw.jobs.runtime_bridge import Handoff
            raise Handoff(StepOutcome('paused', checkpoint=state, reason='strategy_exhausted'))
    else:
        await ctx.checkpoint(state)
