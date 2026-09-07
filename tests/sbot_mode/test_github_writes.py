import json

import httpx
import pytest

from sbot.integrations.github_mcp_server import GitHubClient


def client(handler):
    return GitHubClient('test-token', default_repo='owner/repo', transport=httpx.MockTransport(handler))


def test_publish_files_preserves_tree_and_never_force_pushes():
    calls = []

    def handle(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        assert request.headers['Authorization'] == 'Bearer test-token'
        if request.method == 'GET' and '/git/ref/' in request.url.path:
            return httpx.Response(200, json={'object': {'sha': 'head'}})
        if request.method == 'GET':
            return httpx.Response(200, json={'tree': {'sha': 'old-tree'}})
        return httpx.Response(200, json={'sha': 'new'})

    result = client(handle).publish_files('feature/demo', 'Implement API', {'api.py': 'print(1)'}, expected_sha='head')
    assert result['sha'] == 'new'
    assert calls[2][2]['base_tree'] == 'old-tree'
    assert calls[3][2]['parents'] == ['head']
    assert calls[-1][0] == 'PATCH'
    assert calls[-1][2] == {'sha': 'new', 'force': False}


def test_stale_commit_is_rejected_before_writing():
    requests = []

    def handle(request):
        requests.append(request.method)
        return httpx.Response(200, json={'object': {'sha': 'newer'}})

    with pytest.raises(ValueError, match='branch changed'):
        client(handle).publish_files('feature/demo', 'change', {'x': 'y'}, expected_sha='old')
    assert requests == ['GET']


def test_new_feature_branch_is_created_without_modifying_base():
    calls = []

    def handle(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if '/git/ref/heads/feature' in request.url.path:
            return httpx.Response(404, json={})
        if '/git/ref/' in request.url.path:
            return httpx.Response(200, json={'object': {'sha': 'base-head'}})
        if request.method == 'GET':
            return httpx.Response(200, json={'tree': {'sha': 'base-tree'}})
        return httpx.Response(201, json={'sha': 'new'})

    client(handle).publish_files('feature', 'new', {'a': 'b'})
    assert calls[-1][2] == {'ref': 'refs/heads/feature', 'sha': 'new'}
    assert all(method != 'PATCH' for method, _, _ in calls)


def test_workflow_dispatch_and_draft_pr():
    calls = []

    def handle(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    github = client(handle)
    assert github.dispatch_workflow('deploy.yml', 'feature/demo')['status'] == 'dispatched'
    github.create_pull_request('demo', 'feature/demo')
    assert calls[0][1] == {'ref': 'feature/demo', 'inputs': {}}
    assert calls[1][1]['draft'] is True


@pytest.mark.parametrize('path', ['../secret', '/etc/passwd', '.git/config', 'a/../b', 'a\\b'])
def test_publish_rejects_invalid_paths(path):
    with pytest.raises(ValueError):
        client(lambda r: pytest.fail('no network expected')).publish_files('feature', 'change', {path: 'x'})


@pytest.mark.asyncio
async def test_github_mcp_exposes_write_tools_with_installed_sdk():
    from sbot.integrations.github_mcp_server import mcp
    names = {tool.name for tool in await mcp.list_tools()}
    assert {'publish_files', 'create_pull_request', 'dispatch_workflow'} <= names


def test_all_builtin_connector_servers_import_with_locked_sdk():
    import importlib
    from pathlib import Path
    for path in (Path(__file__).parents[1] / 'sbot/integrations').glob('*_mcp_server.py'):
        importlib.import_module(f'sbot.integrations.{path.stem}')


def test_writes_never_infer_sbots_own_checkout(monkeypatch):
    monkeypatch.setattr('sbot.integrations.github_mcp_server._discover_repo_from_git', lambda: 'sbot/platform')
    github = GitHubClient('token', transport=httpx.MockTransport(lambda r: pytest.fail('no request expected')))
    with pytest.raises(ValueError, match='explicit repo'):
        github.publish_files('feature', 'change', {'x': 'y'})
    with pytest.raises(ValueError, match='explicit repo'):
        github.dispatch_workflow('deploy.yml', 'main')
