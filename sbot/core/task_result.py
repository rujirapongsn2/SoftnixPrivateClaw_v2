"""Runtime-owned completion contract; prose is never verification evidence."""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TaskStatus = Literal["completed", "partial", "blocked", "failed"]
VerificationStatus = Literal["passed", "failed", "not_verified", "not_required"]


@dataclass(slots=True)
class TaskResult:
    status: TaskStatus
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str | None = None
    cost: dict[str, float] = field(default_factory=dict)
    verification_status: VerificationStatus = "not_verified"
    delivery: dict[str, str] = field(default_factory=dict)
    attempts: int = 1
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def scheduler_status(self) -> str:
        if self.status == "blocked" and self.failure_reason == "waiting_for_user":
            return "awaiting_human"
        return "done" if self.status == "completed" else "error"

    @classmethod
    def from_outcome(
        cls, outcome: Any, completion: dict | None = None, require_completion: bool = False
    ) -> "TaskResult":
        text = (outcome.text or "").strip()
        artifacts = list(outcome.artifacts or [])
        reason = None
        status: TaskStatus = "completed"
        verification: VerificationStatus = "not_verified"
        evidence = []
        if completion is not None:
            text = completion["summary"]
            artifacts = list(completion["artifacts"])
            status = completion["status"]
            evidence = list(completion.get("checks", []))
            verification = completion.get("verification_status", "not_verified")
            if status != "completed":
                reason = completion.get("failure_reason") or status
        elif outcome.cut_off:
            status = "partial" if text or artifacts else "failed"
            reason = ("timeout" if outcome.timed_out else "output_limit"
                      if getattr(outcome, 'output_truncated', False) else "iteration_limit")
        elif require_completion:
            status = "partial" if text or artifacts else "failed"
            reason = "missing_completion_record"
        elif not text:
            status = "failed"
            reason = "empty_output"
        # Attachments alone are not a completion record, and a user-facing
        # fallback message must be generated AFTER this classification.
        cost = dict(outcome.cost)
        for key, value in (completion or {}).get("verification_cost", {}).items():
            cost[key] = cost.get(key, 0) + value
        return cls(
            status,
            text,
            artifacts,
            evidence,
            reason,
            cost,
            verification,
            dict((completion or {}).get("delivery", {})),
        )
