"""Opt-in live model + Docker smoke test. Uses a fresh SQLite DB and workspace.

Run from the repository with .venv/bin/python scripts/team-live-e2e.py.
Credentials stay in memory; only synthetic inputs are sent to the provider.
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claw.config import Settings as ClawSettings
from sbot.config import Settings, LLMSettings, SandboxSettings
from sbot.core.bus import EventBus
from sbot.core.memory import MemoryService
from sbot.core.missions import MissionService
from sbot.core.runtime import AgentRuntime
from sbot.db.engine import create_engine_and_factory, init_db
from sbot.db.stores import UserStore, BotStore, MissionStore, SessionStore, MessageStore, MemoryStore, AuditStore
from sbot.providers.litellm_provider import LiteLLMProvider
from sbot.providers.base import ChatResult
from sbot.sandbox.ephemeral import EphemeralSandbox


class MeteredProvider(LiteLLMProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0
        self.usage = []
        self.tool_calls = []

    async def stream_chat(self, *args, **kwargs):
        self.calls += 1
        if self.calls > 24:
            raise RuntimeError('Live test call limit reached')
        async for event in super().stream_chat(*args, **kwargs):
            if isinstance(event, ChatResult):
                self.usage.append(event.usage)
                self.tool_calls.extend(t.name for t in event.tool_calls or [])
            yield event


async def main():
    from loguru import logger
    logger.remove()  # Do not capture prompts, credentials or exception locals.
    root = Path(tempfile.mkdtemp(prefix='sbot-team-live-'))
    report = {'directory': str(root), 'checks': {}, 'started_at': time.time()}
    config = ClawSettings()
    report['model'] = config.llm.model
    sandbox = EphemeralSandbox(SandboxSettings(enabled=True, network='none', timeout_seconds=60))
    probe = root / 'sandbox-probe'
    probe.mkdir()
    result = await sandbox.run("python -c \"from pathlib import Path; Path('probe.txt').write_text(str(6*7)); print('SANDBOX_OK')\"", probe)
    report['checks']['docker_file_roundtrip'] = result.exit_code == 0 and (probe / 'probe.txt').is_file() and (probe / 'probe.txt').read_text() == '42'
    print(json.dumps({'phase': 'sandbox', 'passed': report['checks']['docker_file_roundtrip']}), flush=True)
    provider = MeteredProvider(api_key=config.llm.api_key, api_base=config.llm.api_base, default_model=config.llm.model)
    engine = runtime = service = None
    try:
        answer = ''
        async with asyncio.timeout(60):
            async for event in provider.stream_chat([{'role': 'user', 'content': 'Reply exactly LIVE_MODEL_OK.'}], max_tokens=32):
                if isinstance(event, ChatResult):
                    answer = event.content or ''
        report['checks']['model_connection'] = 'LIVE_MODEL_OK' in answer
        print(json.dumps({'phase': 'model', 'passed': report['checks']['model_connection']}), flush=True)
        if not all(report['checks'].values()):
            return
        settings = Settings(_env_file=None, database_url=f'sqlite+aiosqlite:///{root}/test.db',
                            workspaces_root=root / 'workspaces', sandbox=sandbox.settings,
                            llm=LLMSettings(model=config.llm.model, max_tokens=2048, max_iterations=8, max_turn_seconds=90))
        engine, factory = create_engine_and_factory(settings.database_url)
        await init_db(engine)
        users, bots, missions = UserStore(factory), BotStore(factory), MissionStore(factory)
        sessions, messages = SessionStore(factory, is_postgres=False), MessageStore(factory, is_postgres=False)
        memory = MemoryService(MemoryStore(factory), messages, sessions, provider)
        bus = EventBus()
        runtime = AgentRuntime(settings=settings, provider=provider, bus=bus, users=users, bots=bots,
                               sessions=sessions, messages=messages, memory=memory, audit=AuditStore(factory, is_postgres=False))
        service = MissionService(missions, bots, provider, sandbox, settings, messages=messages, sessions=sessions, bus=bus)
        runtime.missions = service
        user = await users.get_or_create_by_email('live-e2e@example.invalid')
        cos = await bots.get_or_create_cos(user.id)
        a = await bots.create(owner_id=user.id, name='Alpha', role_title='File tester', charter='Execute the requested synthetic test exactly. Use exec for Python. Record finish_step with evidence and files.', tool_allowlist=['exec', 'read_file', 'write_file', 'list_dir'])
        b = await bots.create(owner_id=user.id, name='Beta', role_title='File tester', charter='Execute the requested synthetic test exactly. Use exec for Python. Record finish_step with evidence and files.', tool_allowlist=['exec', 'read_file', 'write_file', 'list_dir'])
        session = await sessions.create(user.id, bot_id=cos.id, kind='direct')
        t0 = time.monotonic()
        receipt = await runtime.handle_message(user.id, session.id,
            'Delegate to Alpha in the background: use exec to run Python that sleeps for 25 seconds, then writes result.txt containing ALPHA_42. Read back the file and finish_step with that file. Required file: result.txt. Return only the job receipt.')
        report['receipt_seconds'] = round(time.monotonic() - t0, 2)
        jobs_a = await missions.list_missions(user.id)
        report['checks']['chat_submitted_job'] = bool(jobs_a) and 'Job ID:' in receipt
        if not jobs_a:
            return
        first = jobs_a[0]
        receipt_b = await runtime.handle_message(user.id, session.id,
            'New unrelated job: delegate to Beta in the background. Use exec to run Python writing result.txt containing BETA_99. Read back and finish_step with result.txt as required file. Return the job receipt.')
        first_now = await missions.get_mission(first.id, user.id)
        report['checks']['second_job_accepted_before_first_finished'] = 'Job ID:' in receipt_b and first_now.status in ('running', 'queued')
        status_reply = await runtime.handle_message(user.id, session.id, 'Use team_status now and report actual job statuses briefly.')
        report['status_reply'] = status_reply
        tasks = list(service._running.values())
        async with asyncio.timeout(180):
            await asyncio.gather(*tasks)
        job_rows = await missions.list_missions(user.id)
        report['jobs'] = [await service.status(j.id, user.id) for j in job_rows]
        report['checks']['jobs_completed'] = len(job_rows) == 2 and all(j.status == 'completed' for j in job_rows)
        files = sorted(settings.workspaces_root.rglob('result.txt'))
        contents = [p.read_text().strip() for p in files]
        report['checks']['same_name_separate_outputs'] = 'ALPHA_42' in contents and 'BETA_99' in contents
        history = await messages.recent(session.id, limit=100)
        report['checks']['reports_delivered'] = len([m for m in history if m.get('meta', {}).get('delivery_id')]) == 2
        chain = await service.submit_work(user.id, session.id, 'live-chain', 'Compute 21 times 2 then double it, and report the result.', [
            {'id': 'compute', 'bot_id': a.id, 'instruction': 'Use exec to run Python computing 21*2 and writing the number to source.txt. Read it back. finish_step completed with source.txt.', 'required_files': ['source.txt']},
            {'id': 'double', 'bot_id': b.id, 'depends_on': ['compute'], 'instruction': 'Read the upstream source.txt from the input snapshot manifest. Use exec Python to double its number and write doubled.txt. Read back and finish_step completed with doubled.txt.', 'required_files': ['doubled.txt']},
        ], cos.id)
        async with asyncio.timeout(180):
            await service._running[chain.id]
        chain_status = await service.status(chain.id, user.id)
        report['chain'] = chain_status
        report['checks']['dependency_chain_and_synthesis'] = chain_status['status'] == 'completed' and any(
            p.read_text().strip() == '84' for p in settings.workspaces_root.rglob('doubled.txt'))
        # Occupy admission slots to cancel a persisted queued job before any
        # worker or model can execute it. No production tasks are affected.
        for _ in range(service.max_parallel_nodes):
            await service._mission_slots.acquire()
        try:
            queued = await service.submit_work(user.id, session.id, 'live-cancel', 'Cancelled test', [
                {'id': 'never', 'bot_id': a.id, 'instruction': 'Write never.txt', 'required_files': ['never.txt']}], cos.id)
            before = provider.calls
            await service.cancel(queued.id, user.id)
            report['checks']['queued_cancel'] = (await missions.get_mission(queued.id, user.id)).status == 'cancelled' and provider.calls == before
        finally:
            for _ in range(service.max_parallel_nodes):
                service._mission_slots.release()
    except Exception as exc:
        # Exception strings from SDKs can embed requests. Keep only class/status.
        report['error'] = {'type': type(exc).__name__, 'status': getattr(exc, 'status_code', None)}
    finally:
        if service:
            await service.stop()
        if runtime:
            await runtime.drain()
        if engine:
            await engine.dispose()
        report['model_calls'] = provider.calls
        report['tool_calls'] = provider.tool_calls
        report['usage'] = provider.usage
        report['finished_at'] = time.time()
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print(json.dumps({'report': str(root / 'report.json'), 'checks': report['checks'], 'error': report.get('error')}), flush=True)
    return bool(report['checks']) and all(report['checks'].values()) and 'error' not in report


if __name__ == '__main__':
    sys.exit(0 if asyncio.run(main()) else 1)
