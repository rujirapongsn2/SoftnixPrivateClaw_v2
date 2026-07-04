"""Spawn tool: the agent delegates a subtask to an isolated subagent."""

from typing import Any

from claw.core.subagent import SubagentManager
from claw.tools.base import Tool


class SpawnTool(Tool):
    name = "spawn"
    description = (
        "Delegate a self-contained subtask to an isolated subagent that works autonomously "
        "and returns a result. Use for research, multi-step lookups, or work you want done "
        "in parallel without cluttering the main conversation. The subagent cannot ask "
        "follow-up questions, so give it everything it needs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The complete task for the subagent"},
            "context": {"type": "string", "description": "Optional supporting context"},
        },
        "required": ["task"],
    }

    def __init__(self, manager: SubagentManager):
        self.manager = manager

    async def execute(self, task: str, context: str = "", **_: Any) -> str:
        return await self.manager.run(task, context)
