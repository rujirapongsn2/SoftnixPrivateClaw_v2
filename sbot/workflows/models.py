"""Dynamic workflow data model."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class WorkflowStep:
    title: str
    instruction: str
    output: str = ""
    status: str = "pending"  # pending|running|done|error


@dataclass(slots=True)
class WorkflowPlan:
    request: str
    steps: list[WorkflowStep] = field(default_factory=list)


@dataclass(slots=True)
class WorkflowResult:
    plan: WorkflowPlan
    final_output: str
    status: str = "completed"  # completed|failed
