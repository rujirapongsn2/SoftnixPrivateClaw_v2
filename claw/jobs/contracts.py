"""Versioned contracts. Model prose is never execution or completion evidence."""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, Field


class JobPolicy(BaseModel):
    max_tokens: int = Field(default=5_000_000, ge=1)
    max_seconds: float = Field(default=21_600, gt=0)
    dependency_wait_seconds: float = Field(default=86_400, gt=0)
    max_parallel_total: int = Field(default=4, ge=1)
    max_parallel_owner: int = Field(default=2, ge=1)
    max_recoveries: int = Field(default=2, ge=0)
    headroom: float = Field(default=1.5, ge=1)


class StepSpec(BaseModel):
    id: str = Field(min_length=1, max_length=48, pattern=r'^[a-zA-Z0-9_-]+$')
    executor: str = Field(min_length=1, max_length=64)
    actor: str = Field(default='', max_length=64)
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict = Field(default_factory=dict)
    acceptance: dict = Field(default_factory=dict)
    effect: Literal['read', 'local', 'external'] = 'read'
    replay_safe: bool = False


@dataclass(frozen=True)
class Lease:
    job_id: str
    step_id: str
    fence: int
    worker_id: str
    owner_id: str
    mode: str
    spec: StepSpec
    checkpoint: dict


@dataclass
class ToolResult:
    status: Literal['ok', 'error', 'unknown']
    text: str = ''
    error_code: str = ''
    dependency: str = ''
    retryable: bool = False
    output_refs: list[dict] = field(default_factory=list)
    receipt: dict = field(default_factory=dict)

    @classmethod
    def legacy(cls, text: str):
        # No inference from rendered error strings or green UI tool badges.
        return cls(status='unknown', text=text)

    @property
    def fingerprint(self) -> str:
        return sha256(f'{self.error_code}:{self.dependency}'.encode()).hexdigest()


@dataclass
class StepOutcome:
    status: Literal['completed', 'yielded', 'dependency', 'retry', 'paused', 'failed', 'awaiting_input']
    checkpoint: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    reason: str = ''
    dependency: str = ''
    delivery: dict | None = None


def digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
