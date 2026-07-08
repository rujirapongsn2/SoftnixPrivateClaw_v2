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
        "matching excerpts with their source document; cite the source in your answer.\n"
        "Retrieval is keyword/phrase based, so recall improves a lot when you provide "
        "several phrasings via `queries`: include synonyms, and — importantly — BOTH "
        "Thai and English wordings for the key terms (e.g. for 'นโยบายการคืนเงิน' also pass "
        "'refund policy', 'เงื่อนไขการคืนเงิน'). Results are merged and ranked by the "
        "best-matching phrasing. Optionally pass `knowledge_base` to restrict to one base."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The main thing to look for (a question or keywords)."},
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional extra phrasings of the same information need — synonyms and "
                    "the other language (Thai↔English). Strongly recommended for better recall."
                ),
            },
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

    async def execute(
        self, query: str, queries: list[str] | None = None, knowledge_base: str = "", **_: Any
    ) -> str:
        bases = await self.store.list_accessible(self.user_id)
        if not bases:
            return "No knowledge bases are available. Ask the user to upload documents in Settings → Knowledge."
        if knowledge_base:
            needle = knowledge_base.strip().lower()
            filtered = [b for b in bases if needle in b["name"].lower()]
            bases = filtered or bases
        ids = [b["id"] for b in bases]
        # Merge the main query with any extra phrasings the agent supplied.
        variants = [query, *(queries or [])]
        results = await self.store.search_multi(variants, ids, limit=6)
        if not results:
            names = ", ".join(b["name"] for b in bases)
            return f"No matching passages found in the knowledge base(s): {names}."

        out: list[str] = []
        total = 0
        for r in results:
            source = f"{r.get('kb_name') or 'knowledge'} / {r.get('title') or 'document'}"
            # Attribute to a page when the source had one (PDF), so the model can
            # cite it precisely; older/paged-less chunks simply omit the page.
            if r.get("page"):
                source += f", p.{r['page']}"
            block = f"[source: {source}]\n{r['text']}"
            if total + len(block) > _MAX_CHARS:
                break
            out.append(block)
            total += len(block)
        return "\n\n---\n\n".join(out)
