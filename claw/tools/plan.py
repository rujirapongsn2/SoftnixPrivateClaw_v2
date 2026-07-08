"""Working-plan tool: let the agent record and update the goal + step checklist
for the current task.

The plan is pinned into the system prompt every turn (see
`claw.core.context.render_plan` and its use in the runtime), so it survives even
when earlier messages are trimmed out of the context window — the agent keeps
the thread on long or autonomous runs instead of drifting once the original
request scrolls away. The tool is stateless about which session it belongs to;
it reads the active session from the per-turn ContextVar so a single cached
per-user agent can serve concurrent sessions safely.
"""

from typing import Any

from claw.core.turn_context import current_session_id
from claw.db.stores import SessionStore
from claw.tools.base import Tool

_STATUSES = ("pending", "in_progress", "done")


class PlanTool(Tool):
    name = "update_plan"
    description = (
        "Record or update your working plan for a multi-step or long-running task: the "
        "overall goal plus an ordered checklist of steps, each with a status. The plan is "
        "PINNED into your context on every turn, so it stays visible even after earlier "
        "messages scroll out of the context window — use it to stay on track across long "
        "sessions and autonomous runs, and to show the user what you're doing. Send the "
        "COMPLETE step list each call (it replaces the stored one); mark steps 'in_progress' "
        "or 'done' as you go. Skip this for simple one-shot questions that need no plan."
    )
    parameters = {
        "type": "object",
        "properties": {
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
        "required": ["goal"],
    }

    def __init__(self, sessions: SessionStore):
        self.sessions = sessions

    async def execute(self, goal: str, steps: Any = None, **kwargs: Any) -> str:
        session_id = current_session_id.get()
        if not session_id:
            return "Error: no active session to attach the plan to."
        goal = str(goal or "").strip()
        clean: list[dict[str, str]] = []
        for item in steps or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("step") or "").strip()
            if not text:
                continue
            status = item.get("status")
            clean.append({"step": text, "status": status if status in _STATUSES else "pending"})
        await self.sessions.set_plan(session_id, goal, clean)
        done = sum(1 for s in clean if s["status"] == "done")
        return f"Plan saved ({done}/{len(clean)} steps done). It stays pinned in your context."
