import json
import os
import threading

import pytest

from sbot import softnix_local_agent as agent
from sbot.local_workspaces import LocalWorkspaces


def test_upgrade_keeps_identity_and_session_and_updates_local_permissions(tmp_path, monkeypatch):
    store = LocalWorkspaces(tmp_path / 'broker')
    folder = tmp_path / 'project'
    folder.mkdir()
    monkeypatch.setattr(agent, 'DATA', tmp_path / 'credentials')
    def request(server, path, body, token=None):
        if path == '/validate':
            return dict(store.authenticate(token))
        return store.redeem(body['code'], body['name'], body['writable'], body.get('previous_token'))
    monkeypatch.setattr(agent, 'request', request)
    agent.connect('https://example.com', store.pair('alice')['code'], str(folder), False)
    original = store.list('alice')[0]['id']
    store.select('alice', 'chat', original)
    agent.connect('https://example.com', store.pair('alice')['code'], str(folder), True)
    assert store.selected('alice', 'chat') == original
    assert len(store.list('alice')) == 1
    assert store.list('alice')[0]['writable']
    files = list(agent.DATA.glob('*.json'))
    assert len(files) == 1
    assert json.loads(files[0].read_text())['writable'] is True
    assert files[0].stat().st_mode & 0o777 == 0o600
    agent.connect('https://example.com', store.pair('alice')['code'], str(folder), False)
    assert not store.list('alice')[0]['writable']


def test_upgrade_rejects_another_account_without_consuming_code(tmp_path):
    store = LocalWorkspaces(tmp_path)
    old = store.redeem(store.pair('alice')['code'], 'sbot', False)
    code = store.pair('bob')['code']
    with pytest.raises(ValueError, match='another account'):
        store.redeem(code, 'sbot', True, old['token'])
    assert not store.list('alice')[0]['writable']
    assert store.redeem(code, 'other', True)


def test_connect_records_private_state_and_rejects_credential_parent(tmp_path, monkeypatch):
    data = tmp_path / 'credentials'
    folder = tmp_path / 'เอกสาร'
    folder.mkdir()
    monkeypatch.setattr(agent, 'DATA', data)
    requests = []
    def request(*args):
        requests.append(args)
        return {'id': 'device', 'token': 'private-token'}
    monkeypatch.setattr(agent, 'request', request)
    agent.connect('https://claw2.softnix.ai', 'pairing', str(folder), True)
    redeem = next(body for _, path, body, *_ in requests if path == '/redeem')
    assert redeem['path'] == str(folder.resolve())
    state = next(data.glob('*.json'))
    assert state.stat().st_mode & 0o777 == 0o600
    assert json.loads(state.read_text())['folder'] == str(folder)
    assert json.loads(state.read_text())['writable'] is True
    with pytest.raises(ValueError, match='parent'):
        agent.connect('https://claw2.softnix.ai', 'pairing', str(tmp_path), True)


def test_result_retry_never_reexecutes_write(tmp_path, monkeypatch):
    folder = tmp_path / 'project'
    folder.mkdir()
    stop = threading.Event()
    reports = []
    def request(server, path, body, token):
        if path == '/poll':
            assert body['path'] == str(folder)
            return {'job': {'id': 'one', 'action': 'write', 'path': 'output.txt', 'data': 'b2s='}}
        reports.append(body)
        if len(reports) == 1:
            raise OSError('network loss')
        stop.set()
        return {'accepted': True}
    monkeypatch.setattr(agent, 'request', request)
    agent.run_folder({'server': 'https://example.com', 'token': 't', 'folder': str(folder), 'writable': True}, stop)
    assert (folder / 'output.txt').read_text() == 'ok'
    assert len(reports) == 2
    assert all('written' in item['result'] for item in reports)


def test_revoked_device_stops_polling(monkeypatch):
    calls = []
    def request(*args):
        calls.append(args)
        raise agent.RequestError(401, 'revoked')
    monkeypatch.setattr(agent, 'request', request)
    agent.run_folder({'server': 'https://example.com', 'token': 't'}, threading.Event())
    assert len(calls) == 1


def test_reconnect_after_network_loss(monkeypatch):
    calls = []
    class Stop:
        def is_set(self): return len(calls) >= 2
        def wait(self, seconds): pass
    def request(*args):
        calls.append(args)
        if len(calls) == 1:
            raise OSError('offline')
        return {'job': None}
    monkeypatch.setattr(agent, 'request', request)
    agent.run_folder({'server': 'https://example.com', 'token': 't'}, Stop())
    assert len(calls) == 2


@pytest.mark.parametrize('server', ['http://example.com', 'https://u:p@example.com', 'https://example.com/path', 'https://example.com?secret=x'])
def test_reject_invalid_server(server):
    with pytest.raises(ValueError):
        agent.origin(server)


def test_start_retries_bootstrap_while_launchd_unloads(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    monkeypatch.setattr(agent, 'PLIST', tmp_path / 'agent.plist')
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    attempts = []
    def run(args, **kwargs):
        if args[1] == 'bootstrap':
            attempts.append(args)
            return CompletedProcess(args, 5 if len(attempts) == 1 else 0, '', 'busy')
        return CompletedProcess(args, 1, '', '')
    monkeypatch.setattr(agent.subprocess, 'run', run)
    agent.service('start')
    assert len(attempts) == 2


def test_install_existing_service_does_not_unload(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    monkeypatch.setattr(agent, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(agent, 'PLIST', tmp_path / 'LaunchAgents/agent.plist')
    monkeypatch.setattr(agent.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(agent.sys, 'frozen', True, raising=False)
    calls = []
    def run(args, **kwargs):
        calls.append(args[1])
        return CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(agent.subprocess, 'run', run)
    # First install with no loaded service.
    monkeypatch.setattr(agent.subprocess, 'run', lambda args, **kwargs: CompletedProcess(args, 1 if args[1] == 'print' else 0, '', ''))
    agent.service('install')
    monkeypatch.setattr(agent.subprocess, 'run', run)
    agent.service('install')
    assert calls == ['print']


def test_bootstrap_failure_has_actionable_error(monkeypatch):
    from subprocess import CompletedProcess
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    monkeypatch.setattr(agent.subprocess, 'run', lambda args, **kwargs: CompletedProcess(args, 5, '', 'disabled'))
    with pytest.raises(ValueError, match='Login Items'):
        agent.service('start')
