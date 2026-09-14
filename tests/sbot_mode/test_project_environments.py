import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from sbot.config import SandboxSettings
from sbot.core.specialist import SpecialistRunner
from sbot.sandbox.ephemeral import EphemeralSandbox, SandboxResult
from sbot.sandbox.projects import ProjectEnvironments, run_process
from sbot.tools.filesystem import ReadFileTool, WriteFileTool
from sbot.tools.project import ProjectTool
from tests.sbot_mode.conftest import FakeProvider, text_turn


@pytest.mark.asyncio
async def test_projects_fail_closed_when_disabled(tmp_path):
    tool = ProjectTool(EphemeralSandbox(SandboxSettings(enabled=False)), tmp_path)
    assert (await tool.execute('demo', 'exec', 'touch marker')).startswith('Error:')
    assert not (tmp_path / 'projects').exists()


def test_project_identity_is_stable_scoped_and_rejects_traversal(tmp_path):
    manager = ProjectEnvironments(SandboxSettings())
    first = manager.identity(tmp_path / 'alice', 'demo')[0]
    assert first == ProjectEnvironments(SandboxSettings()).identity(tmp_path / 'alice', 'demo')[0]
    assert first != manager.identity(tmp_path / 'bob', 'demo')[0]
    assert first != manager.identity(tmp_path / 'alice', 'other')[0]
    for slug in ['../demo', '/tmp', 'Demo', 'a;echo', 'a/b', '']:
        with pytest.raises(ValueError):
            manager.identity(tmp_path, slug)
    (tmp_path / 'projects').symlink_to(tmp_path.parent)
    with pytest.raises(ValueError):
        manager.identity(tmp_path, 'demo')


@pytest.mark.asyncio
async def test_project_network_is_small_labeled_and_internal_when_egress_is_disabled(monkeypatch):
    manager = ProjectEnvironments(SandboxSettings(network="none"))
    calls = []

    async def docker(*args, timeout=120):
        calls.append(args)
        if args[:2] == ("network", "inspect"):
            return SandboxResult(1, "", "No such network", False)
        if args[:2] == ("network", "ls"):
            return SandboxResult(0, "", "", False)
        if args[:2] == ("network", "create"):
            return SandboxResult(0, "network-id", "", False)
        raise AssertionError(args)

    monkeypatch.setattr(manager, "_docker", docker)
    assert await manager._ensure_network("sbot-project-ingress-test") is True

    create = next(args for args in calls if args[:2] == ("network", "create"))
    assert "--internal" in create
    assert "sbot.project.network=true" in create
    subnet = create[create.index("--subnet") + 1]
    assert subnet.endswith("/28")


@pytest.mark.asyncio
async def test_existing_project_network_cannot_bypass_no_egress_policy(monkeypatch):
    manager = ProjectEnvironments(SandboxSettings(network="none"))

    async def docker(*args, timeout=120):
        return SandboxResult(0, json.dumps([{"Internal": False}]), "", False)

    monkeypatch.setattr(manager, "_docker", docker)
    with pytest.raises(RuntimeError, match="incompatible egress policy"):
        await manager._ensure_network("sbot-project-ingress-test")


@pytest.mark.asyncio
async def test_file_tools_understand_container_paths_without_escape(tmp_path):
    await WriteFileTool(tmp_path).execute('/workspace/hello.txt', 'hello')
    assert await ReadFileTool(tmp_path).execute('hello.txt') == 'hello'
    with pytest.raises(ValueError):
        await ReadFileTool(tmp_path).execute('/workspace/../outside')


@pytest.mark.asyncio
async def test_process_output_is_bounded_and_failure_visible():
    result = await run_process([sys.executable, '-c', "import sys; print('x'*1000000); sys.exit(3)"])
    assert result.exit_code == 3
    assert len(result.stdout) <= 20000


@pytest.mark.asyncio
async def test_specialist_receives_connectors_without_widening_allowlist(tmp_path):
    from sbot.tools.base import Tool

    class ConnectorTool(Tool):
        name = 'mcp_github_publish_files'
        description = 'publish'
        parameters: ClassVar[dict] = {'type': 'object'}

        async def execute(self, **kwargs):
            return 'ok'

    class Connectors:
        async def sync_tools(self, owner, registry):
            assert owner == 'alice'
            registry.register(ConnectorTool())

    provider = FakeProvider([text_turn('done'), text_turn('done')])
    runner = SpecialistRunner(provider, None, tmp_path, owner_id='alice', connectors=Connectors())
    bot = SimpleNamespace(tool_allowlist=['project'], model=None)
    seen = []
    # Inspect the actual loop tool registry without relying on provider internals.
    from unittest.mock import patch

    from sbot.core.loop import TurnOutcome

    async def run_turn(loop, *args, **kwargs):
        seen.append(loop.tools.tool_names)
        return TurnOutcome(final_content='done', new_messages=[])

    with patch('sbot.core.specialist.AgentLoop.run_turn', run_turn):
        await runner.run(bot, 'system', 'task', lambda e: None, 'one')
        bot.tool_allowlist = ['project', 'mcp_github_publish_files']
        await runner.run(bot, 'system', 'task', lambda e: None, 'two')
    assert 'project' in seen[0]
    assert 'mcp_github_publish_files' not in seen[0]
    assert 'mcp_github_publish_files' in seen[1]


@pytest.mark.skipif(os.environ.get('SBOT_DOCKER_TESTS') != '1', reason='explicit Docker integration test')
@pytest.mark.asyncio
async def test_real_persistent_compose_restart_timeout_and_cancellation(tmp_path):
    settings = SandboxSettings(projects_enabled=True, project_docker_enabled=True)
    manager = ProjectEnvironments(settings)
    name, path = manager.identity(tmp_path, 'smoke')
    shutil.copytree(Path(__file__).parents[1] / 'examples/software-team', path)
    try:
        status = json.loads(await manager.execute(tmp_path, 'smoke', 'start'))
        assert status['state'] == 'running'
        assert status['ports']['8000/tcp'][0]['HostIp'] == '127.0.0.1'
        result = await manager.execute(tmp_path, 'smoke', 'exec', 'echo persisted >/opt/marker')
        assert '[exit code: 0]' in result
        result = await manager.execute(tmp_path, 'smoke', 'compose_up', timeout_seconds=300)
        assert '[exit code: 0]' in result, result
        result = await manager.execute(tmp_path, 'smoke', 'exec', 'curl -fsS localhost:8000/health')
        assert '+PONG' in result, result
        visits = await manager.execute(tmp_path, 'smoke', 'exec', 'curl -fsS localhost:8000/')
        assert ':1' in visits, visits
        assert 'Sbot project is running' in await manager.execute(
            tmp_path, 'smoke', 'exec', 'curl -fsS localhost:8080')
        timed = await manager.execute(tmp_path, 'smoke', 'exec', 'sleep 10; touch /workspace/leaked', 1)
        assert 'timed out' in timed
        task = asyncio.create_task(manager.execute(tmp_path, 'smoke', 'exec', 'sleep 3; touch /workspace/cancelled'))
        await asyncio.sleep(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(3)
        assert not (path / 'leaked').exists()
        assert not (path / 'cancelled').exists()
        await manager.execute(tmp_path, 'smoke', 'stop')
        manager = ProjectEnvironments(settings)  # simulate Sbot restart: no in-memory state
        await manager.execute(tmp_path, 'smoke', 'start')
        assert 'persisted' in await manager.execute(tmp_path, 'smoke', 'exec', 'cat /opt/marker')
        result = await manager.execute(tmp_path, 'smoke', 'compose_up', timeout_seconds=180)
        assert '[exit code: 0]' in result, result
        assert ':2' in await manager.execute(tmp_path, 'smoke', 'exec', 'curl -fsS localhost:8000/')
    finally:
        await manager._docker('rm', '-f', name)
        await manager._docker('volume', 'rm', f'{name}-docker')
