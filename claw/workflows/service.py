"""Dynamic workflow: decompose a request into steps, run each via a subagent,
then synthesize a final answer.

- plan()      : one forced-tool-call LLM turn returns an ordered step list.
- run()       : each step runs as an isolated subagent, receiving the outputs of
                prior steps as context; progress is reported via a callback.
- synthesize  : a final LLM turn combines the request and all step outputs.

Subagents do the work, so a workflow inherits their sandbox isolation and never
touches the main conversation's context window.
"""

from collections.abc import Awaitable, Callable
from time import monotonic

from loguru import logger

from claw.core.subagent import SubagentManager
from claw.providers.base import LLMProvider, ProviderError
from claw.workflows.models import WorkflowPlan, WorkflowResult, WorkflowStep

# Structured progress: receives a dict {stage, label, index, total, status} so
# the UI can render a live checklist. (stage: plan | step | synthesize)
ProgressCb = Callable[[dict], Awaitable[None]] | None

_PLAN_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "propose_plan",
            "description": "Break the user's request into an ordered list of concrete steps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "instruction": {
                                    "type": "string",
                                    "description": "A self-contained instruction a worker can execute.",
                                },
                            },
                            "required": ["title", "instruction"],
                        },
                    }
                },
                "required": ["steps"],
            },
        },
    }
]

_MAX_STEPS = 8

# Don't start a step with less than this left on the clock: it would spend an
# LLM call only to be cut off, and the step reads better as "never started".
_MIN_STEP_SECONDS = 15.0


class WorkflowService:
    def __init__(self, provider: LLMProvider, subagents: SubagentManager, model: str | None = None):
        self.provider = provider
        self.subagents = subagents
        self.model = model

    async def plan(self, request: str) -> WorkflowPlan:
        result = await self.provider.chat(
            messages=[
                {
                    "role": "system",
                    "content": "You are a planner. Decompose the request into 2-6 concrete, "
                    "independently-executable steps and call propose_plan. Keep steps minimal.",
                },
                {"role": "user", "content": request},
            ],
            tools=_PLAN_TOOL,
            model=self.model,
        )
        steps: list[WorkflowStep] = []
        if result.has_tool_calls:
            raw = result.tool_calls[0].arguments.get("steps") or []
            # Models don't always honor the array schema: `steps` can come back as
            # a single step object, or an object keyed by index. Coerce to a list
            # so a stray shape degrades to a valid plan instead of crashing.
            if isinstance(raw, dict):
                raw = [raw] if raw.get("instruction") else list(raw.values())
            if not isinstance(raw, list):
                raw = []
            for item in raw[:_MAX_STEPS]:
                if isinstance(item, dict) and item.get("instruction"):
                    steps.append(
                        WorkflowStep(
                            title=str(item.get("title") or "step"),
                            instruction=str(item["instruction"]),
                        )
                    )
        if not steps:
            # Fall back to a single step so a workflow always makes progress.
            steps = [WorkflowStep(title="Complete the task", instruction=request)]
        return WorkflowPlan(request=request, steps=steps)

    async def run(
        self,
        plan: WorkflowPlan,
        on_progress: ProgressCb = None,
        outer_deadline: float | None = None,
    ) -> WorkflowResult:
        completed: list[str] = []
        total = len(plan.steps)
        # One wall-clock budget for the whole workflow, shared across its steps.
        # Each step otherwise gets the parent turn's full budget, so an 8-step
        # plan can run 8x as long as the turn that started it — and the parent
        # only checks its own clock between iterations, so it cannot notice.
        budget = self.subagents.max_turn_seconds
        deadline = monotonic() + budget if budget > 0 else None
        # `outer_deadline` is the calling turn's own absolute deadline (same
        # monotonic clock) — a workflow started late in that turn must not grant
        # itself a fresh full budget regardless of how little time is actually
        # left, so the tighter of the two always wins.
        if outer_deadline is not None:
            deadline = outer_deadline if deadline is None else min(deadline, outer_deadline)
        for i, step in enumerate(plan.steps, start=1):
            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining < _MIN_STEP_SECONDS:
                step.status = "error"
                step.output = "Not started: the workflow ran out of time before reaching this step."
                logger.warning("Workflow ran out of time before step {}", step.title)
            else:
                step.status = "running"
                if on_progress:
                    await on_progress(
                        {
                            "stage": "step",
                            "label": step.title,
                            "index": i,
                            "total": total,
                            "status": "running",
                        }
                    )
                context = "\n\n".join(completed) if completed else ""
                try:
                    run = await self.subagents.run_result(
                        step.instruction, context=context, max_turn_seconds=remaining
                    )
                    # A subagent reports failure in prose, not by raising, so the
                    # status has to come from its flag — otherwise a step that
                    # timed out is recorded as done and synthesized into an answer.
                    step.output = run.text
                    step.status = "done" if run.ok else "error"
                except ProviderError as exc:
                    step.status = "error"
                    step.output = f"error: {exc}"
                    logger.warning("Workflow step {} failed: {}", step.title, exc)
            if on_progress:
                await on_progress(
                    {
                        "stage": "step",
                        "label": step.title,
                        "index": i,
                        "total": total,
                        "status": "done" if step.status == "done" else "error",
                    }
                )
            # Mark the failures inline: this list is both the context handed to
            # later steps and the input to synthesis, and neither can tell a
            # limit message from a real result without it.
            marker = "" if step.status == "done" else " (did not finish)"
            completed.append(f"## {step.title}{marker}\n{step.output}")

        if on_progress and total > 1:
            await on_progress({"stage": "synthesize", "label": "Synthesizing answer", "status": "running"})
        final = await self._synthesize(plan, completed)
        status = "failed" if any(s.status == "error" for s in plan.steps) else "completed"
        return WorkflowResult(plan=plan, final_output=final, status=status)

    async def _synthesize(self, plan: WorkflowPlan, step_outputs: list[str]) -> str:
        if len(plan.steps) == 1:
            return plan.steps[0].output
        try:
            result = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "Synthesize the step results into one clear, complete answer "
                        "to the original request. Match the user's language. A step marked "
                        "'did not finish' produced no result — say plainly what is missing "
                        "instead of inventing what it would have found.",
                    },
                    {
                        "role": "user",
                        "content": f"Original request:\n{plan.request}\n\n"
                        f"Step results:\n" + "\n\n".join(step_outputs),
                    },
                ],
                model=self.model,
            )
        except ProviderError as exc:
            return "\n\n".join(step_outputs) + f"\n\n(synthesis failed: {exc})"
        return result.content or "\n\n".join(step_outputs)

    async def run_request(
        self,
        request: str,
        on_progress: ProgressCb = None,
        outer_deadline: float | None = None,
    ) -> WorkflowResult:
        plan = await self.plan(request)
        if on_progress:
            n = len(plan.steps)
            await on_progress(
                {
                    "stage": "plan",
                    "label": f"Planned {n} step{'' if n == 1 else 's'}",
                    "index": 0,
                    "total": n,
                    "status": "done",
                }
            )
        return await self.run(plan, on_progress=on_progress, outer_deadline=outer_deadline)
