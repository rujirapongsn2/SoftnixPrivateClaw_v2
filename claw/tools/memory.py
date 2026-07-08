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


class RecallMemoryTool(Tool):
    name = "recall_memory"
    description = (
        "Search your long-term record of PAST conversations with this user for anything "
        "relevant to a query — decisions made, topics discussed, work done in earlier "
        "sessions. Use this when the user refers to something from before that isn't in the "
        "current conversation or your always-on Long-term Memory (e.g. \"what did we decide "
        "about the report?\", \"continue what we started yesterday\"). Returns the most "
        "relevant summary entries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for — a topic, decision, or keywords from a past chat.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, memory: MemoryService, user_id: str):
        self.memory = memory
        self.user_id = user_id

    async def execute(self, query: str, **_: Any) -> str:
        query = str(query or "").strip()
        if not query:
            return "Error: recall_memory requires a 'query'."
        entries = await self.memory.recall(self.user_id, query)
        if not entries:
            return f"No past-conversation records found matching: {query}"
        return "\n\n---\n\n".join(entries)
