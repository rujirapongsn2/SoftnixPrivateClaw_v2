"""search_knowledge tool: retrieve passages from the user's knowledge bases."""

from typing import Any

from claw.db.stores import KnowledgeStore
from claw.tools.base import Tool

_MAX_CHARS = 6000


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    description = (
        "Search the user's knowledge bases (their uploaded documents) for passages "
        "relevant to a question, and answer from them. Call this whenever the user asks "
        "about information that may live in uploaded/reference documents. Returns the best "
        "matching excerpts with their source document; cite the source in your answer. "
        "Optionally pass `knowledge_base` to restrict the search to one base by name."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for (a question or keywords)"},
            "knowledge_base": {
                "type": "string",
                "description": "Optional knowledge base name to restrict the search to.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, store: KnowledgeStore, user_id: str):
        self.store = store
        self.user_id = user_id

    async def execute(self, query: str, knowledge_base: str = "", **_: Any) -> str:
        bases = await self.store.list_accessible(self.user_id)
        if not bases:
            return "No knowledge bases are available. Ask the user to upload documents in Settings → Knowledge."
        if knowledge_base:
            needle = knowledge_base.strip().lower()
            filtered = [b for b in bases if needle in b["name"].lower()]
            bases = filtered or bases
        ids = [b["id"] for b in bases]
        results = await self.store.search(query, ids, limit=6)
        if not results:
            names = ", ".join(b["name"] for b in bases)
            return f"No matching passages found in the knowledge base(s): {names}."

        out: list[str] = []
        total = 0
        for r in results:
            source = f"{r.get('kb_name') or 'knowledge'} / {r.get('title') or 'document'}"
            block = f"[source: {source}]\n{r['text']}"
            if total + len(block) > _MAX_CHARS:
                break
            out.append(block)
            total += len(block)
        return "\n\n---\n\n".join(out)
