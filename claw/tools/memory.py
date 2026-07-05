"""Memory tool: let the agent persist durable facts about the user on demand.

Core memory is injected into every conversation's system prompt, but it was
previously only ever written by the background consolidation pass (which needs
a full message window to accumulate). This tool lets the agent save a fact the
moment the user shares one ("remember my name is Top"), so it's recalled in
later conversations instead of being lost.
"""

from typing import Any

from claw.core.memory import MemoryService
from claw.tools.base import Tool


class MemoryTool(Tool):
    name = "remember"
    description = (
        "Save a durable fact about the user to long-term memory so you recall it in future "
        "conversations, or review what you currently remember. Use action 'save' when the user "
        "shares something worth keeping (their name, preferences, ongoing projects, a correction, "
        "how they want you to behave). Use action 'list' to see current memory. Memory is included "
        "automatically in every future conversation — you do not need to save things already shown "
        "in your Long-term Memory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["save", "list"]},
            "fact": {
                "type": "string",
                "description": "The fact to remember (required for 'save'). One concise statement, "
                "e.g. \"The user's name is Top\" or \"Prefers concise answers in Thai\".",
            },
        },
        "required": ["action"],
    }

    def __init__(self, memory: MemoryService, user_id: str):
        self.memory = memory
        self.user_id = user_id

    async def execute(self, action: str, **kwargs: Any) -> str:
        if action == "save":
            fact = str(kwargs.get("fact") or "").strip()
            if not fact:
                return "Error: save requires a 'fact'."
            return await self.memory.remember(self.user_id, fact)
        if action == "list":
            core = await self.memory.core_text(self.user_id)
            return core.strip() or "Long-term memory is empty."
        return f"Error: unknown action '{action}'"
