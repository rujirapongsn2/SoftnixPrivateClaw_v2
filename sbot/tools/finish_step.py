"""Explicit mission completion and validated, immutable file delivery."""

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import ClassVar
from sbot.core.verification import check_file, MAX_FILE_BYTES

from sbot.tools.artifacts import PublishArtifactTool
from sbot.tools.base import Tool


class FinishStepTool(Tool):
    name = "finish_step"
    ends_turn_on_success = True
    wants_deadline = True
    description = (
        "Record this mission step result. Required before ending your reply. "
        "Use blocked or failed if unable to meet the instruction. For file tasks, "
        "list every requested deliverable. Only use completed after checking the acceptance criteria."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
            "summary": {"type": "string"},
            "evidence": {
                "type": "string",
                "description": "What was checked, results, and remaining limitations.",
            },
            "files": {"type": "array", "items": {"type": "string"}},
            "needs_input": {
                "type": "boolean",
                "description": "Only for blocked: a specific user decision or missing input is required.",
            },
        },
        "required": ["status", "summary", "evidence", "files"],
    }

    def __init__(
        self,
        workspace: Path,
        required_files: list[str] | None = None,
        verifier=None,
        verification_mode="off",
        delivery=None,
    ):
        self.publisher = PublishArtifactTool(workspace)
        self.required_files = required_files or []
        self.result = None
        self.delivery = delivery
        self.verifier = verifier
        self.verification_mode = verification_mode

    async def execute(self, status, summary, evidence, files, needs_input=False, deadline=None, **kwargs):
        self.pending_result = None
        if self.verifier is not None and deadline is not None:
            self.verifier.task_deadline = min(self.verifier.task_deadline, deadline)
        try:
            async with asyncio.timeout_at(deadline):
                return await self._execute(status, summary, evidence, files, needs_input=needs_input)
        except TimeoutError:
            self.result = self.pending_result or {
                "summary": summary,
                "evidence": evidence,
                "artifacts": [],
                "checks": [],
                "verification_status": "not_verified",
            }
            self.result.update(
                status="partial",
                failure_reason="completion_budget_exhausted",
                verification_cost=dict(self.verifier.cost) if self.verifier else {},
            )
            if self.delivery is not None:
                self.result.setdefault("delivery", {})["local"] = "not_confirmed"
            return "Completion budget exhausted. Partial result recorded; do not claim delivery succeeded."

    async def _execute(self, status, summary, evidence, files, needs_input=False):
        self.result = None
        if status not in {"completed", "blocked", "failed"}:
            return "Error: invalid completion status."
        if not summary.strip() or not evidence.strip():
            return "Error: provide a summary and concrete acceptance evidence."
        if not isinstance(files, list) or len(files) > 50 or any(not isinstance(p, str) for p in files):
            return "Error: files must contain at most 50 paths."
        artifacts = []
        checks = []
        reviews = []
        verification_status = "not_verified"
        try:
            if status == "completed":
                submitted = {self.publisher._resolve(raw) for raw in files}
                if any(self.publisher._resolve(raw) not in submitted for raw in self.required_files):
                    return "Error: files must include every required deliverable: " + ", ".join(
                        self.required_files
                    )
                # Snapshot first: verification and publication must refer to the
                # same bytes even if a parallel worker edits the source.
                with tempfile.TemporaryDirectory(
                    prefix=".verify-", dir=self.publisher.workspace
                ) as directory:
                    snapshots = []
                    seen_sources = set()
                    total_bytes = 0
                    for raw in dict.fromkeys(files):
                        source = self.publisher._resolve(raw)
                        if source in seen_sources:
                            continue
                        seen_sources.add(source)
                        if not source.is_file() or not 0 < source.stat().st_size <= MAX_FILE_BYTES:
                            return f"Error: missing or empty deliverable (or publication size limit): {raw}"
                        total_bytes += source.stat().st_size
                        if total_bytes > 200 * 1024 * 1024:
                            return "Error: combined deliverables exceed the 200 MB snapshot limit."
                        snapshot = Path(directory) / source.relative_to(self.publisher.workspace)
                        snapshot.parent.mkdir(parents=True, exist_ok=True)
                        await asyncio.to_thread(shutil.copyfile, source, snapshot)
                        checks.append(await asyncio.to_thread(check_file, snapshot, raw))
                        checks[-1]["source_relative"] = source.relative_to(
                            self.publisher.workspace
                        ).as_posix()
                        snapshots.append(snapshot)
                    if self.verifier is not None and self.verification_mode != "off":
                        for snapshot, check in zip(snapshots, checks):
                            if snapshot.suffix.lower() in {".docx", ".pptx", ".pdf"}:
                                reviews.append(await self.verifier.review(snapshot, check))
                        if hasattr(self.verifier, "review_task"):
                            reviews.extend(await self.verifier.review_task(Path(directory), checks, summary))
                        if reviews:
                            verification_status = (
                                "failed"
                                if any(r["status"] == "failed" for r in reviews)
                                else "passed"
                                if all(r["status"] == "passed" for r in reviews)
                                and (
                                    len(reviews) == len(snapshots)
                                    or any(r["kind"] in {"code_review", "research_review"} for r in reviews)
                                )
                                else "not_verified"
                            )
                        if (
                            self.verification_mode == "enforce"
                            and reviews
                            and verification_status != "passed"
                        ):
                            self.result = {
                                "status": "partial",
                                "summary": summary,
                                "evidence": evidence,
                                "artifacts": [],
                                "checks": checks + reviews,
                                "verification_status": verification_status,
                                "failure_reason": "verification_incomplete",
                                "verification_cost": dict(self.verifier.cost),
                            }
                            return "Verification did not pass. Result recorded as partial; files were not published."
                    self.pending_result = {
                        "summary": summary,
                        "evidence": evidence,
                        "artifacts": artifacts,
                        "checks": checks + reviews,
                        "verification_status": verification_status,
                        "delivery": {"created": "completed", "published": "partial"},
                    }
                    # Validate all files before publishing any of them.
                    for snapshot, check in zip(snapshots, checks):

                        def collect(event):
                            artifacts.append(event["path"])
                            check["artifact"] = event["path"]

                        answer = await self.publisher.execute(
                            str(snapshot), progress=collect, content_hash=check["sha256"]
                        )
                        if answer.startswith("Error"):
                            return answer
        except (ValueError, OSError, TypeError) as exc:
            return f"Error: {exc}"
        delivery = {
            "created": "completed" if checks else "not_required",
            "published": "completed" if artifacts else "not_required",
        }
        self.pending_result = {
            "status": status,
            "summary": summary,
            "evidence": evidence,
            "artifacts": artifacts,
            "checks": checks + reviews,
            "verification_status": verification_status,
            "delivery": delivery,
        }
        if status == "completed" and self.delivery is not None:
            delivery, receipts = await self.delivery.send(checks, self.publisher.workspace)
            checks.extend(receipts)
            if delivery.get("local") != "completed":
                status = "partial"
        self.result = {
            "status": status,
            "summary": summary,
            "evidence": evidence,
            "artifacts": artifacts,
            "checks": checks + reviews,
            "delivery": delivery,
            "failure_reason": (
                "local_delivery_incomplete"
                if status == "partial"
                else "waiting_for_user"
                if status == "blocked" and needs_input
                else None
            ),
            "verification_cost": dict(self.verifier.cost) if self.verifier else {},
            # Structural validation cannot assert semantic/visual quality.
            "verification_status": verification_status,
        }
        return "Step result recorded."
