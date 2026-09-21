"""Business/tool approval is independent of automatic resource allocation."""
import json
from claw.jobs.contracts import StepOutcome, digest
from claw.jobs.provider import current_execution
from claw.jobs.runtime_bridge import Handoff


async def confirm(tool, arguments):
    ctx = current_execution.get()
    key = digest([ctx.lease.job_id, ctx.lease.step_id, tool, arguments])
    previous = ctx.state.get('approval') or {}
    if previous.get('key') == key and isinstance(previous.get('approved'), bool):
        approved = previous['approved']
        await ctx.checkpoint({**ctx.state, 'approval': None})
        return approved
    request = {'key': key, 'tool': tool, 'arguments': json.dumps(arguments, ensure_ascii=False)}
    await ctx.checkpoint({**ctx.state, 'approval': request})
    raise Handoff(StepOutcome('awaiting_input', checkpoint=ctx.state, reason='approval_required'))
