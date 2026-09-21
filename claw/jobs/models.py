"""Additive tables; sensitive step inputs, checkpoints and event bodies are encrypted."""
from sqlalchemy import ForeignKey, Index, Integer, Float, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from claw.db.models import Base
from claw.db.types import NulSafeJSON as JSON, NulSafeString as String, NulSafeText as Text


class QueueLock(Base):
    __tablename__ = 'agent_queue_locks'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)


class Job(Base):
    __tablename__ = 'agent_jobs'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    locale: Mapped[str] = mapped_column(String(8), default='en')
    status: Mapped[str] = mapped_column(String(32), index=True, default='queued')
    reason: Mapped[str] = mapped_column(String(64), default='')
    spec_hash: Mapped[str] = mapped_column(String(64))
    policy: Mapped[dict] = mapped_column(JSON)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    active_seconds: Mapped[float] = mapped_column(Float, default=0)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Step(Base):
    __tablename__ = 'agent_steps'
    job_id: Mapped[str] = mapped_column(ForeignKey('agent_jobs.id'), primary_key=True)
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default='queued')
    spec: Mapped[str] = mapped_column(Text)
    checkpoint: Mapped[str] = mapped_column(Text, default='')
    checkpoint_version: Mapped[int] = mapped_column(Integer, default=1)
    evidence: Mapped[str] = mapped_column(Text, default='')
    fence: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str] = mapped_column(String(64), default='')
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    accounted_at: Mapped[float] = mapped_column(Float, default=0)
    next_at: Mapped[float] = mapped_column(Float, default=0, index=True)
    waiting_since: Mapped[float | None] = mapped_column(Float, nullable=True)
    dependency: Mapped[str] = mapped_column(String(64), default='')
    recoveries: Mapped[int] = mapped_column(Integer, default=0)
    no_progress: Mapped[int] = mapped_column(Integer, default=0)
    probes: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(64), default='')


class Attempt(Base):
    __tablename__ = 'agent_attempts'
    job_id: Mapped[str] = mapped_column(ForeignKey('agent_jobs.id'), primary_key=True)
    step_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    fence: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default='running')


class ResourceCall(Base):
    __tablename__ = 'agent_resource_calls'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey('agent_jobs.id'), index=True)
    step_id: Mapped[str] = mapped_column(String(48))
    fence: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(255))
    reserved: Mapped[int] = mapped_column(Integer)
    actual: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class JobEvent(Base):
    __tablename__ = 'agent_job_events'
    job_id: Mapped[str] = mapped_column(ForeignKey('agent_jobs.id'), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)


class Delivery(Base):
    __tablename__ = 'agent_deliveries'
    __table_args__ = (UniqueConstraint('job_id', 'step_id', name='uq_agent_delivery_step'),)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey('agent_jobs.id'), index=True)
    step_id: Mapped[str] = mapped_column(String(48))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default='pending')
    created_at: Mapped[float] = mapped_column(Float)


Index('ix_agent_steps_lease', Step.status, Step.lease_until)


class ForegroundJournal(Base):
    __tablename__ = 'agent_foreground_journals'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default='open', index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[float] = mapped_column(Float)
