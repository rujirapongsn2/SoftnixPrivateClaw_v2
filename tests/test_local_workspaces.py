import asyncio
import base64
import os
import time

import pytest

from sbot.local_agent import Folder
from sbot.local_workspaces import LocalWorkspaces
from sbot.tools.local_workspace import LocalWorkspaceTool
from tests.test_sbot_mode import integrated


def test_folder_rejects_escape_symlinks_and_overwrites(tmp_path):
    root = tmp_path/'shared'; root.mkdir()
    (tmp_path/'private.txt').write_text('secret')
    (root/'link').symlink_to(tmp_path/'private.txt')
    (root/'outside').symlink_to(tmp_path, target_is_directory=True)
    fs = Folder(root, writable=True)
    try:
        for path in ['../private.txt', '/etc/passwd', 'link', 'outside/private.txt']:
            with pytest.raises((ValueError, OSError)):
                fs.execute({'action': 'read', 'path': path})
        payload = {'action': 'write', 'path': 'รายงาน.txt', 'data': base64.b64encode(b'new').decode()}
        fs.execute(payload)
        with pytest.raises(FileExistsError):
            fs.execute(payload)
        assert (root/'รายงาน.txt').read_bytes() == b'new'
        fs.writable = False
        with pytest.raises(ValueError, match='read-only'):
            fs.execute({**payload, 'path': 'other.txt'})
    finally:
        os.close(fs.fd)


def test_pairing_expiry_ownership_and_revocation(tmp_path):
    store = LocalWorkspaces(tmp_path)
    pair = store.pair('alice')
    result = store.redeem(pair['code'], 'Folder', True, path='/Users/test/project')
    assert store.list('alice')[0]['path'] == '/Users/test/project'
    store.poll(result['id'], '/Users/test/project-renamed')
    assert store.list('alice')[0]['path'] == '/Users/test/project-renamed'
    with pytest.raises(ValueError):
        store.redeem(pair['code'], 'Again', True)
    with pytest.raises(ValueError):
        store.owned('bob', result['id'])
    store.revoke('alice', result['id'])
    with pytest.raises(ValueError):
        store.authenticate(result['token'])
    code = store.pair('alice')['code']
    with store.db() as db:
        db.execute('UPDATE pairing SET expires=?', (time.time()-1,))
    with pytest.raises(ValueError):
        store.redeem(code, 'expired', False)


async def test_end_to_end_local_import_export_and_download(integrated, tmp_path):
    app, client, user = integrated
    prefix = '/modes/sbot/api/local-workspaces'
    pair = (await client.post(prefix+'/pair')).json()
    device = (await client.post(prefix+'/redeem', json={'code': pair['code'], 'name': 'Local', 'path': '/Users/test/local', 'writable': True})).json()
    assert (await client.get(prefix)).json()[0]['path'] == '/Users/test/local'
    completion = await client.post(prefix+'/pair/status', json={'code': pair['code']})
    assert completion.status_code == 200
    assert completion.json() == {'workspace_id': device['id']}
    headers = {'Authorization': 'Bearer '+device['token']}
    sid = (await client.post('/modes/sbot/api/sessions', json={})).json()['id']
    assert (await client.put(prefix+f'/sessions/{sid}', json={'workspace_id': device['id']})).status_code == 200
    assert (await client.put(prefix+'/sessions/foreign', json={'workspace_id': device['id']})).status_code == 404
    assert (await client.get('/api/sessions/'+sid+'/messages')).status_code == 404
    folder = tmp_path/'local'; folder.mkdir()
    (folder/'ต้นฉบับ.txt').write_text('Thai source', encoding='utf-8')
    fs = Folder(folder, writable=True)
    store = app.state.sbot.runtime.local_workspaces
    tool = app.state.sbot.runtime.get_agent(user.id).tools.get('local_workspace')
    events = []
    async def complete(action):
        await client.post(prefix+'/poll', headers=headers)
        operation = asyncio.create_task(action)
        await asyncio.sleep(.01)
        job = (await client.post(prefix+'/poll', headers=headers)).json()['job']
        assert job
        # A claimed command is not delivered twice, even before result acknowledgement.
        assert (await client.post(prefix+'/poll', headers=headers)).json()['job'] is None
        reply = await client.post(prefix+'/result', headers=headers, json={'id': job['id'], 'result': fs.execute(job)})
        assert reply.json()['accepted'] is True
        return await operation
    try:
        imported = await complete(tool.execute(device['id'], 'import', 'ต้นฉบับ.txt', progress=events.append))
        assert 'Imported and published' in imported
        delivery = events[0]['path']
        response = await client.get(f'/modes/sbot/api/sessions/{sid}/files/{delivery}')
        assert response.status_code == 200 and response.text == 'Thai source'
        fingerprint = await client.get(f'/modes/sbot/api/sessions/{sid}/file-preview/fingerprint', params={'path': delivery})
        import hashlib
        assert fingerprint.status_code == 200
        assert fingerprint.json()['sha256'] == hashlib.sha256(b'Thai source').hexdigest()
        assert fingerprint.headers['x-content-type-options'] == 'nosniff'
        assert (await client.get('/modes/sbot/api/sessions/foreign/file-preview/fingerprint', params={'path': delivery})).status_code == 404
        assert (await client.get(f'/modes/sbot/api/sessions/{sid}/file-preview/fingerprint', params={'path': '../private'})).status_code == 404
        exported = await complete(tool.execute(device['id'], 'export', 'ผลลัพธ์.txt', cloud_path=delivery))
        assert 'written' in exported
        assert (folder/'ผลลัพธ์.txt').read_text() == 'Thai source'
        assert (folder/'ต้นฉบับ.txt').read_text() == 'Thai source'
        assert (await client.delete(prefix+'/'+device['id'])).status_code == 200
        assert (await client.post(prefix+'/poll', headers=headers)).status_code == 401
        assert 'Error:' in await tool.execute(device['id'], 'read', 'ต้นฉบับ.txt')
    finally:
        os.close(fs.fd)


async def test_offline_and_readonly_do_not_queue_work(tmp_path):
    store = LocalWorkspaces(tmp_path)
    device = store.redeem(store.pair('alice')['code'], 'Read only', False)
    with pytest.raises(ValueError, match='offline'):
        await store.call('alice', device['id'], {'action': 'read', 'path': 'a'})
    store.poll(device['id'])
    with pytest.raises(ValueError, match='read-only'):
        await store.call('alice', device['id'], {'action': 'write', 'path': 'a'})
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM job').fetchone()[0] == 0


def test_pairing_completion_is_owner_scoped_and_preserves_reconnected_id(tmp_path):
    store = LocalWorkspaces(tmp_path)
    code = store.pair('alice')['code']
    assert store.pairing_status('alice', code) == {'workspace_id': ''}
    device = store.redeem(code, 'Folder', False)
    assert store.pairing_status('bob', code) == {'workspace_id': ''}
    assert store.pairing_status('alice', code)['workspace_id'] == device['id']
    store.select('alice', 'chat', device['id'])
    code = store.pair('alice')['code']
    upgraded = store.redeem(code, 'Folder', True, device['token'])
    assert store.pairing_status('alice', code)['workspace_id'] == upgraded['id'] == device['id']
    assert store.owned('alice', device['id'])['writable']
    with store.db() as db:
        db.execute('UPDATE pairing_result SET expires=0')
    assert store.pairing_status('alice', code) == {'workspace_id': ''}
    store.revoke('alice', device['id'])
    assert store.selected('alice', 'chat') == ''
