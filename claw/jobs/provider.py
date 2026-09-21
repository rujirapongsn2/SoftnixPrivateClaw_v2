"""Provider accounting follows the root job through inherited asyncio contexts."""
import asyncio
import json
import time
import uuid
from contextvars import ContextVar

from claw.providers.base import ChatResult, LLMProvider

current_execution: ContextVar = ContextVar('durable_job_execution', default=None)
foreground_accounting: ContextVar = ContextVar('foreground_job_accounting', default=None)


class ForegroundAccounting:
    """Call receipts carried into a job if a short turn grows into a workflow.

    Terminal transcript and known usage commit together. Call receipts contain
    no credentials or prompts; missing usage remains a conservative reservation.
    """
    def __init__(self, store=None, owner_id=None, session_id=None, mode="privateclaw"):
        self.store, self.owner_id, self.session_id, self.mode = store, owner_id, session_id, mode
        self.journal_id, self.revision = uuid.uuid4().hex, 0
        self.calls = {}
        self.state = {}
        self.started = time.monotonic()
        self.count_plan_turn = False
        self.adopted_job_id = None
        self.pulse = None
        self.delivered = False

    async def checkpoint(self, state):
        self.state = dict(state)
        await self.persist()

    async def reserve_call(self, model, tokens):
        call_id = uuid.uuid4().hex
        self.calls[call_id] = dict(id=call_id, model=model, reserved=tokens, actual=None)
        await self.persist()
        return call_id

    async def record_usage(self, call_id, actual, *, usage):
        call = self.calls[call_id]
        if call['actual'] is not None and call['actual'] != actual:
            raise ValueError('conflicting foreground usage')
        call.update(actual=actual, usage=dict(usage))
        await self.persist()

    async def persist(self):
        if self.store is not None:
            from claw.jobs.foreground import save
            self.revision = await save(self.store, self.journal_id, self.owner_id,
                self.session_id, self.mode, self.revision,
                {**self.snapshot(), 'checkpoint': self.state})
            if self.pulse is None:
                self.pulse = asyncio.create_task(self._heartbeat())

    async def _heartbeat(self):
        from claw.jobs.foreground import heartbeat
        from sqlalchemy.exc import OperationalError, InterfaceError
        while True:
            await asyncio.sleep(15)
            try:
                if not await heartbeat(self.store, self.journal_id, self.owner_id):
                    return
            except (OperationalError, InterfaceError):
                # The next durable write still checks ownership before any tool.
                continue

    async def deliver(self, entries, *, metrics=None):
        from claw.jobs.foreground import deliver
        await self.checkpoint({**self.state, 'foreground_terminal': True,
                               'terminal_delivery': {'version': 1, 'entries': entries, 'metrics': metrics or {}}})
        await deliver(self.store, self.journal_id, self.owner_id, self.revision)
        self.delivered = True

    async def close(self):
        if self.pulse is not None:
            self.pulse.cancel()
            await asyncio.gather(self.pulse, return_exceptions=True)
        if self.store is not None and self.revision:
            from claw.jobs.foreground import close
            await close(self.store, self.journal_id, self.owner_id, self.revision)

    def snapshot(self):
        return dict(journal_id=self.journal_id if self.store is not None else None,
                    journal_revision=self.revision if self.store is not None else None,calls=list(self.calls.values()),
                    active_seconds=max(0, time.monotonic() - self.started),
                    count_plan_turn=self.count_plan_turn)



class AccountedProvider(LLMProvider):
    def __init__(self, provider):
        self.provider = provider

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def count_tokens(self, messages, model=None):
        return self.provider.count_tokens(messages, model)

    async def stream_chat(self, messages, tools=None, model=None, max_tokens=4096,
                          temperature=.1, api_key=None, api_base=None):
        context = current_execution.get() or foreground_accounting.get()
        call_id = None
        if context is not None:
            estimate = self.count_tokens(messages, model)
            if tools:
                estimate += self.count_tokens([{'role': 'system', 'content': json.dumps(tools)}], model)
            # Reserve each adapter invocation, including fallback models. Provider-
            # internal HTTP retries need their own hooks before runtime rollout.
            # Unknown usage remains reserved; it must never silently become free.
            call_id = await context.reserve_call(model or '', int(estimate * 1.05) + 8 + max_tokens)
        async for event in self.provider.stream_chat(messages, tools, model, max_tokens,
                                                      temperature, api_key=api_key, api_base=api_base):
            if call_id and isinstance(event, ChatResult):
                usage = event.usage
                if 'prompt_tokens' in usage and 'completion_tokens' in usage:
                    await context.record_usage(call_id, usage['prompt_tokens'] + usage['completion_tokens'], usage=usage)
            yield event


def source_context():
    """Source caches may begin in a short turn without granting worker privileges."""
    return current_execution.get() or foreground_accounting.get()
