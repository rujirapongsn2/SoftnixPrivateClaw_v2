"""Independent worker with injected, trusted mode executors and validators.

Run using ``python -m claw.jobs.worker --factory package.module:function``.
The factory is operator configuration, never a callable supplied by an agent.
It returns a Worker; it must not start the HTTP application's lifespan.
"""
import argparse
import asyncio
import importlib
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from claw.jobs.contracts import Lease, StepOutcome
from claw.jobs.store import BudgetExhausted, LeaseLost
from claw.jobs.provider import current_execution


@dataclass
class ExecutionContext:
    store: object
    lease: Lease
    connector_authorize: object = None

    state: dict = field(init=False)

    def __post_init__(self):
        self.state = dict(self.lease.checkpoint)

    async def checkpoint(self, value):
        await self.store.checkpoint(self.lease, value)
        self.state = value

    async def reserve_call(self, model, tokens):
        call_id = uuid.uuid4().hex
        await self.store.reserve(self.lease, call_id, model, tokens)
        return call_id

    async def record_usage(self, call_id, tokens, usage=None):
        await self.store.reconcile(call_id, tokens, job_id=self.lease.job_id, usage=usage,
                                   count_plan_turn=getattr(self, 'count_plan_turn', False))


def valid_worker_process_contract(worker):
    """Accept workers loaded through either package or ``python -m`` identity."""
    return (callable(getattr(worker, 'serve', None))
            and callable(getattr(worker, 'run_once', None))
            and isinstance(getattr(worker, 'stopping', None), asyncio.Event))


@dataclass(frozen=True)
class Executor:
    execute: Callable[[ExecutionContext], Awaitable[StepOutcome]]
    validate: Callable[[ExecutionContext, StepOutcome], Awaitable[bool]]


class Worker:
    def __init__(self, store, executors, authorize, *, probes=None, total=4, per_owner=2,
                 heartbeat_seconds=15, lease_seconds=60, slice_seconds=600, connector_authorize=None):
        if not 0 < heartbeat_seconds < lease_seconds or slice_seconds <= 0:
            raise ValueError('invalid worker timing')
        self.store, self.executors, self.authorize = store, executors, authorize
        self.connector_authorize = connector_authorize
        self.probes = probes or {}
        self.recover_foreground = None
        self.total, self.per_owner = total, per_owner
        self.heartbeat_seconds, self.lease_seconds = heartbeat_seconds, lease_seconds
        self.slice_seconds = slice_seconds
        self.id = uuid.uuid4().hex
        self.stopping = asyncio.Event()

    async def _pulse(self, lease):
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not await self.authorize(lease.owner_id, lease.mode, lease.job_id):
                raise PermissionError('job authorization changed')
            if not await self.store.heartbeat(lease, self.lease_seconds):
                raise LeaseLost('active time allowance exhausted')

    async def run_once(self):
        lease = await self.store.claim(self.id, set(self.executors), total=self.total,
                                       per_owner=self.per_owner, lease_seconds=self.lease_seconds)
        if lease is None:
            return False
        context = ExecutionContext(self.store, lease, self.connector_authorize)
        if not await self.authorize(lease.owner_id, lease.mode, lease.job_id):
            await self.store.settle(lease, StepOutcome('paused', reason='permission_denied'))
            return True
        executor = self.executors[lease.spec.executor]

        async def execute_and_validate():
            token = current_execution.set(context)
            try:
                async with asyncio.timeout(self.slice_seconds):
                    result = await executor.execute(context)
                    valid = result.status in {'completed', 'yielded'} and await executor.validate(context, result)
                    if result.status == 'completed' and not valid:
                        result = StepOutcome('paused', checkpoint=result.checkpoint,
                                             evidence=result.evidence, reason='validation_failed')
                    return result, valid
            finally:
                current_execution.reset(token)

        pulse = asyncio.create_task(self._pulse(lease))
        work = asyncio.create_task(execute_and_validate())
        try:
            done, _ = await asyncio.wait({pulse, work}, return_when=asyncio.FIRST_COMPLETED)
            if pulse in done:
                await pulse
            result, valid = await work
            await self.store.settle(lease, result, validated=valid)
        except BudgetExhausted:
            await self._pause(lease, 'resource_limit')
        except PermissionError:
            await self._pause(lease, 'permission_denied')
        except TimeoutError:
            # Do not discard the checkpoint most recently saved by the executor.
            checkpoint = await self._checkpoint(lease)
            await self.store.settle(lease, StepOutcome('yielded', checkpoint=checkpoint, reason='slice_timeout'))
        except LeaseLost:
            pass  # stale writes are forbidden, including failure reports
        except asyncio.CancelledError:
            # Shutdown leaves the lease to expire. Unsafe effects require inspection.
            raise
        except Exception:
            from loguru import logger
            logger.exception("Durable worker failed job={} step={}", lease.job_id, lease.step_id)
            # Unknown adapter/validator failures must never trigger blind replay.
            await self._pause(lease, 'executor_error')
        finally:
            pulse.cancel()
            work.cancel()
            await asyncio.gather(pulse, work, return_exceptions=True)
        return True

    async def _checkpoint(self, lease):
        from claw.jobs.models import Step
        async with self.store.factory() as db:
            step = await db.get(Step, (lease.job_id, lease.step_id))
            return self.store.unpack(step.checkpoint) if step else {}

    async def _pause(self, lease, reason):
        try:
            await self.store.settle(lease, StepOutcome('paused', checkpoint=await self._checkpoint(lease), reason=reason))
        except LeaseLost:
            pass

    async def probe_once(self):
        for job_id, step_id, fence, dependency, owner_id, mode in await self.store.due_dependencies():
            if not await self.authorize(owner_id, mode, job_id):
                # Do not use revoked credentials even for a readiness probe.
                await self.store.pause_waiting(job_id, step_id, fence, 'permission_denied')
                continue
            probe = self.probes.get((mode, dependency))
            connector_probe = self.probes.get((mode, 'connector')) if dependency.startswith('connector:') else None
            ready = False
            if probe or connector_probe:
                try:
                    async with asyncio.timeout(10):
                        ready = await (connector_probe(owner_id, job_id, dependency[10:])
                                       if connector_probe else probe(owner_id, job_id))
                except Exception:
                    ready = False
            await self.store.dependency_result(job_id, step_id, fence, ready)

    async def serve(self):
        await self.store.initialize()

        async def database_retry(operation):
            from sqlalchemy.exc import OperationalError, InterfaceError
            from loguru import logger
            delay = 1
            while not self.stopping.is_set():
                try:
                    return await operation()
                except (OperationalError, InterfaceError):
                    # Leases fence any operation whose commit was uncertain. Never
                    # replay the tool here; run_once must reacquire through claim.
                    logger.warning('Durable worker database unavailable; retry in {}s', delay)
                    try:
                        await asyncio.wait_for(self.stopping.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    delay = min(30, delay * 2)

        async def consume():
            while not self.stopping.is_set():
                if not await database_retry(self.run_once):
                    await asyncio.sleep(1)

        async def watch():
            last_prune = float('-inf')
            while not self.stopping.is_set():
                if self.recover_foreground is not None:
                    await database_retry(self.recover_foreground)
                await database_retry(self.probe_once)
                await database_retry(self.store.publish_deliveries)
                if self.store.clock() - last_prune >= 3600:
                    await database_retry(self.store.prune)
                    last_prune = self.store.clock()
                await asyncio.sleep(5)

        tasks = [asyncio.create_task(consume()) for _ in range(self.total)]
        tasks.append(asyncio.create_task(watch()))
        stopped = asyncio.create_task(self.stopping.wait())
        group = asyncio.gather(*tasks)
        try:
            done, _ = await asyncio.wait({group, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if group in done:
                await group
        finally:
            stopped.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(group, stopped, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--factory', default='claw.jobs.application:create_worker')
    args = parser.parse_args()
    module, name = args.factory.split(':', 1)
    factory = getattr(importlib.import_module(module), name)

    async def run():
        worker = factory()
        if hasattr(worker, '__await__'):
            worker = await worker
        # ``python -m claw.jobs.worker`` can load this source once as
        # ``__main__`` and once by its package name while the application
        # factory imports it.  Class identity is then different even though the
        # returned object implements the exact worker contract.  Validate the
        # process boundary structurally instead of rejecting a valid worker.
        if not valid_worker_process_contract(worker):
            raise TypeError('factory must return a Worker')
        import signal
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, worker.stopping.set)
        await worker.serve()

    asyncio.run(run())


if __name__ == '__main__':
    main()
