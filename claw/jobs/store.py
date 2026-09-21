"""Transactional job state. The short queue lock serializes admissions across processes.

No network/LLM/tool call is made inside a database transaction. Every worker write
checks both fence and unexpired lease; a cancelled/stale worker cannot settle work.
"""
import json
import random
import time
from contextlib import asynccontextmanager

from sqlalchemy import select, update

from claw.jobs.contracts import JobPolicy, Lease, StepOutcome, StepSpec, digest
from claw.jobs.graph import validate_dag
from claw.jobs.models import Attempt, Delivery, Job, JobEvent, QueueLock, ResourceCall, Step


class LeaseLost(RuntimeError):
    pass


class BudgetExhausted(RuntimeError):
    pass


class JobStore:
    def __init__(self, factory, secret_box, *, clock=time.time, jitter=random.uniform):
        self.factory, self.box, self.clock, self.jitter = factory, secret_box, clock, jitter

    def pack(self, value):
        return self.box.encrypt(json.dumps(value, ensure_ascii=False))

    def unpack(self, value):
        return json.loads(self.box.decrypt(value)) if value else {}

    async def initialize(self):
        # Safe concurrently; also seeded by the migration.
        async with self.factory() as db, db.begin():
            dialect = db.bind.dialect.name
            if dialect == 'postgresql':
                from sqlalchemy.dialects.postgresql import insert
            elif dialect == 'sqlite':
                from sqlalchemy.dialects.sqlite import insert
            else:
                raise RuntimeError('durable jobs require PostgreSQL or SQLite')
            await db.execute(insert(QueueLock).values(id=1, version=0).on_conflict_do_nothing())

    @asynccontextmanager
    async def transaction(self):
        async with self.factory() as db, db.begin():
            result = await db.execute(update(QueueLock).where(QueueLock.id == 1).values(version=QueueLock.version + 1))
            if result.rowcount != 1:
                raise RuntimeError('job queue is not initialized')
            yield db

    def event(self, db, job, step=None):
        job.sequence += 1
        job.updated_at = self.clock()
        payload = {'job_id': job.id, 'sequence': job.sequence, 'mode': job.mode,
                   'session_id': job.session_id, 'status': job.status, 'reason': job.reason,
                   'step_id': step.id if step else None,
                   'actor': self.unpack(step.spec).get('actor', '') if step else '',
                   'step_status': step.status if step else None}
        db.add(JobEvent(job_id=job.id, sequence=job.sequence, payload=self.pack(payload), created_at=self.clock()))

    async def submit(self, job_id, owner_id, session_id, mode, steps, *, policy=None, locale='en', user_message=None, adoption=None, orphan_before=None, pause_reason="", prepared_mission=None):
        if mode not in {'privateclaw', 'sbot'}:
            raise ValueError('unknown mode')
        if orphan_before is not None and not (adoption and adoption.get('journal_id')):
            raise ValueError('orphan recovery requires a journal')
        specs = [StepSpec.model_validate(s).model_dump() for s in steps]
        if not 1 <= len(specs) <= 40:
            raise ValueError('jobs require 1–40 steps')
        validate_dag(specs)
        policy = JobPolicy.model_validate(policy or {}).model_dump()
        if adoption is not None:
            if user_message is not None or (mode == 'privateclaw' and len(specs) != 1):
                raise ValueError('invalid foreground adoption')
            if adoption['active_seconds'] < 0:
                raise ValueError('negative foreground duration')
            for call in adoption['calls']:
                if (not isinstance(call['reserved'], int) or call['reserved'] < 0
                        or (call['actual'] is not None and
                            (not isinstance(call['actual'], int) or call['actual'] < 0))):
                    raise ValueError('invalid foreground usage')
        fingerprint = digest([owner_id, session_id, mode, specs, policy] + ([adoption] if adoption is not None else []))
        async with self.transaction() as db:
            existing = await db.get(Job, job_id)
            if existing:
                if existing.spec_hash != fingerprint:
                    raise ValueError('job id already belongs to another submission')
                return job_id
            if adoption is not None and adoption.get('journal_id'):
                from claw.jobs.models import ForegroundJournal
                journal = await db.get(ForegroundJournal, adoption['journal_id'])
                if (journal is None or journal.owner_id != owner_id or journal.session_id != session_id
                        or journal.mode != mode or journal.status != 'open'
                        or journal.revision != adoption.get('journal_revision')):
                    raise LeaseLost('foreground admission lost ownership')
                if orphan_before is not None and journal.updated_at > orphan_before:
                    raise LeaseLost('foreground execution is still active')
                saved = self.unpack(journal.payload)
                pending = saved.get('checkpoint', {}).get('pending_tool')
                if pending and mode == 'privateclaw' and not (orphan_before is not None and bool(pause_reason)):
                    raise ValueError('unconfirmed foreground tool must be reconciled before adoption')
                if saved['calls'] != adoption['calls'] or adoption['active_seconds'] < saved['active_seconds']:
                    raise ValueError('foreground accounting evidence cannot be discarded')
                journal.status, journal.job_id = 'adopted', job_id
                journal.revision += 1
                journal.updated_at = self.clock()
            job = Job(id=job_id, owner_id=owner_id, session_id=session_id, mode=mode, locale=locale,
                      policy=policy, spec_hash=fingerprint, status='paused' if pause_reason else 'queued', reason=pause_reason, sequence=0,
                      created_at=self.clock(), updated_at=self.clock())
            db.add(job)
            await db.flush()
            for spec in specs:
                db.add(Step(job_id=job_id, id=spec['id'], spec=self.pack(spec),
                            status='paused' if pause_reason else 'queued', reason=pause_reason,
                            checkpoint=self.pack(adoption['checkpoint']) if adoption else ''))
            if adoption is not None:
                if mode == 'sbot':
                    from sbot.db.models import ChatSession
                else:
                    from claw.db.models import ChatSession
                session = await db.get(ChatSession, session_id)
                if not session or session.user_id != owner_id:
                    raise PermissionError('session ownership mismatch')
                job.active_seconds = adoption['active_seconds']
                job.policy = {**job.policy, 'foreground_counts_as_plan_turn': bool(adoption.get('count_plan_turn', False))}
                for receipt in adoption['calls']:
                    call = ResourceCall(id=receipt['id'], job_id=job_id, step_id=specs[0]['id'],
                        fence=0, model=receipt['model'], reserved=receipt['reserved'],
                        actual=receipt['actual'], created_at=self.clock())
                    db.add(call)
                    if call.actual is None:
                        job.reserved_tokens += call.reserved
                    else:
                        job.tokens += call.actual
                        await self._record_usage(db, job, call, receipt['usage'],
                                                 adoption.get('count_plan_turn', False))
            if mode == 'sbot' and any(s['executor'] == 'sbot.node' for s in specs):
                from sbot.db.models import Mission, MissionNode
                mission = await db.get(Mission, job_id)
                if not mission or mission.owner_id != owner_id or mission.status != 'planned':
                    raise ValueError('shared admission requires an unclaimed mission')
                if mission.session_id != session_id:
                    raise PermissionError('mission session ownership mismatch')
                if prepared_mission is not None:
                    from claw.jobs.bot_bridge import prepared_graph
                    nodes = list((await db.scalars(select(MissionNode).where(MissionNode.mission_id == job_id))).all())
                    if (prepared_graph(mission, nodes) != prepared_mission or any(
                            n.status not in {'pending', 'ready'} or n.attempts or n.output or n.artifacts
                            or n.lease_owner or n.started_at for n in nodes)):
                        raise ValueError('prepared mission changed or already executed')
                mission.status = 'queued'
            if user_message is not None:
                from datetime import datetime, timezone
                from sqlalchemy import func
                from claw.db.models import ChatSession, Message
                if mode != 'privateclaw':
                    raise ValueError('message admission is normal-chat only')
                await db.execute(update(ChatSession).where(ChatSession.id == session_id)
                                 .values(updated_at=datetime.now(timezone.utc)))
                session = await db.get(ChatSession, session_id)
                if not session or session.user_id != owner_id:
                    raise PermissionError('session ownership mismatch')
                seq = (await db.scalar(select(func.coalesce(func.max(Message.seq), 0))
                                       .where(Message.session_id == session_id))) + 1
                db.add(Message(id=digest('request:' + job_id)[:32], session_id=session_id,
                               seq=seq, role='user', content=user_message, meta={'job_id': job_id}))
            self.event(db, job)
        return job_id

    async def _guard(self, db, lease):
        job = await db.get(Job, lease.job_id)
        step = await db.get(Step, (lease.job_id, lease.step_id))
        if (not job or not step or job.status != 'running' or step.status != 'running'
                or step.fence != lease.fence or step.worker_id != lease.worker_id
                or step.lease_until <= self.clock()):
            raise LeaseLost('worker no longer owns this attempt')
        return job, step

    def _account(self, job, step):
        now = min(self.clock(), step.lease_until)
        job.active_seconds += max(0, now - step.accounted_at)
        step.accounted_at = now

    async def _fence_siblings(self, db, job, current=None):
        """A suspended job must not leave live attempts consuming execution slots.

        Replay-safe steps retain checkpoints and queue behind the suspended job.
        External effects without a replay contract remain paused for inspection.
        """
        steps = (await db.scalars(select(Step).where(
            Step.job_id == job.id, Step.status == 'running'))).all()
        for sibling in steps:
            if sibling.id == current:
                continue
            self._account(job, sibling)
            attempt = await db.get(Attempt, (job.id, sibling.id, sibling.fence))
            if attempt:
                attempt.status, attempt.finished_at = 'interrupted', self.clock()
            spec = StepSpec.model_validate(self.unpack(sibling.spec))
            safe = spec.effect == 'read' or spec.replay_safe
            sibling.status = 'queued' if safe else 'paused'
            sibling.reason = 'job_suspended' if safe else 'uncertain_effect'
            sibling.fence += 1
            sibling.worker_id, sibling.lease_until = '', 0

    async def claim(self, worker_id, executors, *, total=4, per_owner=2, lease_seconds=60):
        async with self.transaction() as db:
            jobs = list((await db.scalars(select(Job).where(Job.status.in_(['queued', 'running'])).order_by(Job.created_at, Job.id))).all())
            by_id = {j.id: j for j in jobs}
            steps = list((await db.scalars(select(Step).where(Step.job_id.in_(by_id)))).all())
            now = self.clock()
            for step in steps:
                if step.status == 'running' and step.lease_until <= now:
                    job = by_id[step.job_id]
                    self._account(job, step)
                    spec = StepSpec.model_validate(self.unpack(step.spec))
                    step.status = 'queued' if spec.effect == 'read' or spec.replay_safe else 'paused'
                    step.reason = 'worker_lost' if step.status == 'queued' else 'uncertain_effect'
                    siblings = [s for s in steps if s.job_id == job.id and s.id != step.id]
                    job.status = ('paused' if step.status == 'paused' else
                                  'running' if any(s.status == 'running' and s.lease_until > now for s in siblings)
                                  else 'queued')
                    job.reason = step.reason
                    step.worker_id = ''
                    attempt = await db.get(Attempt, (job.id, step.id, step.fence))
                    attempt.status, attempt.finished_at = 'interrupted', now
                    if job.status == 'paused':
                        await self._fence_siblings(db, job, step.id)
                    self.event(db, job, step)
            running = [s for s in steps if s.status == 'running']
            # All ready steps, including specialists, share the same admission slots.
            if len(running) >= total:
                return None
            for job in jobs:
                if job.status not in {'queued', 'running'}:
                    continue
                if sum(by_id[s.job_id].owner_id == job.owner_id for s in running) >= per_owner:
                    continue
                policy = JobPolicy.model_validate(job.policy)
                if job.tokens + job.reserved_tokens >= policy.max_tokens or job.active_seconds >= policy.max_seconds:
                    if job.status == 'running':
                        continue  # in-flight reservations do not cancel their own calls
                    job.status, job.reason = 'paused', 'resource_limit'
                    self.event(db, job)
                    continue
                own = {s.id: s for s in steps if s.job_id == job.id}
                for step in own.values():
                    spec = StepSpec.model_validate(self.unpack(step.spec))
                    if (step.status != 'queued' or step.next_at > now or spec.executor not in executors
                            or any(own[d].status != 'completed' for d in spec.depends_on)):
                        continue
                    if step.checkpoint_version != 1:
                        job.status, job.reason = 'paused', 'unsupported_checkpoint'
                        step.status = 'paused'
                        await self._fence_siblings(db, job, step.id)
                        self.event(db, job, step)
                        break
                    step.fence += 1
                    step.status, step.worker_id = 'running', worker_id
                    step.lease_until = now + lease_seconds
                    step.accounted_at = now
                    job.status, job.reason = 'running', ''
                    db.add(Attempt(job_id=job.id, step_id=step.id, fence=step.fence,
                                   worker_id=worker_id, started_at=now))
                    self.event(db, job, step)
                    return Lease(job.id, step.id, step.fence, worker_id, job.owner_id, job.mode,
                                 spec, self.unpack(step.checkpoint))
        return None

    async def heartbeat(self, lease, lease_seconds=60):
        async with self.transaction() as db:
            job, step = await self._guard(db, lease)
            self._account(job, step)
            if job.active_seconds >= job.policy['max_seconds']:
                job.status, job.reason = 'paused', 'resource_limit'
                step.status = 'paused'
                attempt = await db.get(Attempt, (job.id, step.id, step.fence))
                attempt.status, attempt.finished_at = 'paused', self.clock()
                await self._fence_siblings(db, job, step.id)
                self.event(db, job, step)
                return False
            step.lease_until = self.clock() + lease_seconds
            return True

    async def checkpoint(self, lease, value):
        async with self.transaction() as db:
            _, step = await self._guard(db, lease)
            step.checkpoint = self.pack(value)

    async def reserve(self, lease, call_id, model, tokens):
        if not isinstance(tokens, int) or tokens <= 0:
            raise ValueError('positive token reservation required')
        async with self.transaction() as db:
            job, _ = await self._guard(db, lease)
            old = await db.get(ResourceCall, call_id)
            if old:
                # An attempt may never resend a provider call using an old reservation.
                raise ValueError('provider call id already used')
            if job.tokens + job.reserved_tokens + tokens > job.policy['max_tokens']:
                raise BudgetExhausted('cumulative token allowance exhausted')
            job.reserved_tokens += tokens
            db.add(ResourceCall(id=call_id, job_id=job.id, step_id=lease.step_id, fence=lease.fence,
                                model=model, reserved=tokens, created_at=self.clock()))

    async def reconcile(self, call_id, actual, *, job_id, usage=None, count_plan_turn=False):
        # Accounting is allowed after lease expiry/cancel: spend really happened.
        if not isinstance(actual, int) or actual < 0:
            raise ValueError('nonnegative actual usage required')
        async with self.transaction() as db:
            call = await db.get(ResourceCall, call_id)
            if call is None or call.job_id != job_id:
                raise ValueError('unknown call')
            if call.actual is not None:
                if call.actual != actual:
                    raise ValueError('conflicting usage reconciliation')
                return
            job = await db.get(Job, call.job_id)
            job.reserved_tokens -= call.reserved
            job.tokens += actual
            call.actual = actual
            if usage is not None:
                await self._record_usage(db, job, call, usage, count_plan_turn)

    async def _record_usage(self, db, job, call, usage, count_plan_turn):
        from datetime import datetime, timezone
        from claw.db.models import UsageRecord, UsageDaily
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        prompt, completion = int(usage['prompt_tokens']), int(usage['completion_tokens'])
        if prompt < 0 or completion < 0 or prompt + completion != call.actual:
            raise ValueError('invalid usage breakdown')
        first_id = digest('job-turn:' + job.id)[:32]
        adopted = call.fence == 0 or await db.scalar(select(ResourceCall.id).where(
            ResourceCall.job_id == job.id, ResourceCall.fence == 0).limit(1))
        first = (job.mode == 'privateclaw' or bool(adopted)) and not await db.get(UsageRecord, first_id)
        count_plan_turn = job.policy.get('foreground_counts_as_plan_turn', count_plan_turn)
        db.add(UsageRecord(id=first_id if first else call.id, user_id=job.owner_id,
            session_id=job.session_id, model=call.model, prompt_tokens=prompt,
            completion_tokens=completion, counts_as_turn=first))
        insert = pg_insert if db.bind.dialect.name == 'postgresql' else sqlite_insert
        statement = insert(UsageDaily).values(day=datetime.now(timezone.utc).date(),
            user_id=job.owner_id, model=call.model, prompt_tokens=prompt,
            completion_tokens=completion, turns=int(first), plan_turns=int(first and count_plan_turn))
        await db.execute(statement.on_conflict_do_update(index_elements=['day', 'user_id', 'model'],
            set_={'prompt_tokens': UsageDaily.prompt_tokens + prompt,
                  'completion_tokens': UsageDaily.completion_tokens + completion,
                  'turns': UsageDaily.turns + int(first),
                  'plan_turns': UsageDaily.plan_turns + int(first and count_plan_turn)}))

    async def settle(self, lease, outcome: StepOutcome, *, validated=False):
        async with self.transaction() as db:
            job, step = await self._guard(db, lease)
            self._account(job, step)
            previous_evidence = self.unpack(step.evidence)
            step.checkpoint = self.pack(outcome.checkpoint)
            step.evidence = self.pack(outcome.evidence)
            step.reason, job.reason = outcome.reason, outcome.reason
            now = self.clock()
            if outcome.status == 'completed':
                if not validated:
                    raise ValueError('completion requires an independent validator')
                step.status = 'completed'
                others = list((await db.scalars(select(Step).where(Step.job_id == job.id, Step.id != step.id))).all())
                job.status = ('completed' if all(s.status == 'completed' for s in others) else
                              'running' if any(s.status == 'running' for s in others) else
                              'queued' if any(s.status == 'queued' for s in others) else
                              'waiting_dependency' if any(s.status == 'waiting_dependency' for s in others)
                              else 'paused')
                if outcome.delivery is not None:
                    db.add(Delivery(key=f'{job.id}:{step.id}', job_id=job.id, step_id=step.id,
                                    payload=self.pack(outcome.delivery), created_at=now))
            elif outcome.status == 'dependency':
                step.status = job.status = 'waiting_dependency'
                step.dependency = outcome.dependency
                step.waiting_since = step.waiting_since if step.waiting_since is not None else now
                step.next_at = now + 30
            elif outcome.status in {'retry', 'yielded'}:
                if outcome.status == 'retry':
                    step.recoveries += 1
                if outcome.status == 'yielded':
                    progressed = validated and bool(outcome.evidence) and outcome.evidence != previous_evidence
                    step.no_progress = 0 if progressed else step.no_progress + 1
                safe = lease.spec.effect == 'read' or lease.spec.replay_safe
                if step.no_progress > 2:
                    step.status = job.status = 'paused'
                    job.reason = 'no_progress'
                elif step.recoveries > job.policy['max_recoveries'] or not safe:
                    step.status = job.status = 'paused'
                    job.reason = 'recovery_exhausted' if safe else 'uncertain_effect'
                else:
                    step.status = job.status = 'queued'
                    step.next_at = now + (min(30, 2 ** step.recoveries) if outcome.status == 'retry' else 0)
            else:
                step.status = job.status = outcome.status
            if job.status in {'queued', 'waiting_dependency'}:
                siblings = list((await db.scalars(select(Step).where(Step.job_id == job.id, Step.id != step.id))).all())
                if any(s.status == 'running' for s in siblings):
                    job.status = 'running'
                elif any(s.status == 'queued' for s in siblings):
                    job.status = 'queued'
            if job.status in {'paused', 'failed', 'awaiting_input'}:
                await self._fence_siblings(db, job, step.id)
            if job.mode == 'sbot' and lease.spec.executor == 'sbot.node':
                from sbot.db.models import Mission, MissionNode, MissionBlackboard
                mission = await db.get(Mission, job.id)
                node = await db.get(MissionNode, {'id': step.id, 'mission_id': job.id})
                if mission and node:
                    mission.status = {'waiting_dependency': 'paused', 'queued': 'queued'}.get(job.status, job.status)
                    node.status = 'done' if step.status == 'completed' else 'pending' if step.status == 'queued' else 'error'
                    if step.status == 'completed':
                        node.output = outcome.evidence.get('output', '')
                        contract = outcome.evidence.get('contract', {})
                        node.artifacts = contract.get('artifacts', [])
                        for key, value in {f'result:{step.id}': node.output, f'contract:{step.id}': contract}.items():
                            record = await db.scalar(select(MissionBlackboard).where(
                                MissionBlackboard.mission_id == job.id, MissionBlackboard.key == key))
                            if record:
                                record.value = value
                            else:
                                db.add(MissionBlackboard(mission_id=job.id, key=key, value=value, written_by_node=step.id))
            step.worker_id, step.lease_until = '', 0
            attempt = await db.get(Attempt, (job.id, step.id, lease.fence))
            attempt.status, attempt.finished_at = step.status, now
            if job.status in {'completed', 'failed', 'cancelled'}:
                job.finished_at = now
            self.event(db, job, step)

    async def due_dependencies(self):
        async with self.factory() as db:
            rows = (await db.execute(select(Step, Job).join(Job, Job.id == Step.job_id).where(
                Step.status == 'waiting_dependency', Job.status.in_(['queued', 'running', 'waiting_dependency']), Step.next_at <= self.clock()
            ))).all()
            return [(s.job_id, s.id, s.fence, s.dependency, j.owner_id, j.mode) for s, j in rows]

    async def dependency_result(self, job_id, step_id, fence, ready):
        async with self.transaction() as db:
            job, step = await db.get(Job, job_id), await db.get(Step, (job_id, step_id))
            if (not job or not step or job.status not in {'queued', 'running', 'waiting_dependency'}
                    or step.status != 'waiting_dependency' or step.fence != fence):
                return
            if self.clock() - step.waiting_since >= job.policy['dependency_wait_seconds']:
                job.status = step.status = 'paused'
                job.reason = 'dependency_wait_expired'
                await self._fence_siblings(db, job, step.id)
            elif ready:
                step.next_at = self.clock()
                step.status = 'queued'
                if job.status != 'running':
                    job.status = 'queued'
                job.reason = ''
            else:
                step.probes += 1
                step.next_at = self.clock() + min(900, 30 * 2 ** min(step.probes, 5)) * self.jitter(.8, 1)
                return
            self.event(db, job, step)

    async def control(self, job_id, owner_id, action):
        if action not in {'cancel', 'resume'}:
            raise ValueError('unsupported action')
        async with self.transaction() as db:
            job = await db.get(Job, job_id)
            if not job or job.owner_id != owner_id:
                return False
            if action == 'cancel':
                if job.status in {'completed', 'failed', 'cancelled'}:
                    return False
                job.status, job.reason, job.finished_at = 'cancelled', 'user_cancelled', self.clock()
                steps = (await db.scalars(select(Step).where(Step.job_id == job_id))).all()
                for step in steps:
                    if step.status == 'running':
                        self._account(job, step)
                    if step.status == 'running':
                        attempt = await db.get(Attempt, (job.id, step.id, step.fence))
                        if attempt:
                            attempt.status, attempt.finished_at = 'cancelled', self.clock()
                    if step.status != 'completed':
                        step.status, step.fence = 'cancelled', step.fence + 1
                await db.execute(update(Delivery).where(Delivery.job_id == job_id, Delivery.status == 'pending').values(status='cancelled'))
            else:
                # An API resume cannot authorize an uncertain external replay or reset accounting.
                if job.status != 'paused' or job.reason not in {'dependency_wait_expired', 'worker_lost', 'resource_limit'}:
                    return False
                if job.tokens + job.reserved_tokens >= job.policy['max_tokens'] or job.active_seconds >= job.policy['max_seconds']:
                    return False
                paused = list((await db.scalars(select(Step).where(Step.job_id == job_id, Step.status == 'paused'))).all())
                if any(step.reason == 'uncertain_effect' for step in paused):
                    return False
                job.status, job.reason = 'queued', ''
                for step in paused:
                    step.status, step.waiting_since, step.probes = 'queued', None, 0
            self.event(db, job)
            return True

    async def approve(self, owner_id, job_id, step_id, key, approved):
        async with self.transaction() as db:
            job = await db.get(Job, job_id)
            step = await db.get(Step, (job_id, step_id))
            if not job or job.owner_id != owner_id or job.status != 'awaiting_input' or not step or step.status != 'awaiting_input':
                return False
            checkpoint = self.unpack(step.checkpoint)
            request = checkpoint.get('approval') or {}
            if not key or request.get('key') != key or 'approved' in request:
                return False
            checkpoint['approval'] = {**request, 'approved': bool(approved)}
            step.checkpoint = self.pack(checkpoint)
            job.status = step.status = 'queued'
            job.reason = step.reason = ''
            self.event(db, job, step)
            return True

    async def snapshot(self, owner_id, job_id):
        async with self.factory() as db:
            job = await db.get(Job, job_id)
            if not job or job.owner_id != owner_id:
                return None
            steps = (await db.scalars(select(Step).where(Step.job_id == job_id))).all()
            return {'job_id': job.id, 'mode': job.mode, 'session_id': job.session_id, 'locale': job.locale,
                    'status': job.status, 'reason': job.reason, 'sequence': job.sequence,
                    'deliveries': len(list((await db.scalars(select(Delivery.key).where(Delivery.job_id == job.id, Delivery.status == 'delivered'))).all())),
                    'steps': [{'id': s.id, 'status': s.status, 'reason': s.reason,
                               'actor': self.unpack(s.spec).get('actor', ''),
                               'approval': self.unpack(s.checkpoint).get('approval') if s.status == 'awaiting_input' else None} for s in steps]}

    async def list_jobs(self, owner_id, session_id=None):
        async with self.factory() as db:
            query = select(Job.id).where(Job.owner_id == owner_id).order_by(Job.created_at.desc()).limit(100)
            if session_id:
                from sqlalchemy import or_
                from sbot.db.models import ChatSession as BotSession, Bot, MissionNode
                room = await db.get(BotSession, session_id)
                actor_jobs = select(Job.id).where(False)
                if room and room.user_id == owner_id and room.bot_id:
                    bot = await db.get(Bot, room.bot_id)
                    if bot and bot.owner_id == owner_id and not bot.is_archived:
                        # Keep high-frequency room polling in SQL. Decrypting every
                        # historical Step.spec here made latency grow with an
                        # owner's lifetime job count rather than visible jobs.
                        actor_jobs = (select(MissionNode.mission_id).join(
                            Job, Job.id == MissionNode.mission_id
                        ).where(Job.owner_id == owner_id, Job.mode == 'sbot',
                                MissionNode.bot_id == room.bot_id))
                query = query.where(or_(Job.session_id == session_id, Job.id.in_(actor_jobs)))
            ids = list((await db.scalars(query)).all())
        return [await self.snapshot(owner_id, job_id) for job_id in ids]

    async def events(self, owner_id, job_id, after=0):
        if await self.snapshot(owner_id, job_id) is None:
            return None
        async with self.factory() as db:
            rows = (await db.scalars(select(JobEvent).where(JobEvent.job_id == job_id,
                JobEvent.sequence > after).order_by(JobEvent.sequence).limit(500))).all()
            return [self.unpack(row.payload) for row in rows]

    async def pause_waiting(self, job_id, step_id, fence, reason):
        async with self.transaction() as db:
            job, step = await db.get(Job, job_id), await db.get(Step, (job_id, step_id))
            if (job and step and job.status in {'queued', 'running', 'waiting_dependency'}
                    and step.status == 'waiting_dependency' and step.fence == fence):
                job.status = step.status = 'paused'
                job.reason = reason
                await self._fence_siblings(db, job, step.id)
                self.event(db, job, step)

    async def publish_deliveries(self, limit=50):
        """Materialize outbox + chat message atomically in the same database.

        Notifications are only hints; clients can replay the durable job event.
        A crash before commit rolls back both; after commit the row is delivered.
        """
        from datetime import datetime, timezone
        from sqlalchemy import func
        from claw.db.models import ChatSession, Message, User
        from sbot.db.models import ChatSession as BotSession, Message as BotMessage, Bot, BotChatGroup
        delivered = 0
        async with self.transaction() as db:
            rows = (await db.scalars(select(Delivery).where(Delivery.status == 'pending')
                                    .order_by(Delivery.created_at).limit(limit))).all()
            for delivery in rows:
                job = await db.get(Job, delivery.job_id)
                user = await db.get(User, job.owner_id)
                if job.status == 'cancelled' or not user or not user.is_active:
                    delivery.status = 'cancelled'
                    continue
                payload = self.unpack(delivery.payload)
                session_type, message_type = (ChatSession, Message) if job.mode == 'privateclaw' else (BotSession, BotMessage)
                session_id = payload.get('session_id', job.session_id)
                # Same row lock as MessageStore.append; avoids seq collisions.
                await db.execute(update(session_type).where(session_type.id == session_id)
                                 .values(updated_at=datetime.now(timezone.utc)))
                session = await db.get(session_type, session_id)
                if not session or session.user_id != job.owner_id:
                    delivery.status = 'rejected'
                    continue
                kwargs = {}
                if job.mode == 'sbot':
                    step = await db.get(Step, (job.id, delivery.step_id))
                    actor = self.unpack(step.spec).get('actor')
                    if actor:
                        bot = await db.get(Bot, actor)
                        target_matches = session.bot_id == actor
                        if session.kind == 'group' and session_id == job.session_id and step.id == '__summary':
                            group = await db.get(BotChatGroup, session.group_id)
                            target_matches = bool(group and group.owner_id == job.owner_id and group.leader_id == actor and actor in group.member_ids)
                        origin = await db.get(BotSession, job.session_id)
                        if origin and origin.kind == 'group':
                            group = await db.get(BotChatGroup, origin.group_id)
                            target_matches = target_matches and bool(group and group.owner_id == job.owner_id and actor in group.member_ids)
                        if not bot or bot.is_archived or bot.owner_id != job.owner_id or not target_matches:
                            delivery.status = 'rejected'
                            continue
                        kwargs['speaker_bot_id'] = actor
                elif session_id != job.session_id:
                    delivery.status = 'rejected'
                    continue
                message_id = digest(delivery.key)[:32]
                if not await db.get(message_type, message_id):
                    seq = (await db.scalar(select(func.coalesce(func.max(message_type.seq), 0))
                                          .where(message_type.session_id == session_id))) + 1
                    db.add(message_type(id=message_id, session_id=session_id, seq=seq, role='assistant',
                                        content=payload.get('content', ''),
                                        meta={**payload.get('meta', {}), 'job_id': job.id, 'delivery_key': delivery.key},
                                        **kwargs))
                delivery.status = 'delivered'
                self.event(db, job)
                delivered += 1
        return delivered

    async def prune(self, checkpoint_days=7, audit_days=30):
        """Expire recovery payloads; never delete workspace/delivered artifact files."""
        from sqlalchemy import delete
        now = self.clock()
        removed = 0
        async with self.transaction() as db:
            from claw.jobs.models import ForegroundJournal
            journals = (await db.scalars(select(ForegroundJournal).where(
                ForegroundJournal.updated_at <= now - checkpoint_days * 86400))).all()
            for journal in journals:
                if now - journal.updated_at >= audit_days * 86400:
                    await db.delete(journal)
                else:
                    payload = self.unpack(journal.payload)
                    payload['checkpoint'] = {}
                    journal.payload = self.pack(payload)
                    if journal.status == 'open':
                        journal.status = 'expired'
                        journal.revision += 1
            jobs = (await db.scalars(select(Job).where(Job.status.in_(
                ['paused', 'awaiting_input', 'completed', 'failed', 'cancelled'])))).all()
            for job in jobs:
                stamp = job.finished_at if job.finished_at is not None else job.updated_at
                if now - stamp >= checkpoint_days * 86400:
                    if job.status in {'paused', 'awaiting_input'}:
                        job.status, job.reason, job.finished_at = 'failed', 'checkpoint_expired', now
                        self.event(db, job)
                    for step in (await db.scalars(select(Step).where(Step.job_id == job.id))).all():
                        spec = self.unpack(step.spec)
                        spec['inputs'] = {}
                        spec['acceptance'] = {}
                        step.spec, step.checkpoint, step.evidence = self.pack(spec), '', ''
                if job.finished_at is not None and now - job.finished_at >= audit_days * 86400:
                    for model in [Delivery, JobEvent, ResourceCall, Attempt, Step]:
                        await db.execute(delete(model).where(model.job_id == job.id))
                    await db.delete(job)
                    removed += 1
        return removed
