"""Knowledge ingestion: turn uploaded files into OKF concepts + a search index.

Uploads are parsed by a background worker pool, not inline in the request, so a
big or scanned document never blocks the event loop or ties up the HTTP request:

    upload → stream to a staging file + create a `pending` doc → enqueue
           → worker: parse/chunk (+ optional OCR) off the loop → OKF write
           → `ready` (or `failed` with a message)

The queue is in-process (single-instance deployment). Interrupted docs are
recovered on startup from their staging file, so a restart mid-ingest doesn't
silently drop an upload.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from loguru import logger

from claw.config import KnowledgeSettings
from claw.db.stores import KnowledgeStore
from claw.knowledge.okf import OkfBundle, render_concept, slugify
from claw.knowledge.parse import parse_document_path


@dataclass
class _IngestJob:
    doc_id: str
    kb_id: str
    filename: str
    mime: str
    staging_path: str


class KnowledgeService:
    def __init__(self, store: KnowledgeStore, root: Path, settings: KnowledgeSettings | None = None):
        self.store = store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = settings or KnowledgeSettings()
        self.staging_dir = self.root / "_staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue[_IngestJob] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        # Serializes the per-bundle commit (concept-id assignment + OKF/index
        # writes + chunk insert) so two workers ingesting into the same base
        # can't race on slug uniqueness or corrupt index.md/log.md.
        self._commit_lock = asyncio.Lock()

    @property
    def max_doc_bytes(self) -> int:
        return self.settings.max_doc_mb * 1_000_000

    def _staging_path(self, doc_id: str, filename: str) -> Path:
        # Keep the original extension so OCR/format detection still works.
        ext = ("." + filename.rsplit(".", 1)[-1]) if "." in (filename or "") else ""
        return self.staging_dir / f"{doc_id}{ext[:16]}"

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        """Spawn the worker pool and re-enqueue any interrupted ingests."""
        n = max(1, self.settings.ingest_concurrency)
        self._workers = [asyncio.create_task(self._worker(), name=f"kb-ingest-{i}") for i in range(n)]
        await self._recover()
        logger.info("Knowledge ingest queue started ({} worker(s))", n)

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._workers = []

    async def _recover(self) -> None:
        """After a crash/restart, resume docs left pending/processing if their
        staging file survived; otherwise mark them failed (don't hang forever)."""
        for doc in await self.store.docs_to_recover():
            path = self._staging_path(doc.id, doc.filename)
            if path.exists():
                await self._queue.put(
                    _IngestJob(doc.id, doc.kb_id, doc.filename, doc.mime, str(path))
                )
            else:
                await self.store.set_doc_status(
                    doc.id, "failed", "ingest was interrupted before processing — please re-upload"
                )

    # -- enqueue (called from the upload endpoint) --------------------------
    async def enqueue_upload(
        self, kb_id: str, filename: str, mime: str, temp_path: str, size: int
    ) -> dict:
        """Register a staged upload and queue it for background parsing. The
        caller has already streamed the bytes to `temp_path`."""
        title = (filename.rsplit(".", 1)[0] if "." in filename else filename) or "Document"
        doc = await self.store.create_pending_doc(
            kb_id=kb_id, title=title, filename=filename, mime=mime, size=size
        )
        # Rename the temp staging file to a doc-id-stable name so recovery can
        # find it again after a restart.
        stable = self._staging_path(doc.id, filename)
        try:
            os.replace(temp_path, stable)
        except OSError:
            # Fall back to a copy if os.replace can't (cross-device, etc.).
            import shutil

            shutil.move(temp_path, stable)
        await self._queue.put(_IngestJob(doc.id, kb_id, filename, mime, str(stable)))
        return {"id": doc.id, "title": title, "filename": filename, "status": "pending"}

    # -- worker -------------------------------------------------------------
    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad doc must not kill the worker
                logger.exception("Knowledge ingest worker crashed on doc {}", job.doc_id)
                try:
                    await self.store.set_doc_status(job.doc_id, "failed", "internal ingest error")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._queue.task_done()

    async def _process(self, job: _IngestJob) -> None:
        await self.store.set_doc_status(job.doc_id, "processing")
        try:
            # CPU-bound (pypdf/OCR/chunking) — off the event loop.
            text, chunks, err = await asyncio.to_thread(
                parse_document_path,
                job.staging_path,
                job.filename,
                job.mime,
                ocr_enabled=self.settings.ocr_enabled,
                ocr_min_chars=self.settings.ocr_min_chars,
                ocr_timeout=self.settings.ocr_timeout_seconds,
            )
            if err:
                await self.store.set_doc_status(job.doc_id, "failed", err)
                return
            if not text.strip():
                await self.store.set_doc_status(
                    job.doc_id, "failed", f"no readable text found in {job.filename}"
                )
                return

            title = (job.filename.rsplit(".", 1)[0] if "." in job.filename else job.filename) or "Document"
            description = " ".join(text.split())[:200]
            # One-at-a-time bundle commit (see _commit_lock).
            async with self._commit_lock:
                bundle = OkfBundle(self.root, job.kb_id)
                concept_id = slugify(job.filename, bundle.existing_concepts())
                content = render_concept(
                    title=title,
                    description=description,
                    body=text,
                    resource=job.filename,
                    tags=["upload"],
                )
                bundle.write_concept(concept_id, content)
                await self.store.finalize_doc(
                    doc_id=job.doc_id, concept_id=concept_id, chars=len(text), chunk_records=chunks
                )
                await self._refresh_bundle(
                    job.kb_id, bundle, action="Upload", title=title, concept_id=concept_id
                )
            logger.info(
                "Knowledge ingest: {} -> kb={} ({} chars, {} chunks)",
                job.filename, job.kb_id, len(text), len(chunks),
            )
        finally:
            try:
                os.remove(job.staging_path)
            except OSError:
                pass

    # -- deletion -----------------------------------------------------------
    async def delete_doc(self, doc_id: str) -> None:
        doc = await self.store.get_doc(doc_id)
        if doc is None:
            return
        kb_id = doc.kb_id
        # Drop any staging file if the doc was still queued/mid-ingest.
        try:
            self._staging_path(doc.id, doc.filename).unlink()
        except OSError:
            pass
        bundle = OkfBundle(self.root, kb_id)
        if doc.concept_id:
            bundle.remove_concept(doc.concept_id)
            self._read_concept_body.cache_clear()
        await self.store.delete_doc(doc_id)
        await self._refresh_bundle(kb_id, bundle, action="Deletion", title=doc.title, concept_id=doc.concept_id)

    # -- preview ------------------------------------------------------------
    async def preview_document(self, doc_id: str, *, offset: int = 0, limit: int = 50_000) -> dict:
        """A bounded slice of a document's extracted text, for the UI preview.

        Reads the canonical OKF concept body (what the agent actually sees), not
        the original file (which isn't retained). Paged via offset so long docs
        don't ship megabytes at once.
        """
        doc = await self.store.get_doc(doc_id)
        if doc is None:
            return {"available": False, "status": "missing"}
        if doc.status != "ready" or not doc.concept_id:
            return {"available": False, "status": doc.status}

        offset = max(0, offset)
        body = await asyncio.to_thread(self._read_concept_body, doc.kb_id, doc.concept_id)
        if body is None:
            return {"available": False, "status": "missing"}
        slice_ = body[offset : offset + limit]
        next_offset = offset + len(slice_)
        return {
            "available": True,
            "title": doc.title,
            "filename": doc.filename,
            "total_chars": len(body),
            "offset": offset,
            "next_offset": next_offset,
            "has_more": next_offset < len(body),
            "text": slice_,
        }

    @lru_cache(maxsize=8)
    def _read_concept_body(self, kb_id: str, concept_id: str) -> str | None:
        """Read the OKF concept file and strip its YAML frontmatter. Returns None
        if the file is missing or escapes the knowledge root (defensive).

        Cached (a doc's concept file is immutable once ingested) so paging
        through a large preview doesn't re-read the whole file per page."""
        path = (self.root / kb_id / f"{concept_id}.md").resolve()
        root = self.root.resolve()
        if root not in path.parents or not path.is_file():
            return None
        raw = path.read_text("utf-8")
        # Frontmatter is a leading `---\n … \n---\n` block; body is what follows.
        if raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end != -1:
                nl = raw.find("\n", end + 1)
                raw = raw[nl + 1 :] if nl != -1 else ""
        return raw.lstrip("\n")

    async def _refresh_bundle(
        self, kb_id: str, bundle: OkfBundle, *, action: str, title: str, concept_id: str
    ) -> None:
        """Regenerate index.md and append a log.md entry after a change."""
        docs = await self.store.list_docs(kb_id)
        # Only list docs that actually have a concept written (ready ones).
        entries = [(d.concept_id, d.title) for d in docs if d.concept_id]
        bundle.write_index([(cid, ttl, "") for cid, ttl in entries])
        bundle.append_log(action, title, concept_id)
