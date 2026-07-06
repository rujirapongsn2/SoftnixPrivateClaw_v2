"""Knowledge ingestion: turn an uploaded file into an OKF concept + search index.

The user just uploads a document; this service handles everything else —
parsing (pdf/docx/html/md/txt), chunking, writing the OKF bundle files
(concept `.md` + regenerated `index.md` + appended `log.md`), and inserting the
searchable chunks into the DB. No format knowledge required of the user.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from claw.db.stores import KnowledgeStore
from claw.knowledge.okf import OkfBundle, render_concept, slugify
from claw.knowledge.parse import chunk_text, extract_text


class KnowledgeService:
    def __init__(self, store: KnowledgeStore, root: Path):
        self.store = store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def ingest(self, kb_id: str, filename: str, mime: str, data: bytes) -> dict:
        """Parse + chunk + persist one uploaded document. Raises ValueError on
        an unreadable/empty file so the API can return a clean 400."""
        text, err = extract_text(data, filename, mime)
        if err:
            raise ValueError(err)
        if not text.strip():
            raise ValueError(f"no readable text found in {filename}")

        chunks = chunk_text(text)
        title = (filename.rsplit(".", 1)[0] if "." in filename else filename) or "Document"
        description = " ".join(text.split())[:200]

        bundle = OkfBundle(self.root, kb_id)
        concept_id = slugify(filename, bundle.existing_concepts())
        content = render_concept(
            title=title,
            description=description,
            body=text,
            resource=filename,
            tags=["upload"],
        )
        bundle.write_concept(concept_id, content)

        doc = await self.store.add_doc(
            kb_id=kb_id,
            concept_id=concept_id,
            title=title,
            filename=filename,
            mime=mime,
            size=len(data),
            chars=len(text),
            chunk_texts=chunks,
        )
        await self._refresh_bundle(kb_id, bundle, action="Upload", title=title, concept_id=concept_id)
        logger.info("Knowledge ingest: {} -> kb={} ({} chars, {} chunks)", filename, kb_id, len(text), len(chunks))
        return {
            "id": doc.id,
            "concept_id": concept_id,
            "title": title,
            "filename": filename,
            "chars": len(text),
            "chunks": len(chunks),
        }

    async def delete_doc(self, doc_id: str) -> None:
        doc = await self.store.get_doc(doc_id)
        if doc is None:
            return
        kb_id = doc.kb_id
        bundle = OkfBundle(self.root, kb_id)
        bundle.remove_concept(doc.concept_id)
        await self.store.delete_doc(doc_id)
        await self._refresh_bundle(kb_id, bundle, action="Deletion", title=doc.title, concept_id=doc.concept_id)

    async def _refresh_bundle(
        self, kb_id: str, bundle: OkfBundle, *, action: str, title: str, concept_id: str
    ) -> None:
        """Regenerate index.md and append a log.md entry after a change."""
        docs = await self.store.list_docs(kb_id)
        entries = [(d.concept_id, d.title, (d.title or d.concept_id)) for d in docs]
        # Use each doc's own title as its index description (we don't re-read
        # frontmatter here); keeps index.md a faithful listing.
        bundle.write_index([(cid, ttl, "") for cid, ttl, _ in entries])
        bundle.append_log(action, title, concept_id)
