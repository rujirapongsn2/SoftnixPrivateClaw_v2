"""Working-plan tool: let the agent record and update the goal + step checklist
for the current task.

The plan is pinned into the system prompt every turn (see
`sbot.core.context.render_plan` and its use in the runtime), so it survives even
when earlier messages are trimmed out of the context window — the agent keeps
the thread on long or autonomous runs instead of drifting once the original
request scrolls away. The tool is stateless about which session it belongs to;
it reads the active session from the per-turn ContextVar so a single cached
per-user agent can serve concurrent sessions safely.
"""

from typing import Any

from sbot.core.turn_context import current_session_id
from sbot.db.stores import SessionStore
from sbot.tools.base import Tool

_STATUSES = ("pending", "in_progress", "done", "waiting_for_user", "blocked")

# The plan is pinned into the system prompt, which means it is never trimmed to
# fit the context window — so the model writing it must not be able to decide how
# much of its own window it occupies. A plan longer than this is not a plan.
_MAX_GOAL_CHARS = 500
_MAX_STEPS = 40
_MAX_STEP_CHARS = 300


class PlanTool(Tool):
    name = "update_plan"
    description = (
        "Record or update your working plan for a multi-step or long-running task: the "
        "overall goal plus an ordered checklist of steps, each with a status. The plan is "
        "PINNED into your context on every turn, so it stays visible even after earlier "
        "messages scroll out of the context window — use it to stay on track across long "
        "sessions and autonomous runs, and to show the user what you're doing. Send the "
        "COMPLETE step list each call (it replaces the stored one); mark steps 'in_progress' "
        "or 'done' as you go. Set continue_work=true only when the current user request "
        "asks you to perform or resume this work. For status questions or summaries, "
        "leave it false: recording pending steps must not restart the old task. "
        "Use 'waiting_for_user' only for essential missing input or required permission, "
        "and 'blocked' for an external failure preventing progress; include the concrete "
        "reason in the step text and explain it in your answer. Never invent an approval "
        "gate for intermediate files. Skip this for simple one-shot questions that need no plan."
    )
    parameters = {
        "type": "object",
        "properties": {
            "continue_work": {
                "type": "boolean",
                "description": "Continue unfinished steps in this turn only for a current execution request; false for status/summary requests.",
            },
            "goal": {
                "type": "string",
                "description": "One concise line stating the overall objective of the current task.",
            },
            "steps": {
                "type": "array",
                "description": "The full ordered list of steps (send every step each time — it "
                "replaces the previous list, so include already-done ones marked 'done').",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string", "description": "Short description of the step."},
                        "status": {"type": "string", "enum": list(_STATUSES)},
                    },
                    "required": ["step", "status"],
                },
            },
        },
        # This must be explicit.  An omitted flag used to make the loop choose
        # between prematurely ending requested work and reviving a status-only
        # request based on a schema default that is not applied to raw tool
        # arguments.
        "required": ["goal", "continue_work"],
    }

    def __init__(self, sessions: SessionStore):
        self.sessions = sessions

    async def execute(self, goal: str, steps: Any = None, **kwargs: Any) -> str:
        session_id = current_session_id.get()
        if not session_id:
            return "Error: no active session to attach the plan to."
        goal = str(goal or "").strip()[:_MAX_GOAL_CHARS]
        clean: list[dict[str, str]] = []
        for item in steps or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("step") or "").strip()[:_MAX_STEP_CHARS]
            if not text:
                continue
            status = item.get("status")
            clean.append({"step": text, "status": status if status in _STATUSES else "pending"})
        dropped = max(0, len(clean) - _MAX_STEPS)
        clean = clean[:_MAX_STEPS]
        await self.sessions.set_plan(session_id, goal, clean)
        done = sum(1 for s in clean if s["status"] == "done")
        note = f" The last {dropped} step(s) were dropped — keep the plan under {_MAX_STEPS}." if dropped else ""
        return f"Plan saved ({done}/{len(clean)} steps done). It stays pinned in your context.{note}"
