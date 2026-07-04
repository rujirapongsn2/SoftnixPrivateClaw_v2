"""Memory consolidation — the agent's continuous-learning backbone.

Old conversation turns are summarized by the LLM (forced tool call, no
free-text parsing) into:
- a living core-memory document per user (facts, preferences, corrections)
- append-only history entries (grep/searchable recall)
"""

from typing import Any

from loguru import logger

from claw.db.stores import MemoryStore, MessageStore, SessionStore
from claw.providers.base import LLMProvider

_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "2-5 sentence summary of key events/decisions, starting with [YYYY-MM-DD HH:MM].",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated core memory as markdown: stable facts about the user, "
                        "their preferences, feedback they gave, and how to apply it. "
                        "Return the existing content unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


class MemoryService:
    def __init__(
        self,
        memories: MemoryStore,
        messages: MessageStore,
        sessions: SessionStore,
        provider: LLMProvider,
        model: str | None = None,
        window: int = 60,
        keep: int = 20,
    ):
        self.memories = memories
        self.messages = messages
        self.sessions = sessions
        self.provider = provider
        self.model = model
        self.window = window
        self.keep = keep

    async def build_context(self, user_id: str) -> str:
        core = await self.memories.get_core(user_id)
        if not core:
            return ""
        return f"# Memory\n\n## Long-term Memory\n{core}"

    async def maybe_consolidate(self, user_id: str, session_id: str) -> bool:
        """Consolidate when enough unconsolidated messages accumulated. Returns True if ran."""
        session = await self.sessions.get(session_id)
        if session is None:
            return False
        max_seq = await self.messages.max_seq(session_id)
        unconsolidated = max_seq - session.last_consolidated_seq
        if unconsolidated < self.window:
            return False

        cutoff = max_seq - self.keep
        old = await self.messages.recent(
            session_id, after_seq=session.last_consolidated_seq, limit=self.window * 2
        )
        # Only messages up to the cutoff participate; the recent tail stays raw.
        old = old[: max(0, len(old) - self.keep)]
        if not old:
            return False

        current_memory = await self.memories.get_core(user_id)
        transcript = "\n".join(
            f"{m['role'].upper()}: {m.get('content') or ''}"[:2000] for m in old if m.get("content")
        )
        prompt = (
            "Process this conversation and call save_memory with your consolidation.\n\n"
            f"## Current Core Memory\n{current_memory or '(empty)'}\n\n"
            f"## Conversation to Process\n{transcript}"
        )
        try:
            result = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a memory consolidation agent. "
                        "Call the save_memory tool with your consolidation.",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=self.model,
            )
        except Exception:
            logger.exception("Memory consolidation LLM call failed for session {}", session_id)
            return False

        if not result.has_tool_calls:
            logger.warning("Memory consolidation: model did not call save_memory")
            return False
        args: dict[str, Any] = result.tool_calls[0].arguments
        entry = args.get("history_entry")
        update = args.get("memory_update")
        if isinstance(entry, str) and entry.strip():
            await self.memories.append_history(user_id, entry)
        if isinstance(update, str) and update.strip() and update != current_memory:
            await self.memories.set_core(user_id, update)
        await self.sessions.set_consolidated_seq(session_id, cutoff)
        logger.info("Consolidated session {} up to seq {}", session_id, cutoff)
        return True
