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

    async def run(self, plan: WorkflowPlan, on_progress: ProgressCb = None) -> WorkflowResult:
        completed: list[str] = []
        total = len(plan.steps)
        for i, step in enumerate(plan.steps, start=1):
            step.status = "running"
            if on_progress:
                await on_progress(
                    {"stage": "step", "label": step.title, "index": i, "total": total, "status": "running"}
                )
            context = "\n\n".join(completed) if completed else ""
            try:
                step.output = await self.subagents.run(step.instruction, context=context)
                step.status = "done"
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
            completed.append(f"## {step.title}\n{step.output}")

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
                        "to the original request. Match the user's language.",
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

    async def run_request(self, request: str, on_progress: ProgressCb = None) -> WorkflowResult:
        plan = await self.plan(request)
        if on_progress:
            n = len(plan.steps)
            await on_progress(
                {"stage": "plan", "label": f"Planned {n} step{'' if n == 1 else 's'}",
                 "index": 0, "total": n, "status": "done"}
            )
        return await self.run(plan, on_progress=on_progress)
