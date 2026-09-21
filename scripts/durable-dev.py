"""Isolated local dev supervisor: HTTP and durable worker in separate processes.

Never uses the existing installation's database or workspace. Provider settings
are loaded normally; source content stays in the dedicated directory. Stop with
Ctrl-C; restarting with the same --directory recovers retained jobs.
"""
import argparse
import asyncio
import os
from pathlib import Path
import signal
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--port', type=int, default=8701)
    parser.add_argument('--daemon', action='store_true', help='Keep dev services running after the invoking terminal exits')
    args = parser.parse_args()
    directory = (args.directory or Path(tempfile.mkdtemp(prefix='privateclaw-durable-dev-'))).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if not 1024 <= args.port <= 65535 or args.port == 8700:
        raise SystemExit('Use a separate unprivileged dev port, not 8700.')
    if args.daemon:
        import subprocess
        with open(directory / 'supervisor.log', 'ab') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                '--directory', str(directory), '--port', str(args.port)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        (directory / 'supervisor.pid').write_text(str(process.pid))
        print(f'Dev supervisor PID: {process.pid}; URL: http://127.0.0.1:{args.port}', flush=True)
        return
    from claw.config import load_settings
    settings = load_settings()
    if (directory / 'workspaces').resolve() == settings.workspaces_root.resolve():
        raise SystemExit('Use a separate dev directory.')
    env = {**os.environ, 'CLAW_DATABASE_URL': f'sqlite+aiosqlite:///{directory}/dev.db',
           'CLAW_AUTO_MIGRATE': 'false', 'CLAW_WORKSPACES_ROOT': str(directory / 'workspaces'),
           'CLAW_SBOT_WORKSPACES_ROOT': str(directory / 'bot-workspaces'),
           'CLAW_BRANDING_ROOT': str(directory / 'branding'),
           'CLAW_KNOWLEDGE_ROOT': str(directory / 'knowledge'),
           'CLAW_BLUEPRINTS_ROOT': str(directory / 'blueprints'),
           'CLAW_DURABLE_JOBS_PRIVATECLAW': 'true', 'CLAW_DURABLE_JOBS_SBOT': 'true',
           'CLAW_AUTH_MODE': 'dev', 'CLAW_SBOT_ENABLED': 'true', 'CLAW_OPEN_REGISTRATION': 'true'}
    from claw.db.engine import create_engine_and_factory, init_db
    engine, _ = create_engine_and_factory(env['CLAW_DATABASE_URL'])
    await init_db(engine)
    await engine.dispose()
    processes = []
    handles = []
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    try:
        for label, command in [
            ('api', ['uvicorn', 'claw.main:create_app', '--factory', '--host', '127.0.0.1', '--port', str(args.port)]),
            ('worker', ['claw.jobs.worker']),
        ]:
            handle = open(directory / f'{label}.log', 'ab')
            handles.append(handle)
            process = await asyncio.create_subprocess_exec(sys.executable, '-m', *command,
                cwd=ROOT, env=env, stdout=handle, stderr=handle)
            processes.append(process)
        print(f'Dev URL: http://127.0.0.1:{args.port}\nDev directory: {directory}', flush=True)
        waiters = [asyncio.create_task(p.wait()) for p in processes]
        stop_waiter = asyncio.create_task(stopped.wait())
        await asyncio.wait([*waiters, stop_waiter], return_when=asyncio.FIRST_COMPLETED)
        stop_waiter.cancel()
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
        for process in processes:
            try:
                await asyncio.wait_for(process.wait(), timeout=15)
            except TimeoutError:
                process.kill()
                await process.wait()
        for handle in handles:
            handle.close()


if __name__ == '__main__':
    asyncio.run(main())
