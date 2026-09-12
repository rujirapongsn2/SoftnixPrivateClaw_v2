import asyncio
import base64
import hashlib
import os
import time

import pytest

from sbot.core.delivery import LocalDeliveryWorker, read_capped
from sbot.local_agent import Folder
from sbot.local_workspaces import DELIVERY_STALE, MAX_PENDING_DELIVERIES, LocalWorkspaces
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
    # A read-only folder cannot accumulate deferred writes either.
    with pytest.raises(ValueError, match='read-only'):
        store.enqueue('alice', device['id'], 'a.txt', 'digest', 'a.txt')


def test_delivery_queue_is_bounded_deduplicated_and_owner_scoped(tmp_path):
    store = LocalWorkspaces(tmp_path)
    device = store.redeem(store.pair('alice')['code'], 'Folder', True)
    first = store.enqueue('alice', device['id'], 'out.txt', 'digest', 'out.txt')
    assert store.enqueue('alice', device['id'], 'out.txt', 'digest', 'out.txt') == first
    assert store.list('alice')[0]['pending'] == 1
    with pytest.raises(ValueError):
        store.enqueue('bob', device['id'], 'out.txt', 'digest', 'out.txt')
    with pytest.raises(ValueError):
        store.cancel_delivery('bob', first)
    for index in range(MAX_PENDING_DELIVERIES - 1):
        store.enqueue('alice', device['id'], f'{index}.txt', f'digest{index}', f'{index}.txt')
    with pytest.raises(ValueError, match='already waiting'):
        store.enqueue('alice', device['id'], 'extra.txt', 'extra', 'extra.txt')
    store.cancel_delivery('alice', first)
    assert all(item['id'] != first for item in store.deliveries('alice'))


def test_queued_delivery_waits_for_reconnect_and_yields_to_live_work(tmp_path):
    store = LocalWorkspaces(tmp_path)
    device = store.redeem(store.pair('alice')['code'], 'Folder', True)
    store.enqueue('alice', device['id'], 'out.txt', 'digest', 'out.txt')
    assert store.claim_delivery() is None  # offline
    store.poll(device['id'])
    with store.db() as db:
        db.execute("INSERT INTO job VALUES ('live',?,'{}','queued',?,NULL)", (device['id'], time.time()+45))
    assert store.claim_delivery() is None  # an interactive turn is waiting
    with store.db() as db:
        db.execute("DELETE FROM job WHERE id='live'")
    claimed = store.claim_delivery()
    assert claimed['dest'] == 'out.txt' and claimed['attempts'] == 1
    assert store.claim_delivery() is None  # never handed out twice
    store.finish_delivery(claimed['id'], False, 'transient')
    assert store.deliveries('alice')[0]['status'] == 'pending'
    store.finish_delivery(claimed['id'], True)
    assert store.deliveries('alice') == []


def test_expired_and_revoked_deliveries_never_reach_the_folder(tmp_path):
    store = LocalWorkspaces(tmp_path)
    device = store.redeem(store.pair('alice')['code'], 'Folder', True)
    store.poll(device['id'])
    stale = store.enqueue('alice', device['id'], 'old.txt', 'digest', 'old.txt')
    with store.db() as db:
        db.execute('UPDATE delivery SET expires=? WHERE id=?', (time.time()-1, stale))
    assert store.claim_delivery() is None
    assert store.deliveries('alice')[0]['status'] == 'failed'
    store.enqueue('alice', device['id'], 'new.txt', 'digest', 'new.txt')
    store.revoke('alice', device['id'])
    assert store.deliveries('alice') == []
    assert store.claim_delivery() is None


async def test_export_to_offline_folder_is_queued_then_delivered_once(tmp_path):
    store = LocalWorkspaces(tmp_path/'broker')
    device = store.redeem(store.pair('alice')['code'], 'Folder', True)
    cloud = tmp_path/'cloud'
    (cloud/'alice').mkdir(parents=True)
    (cloud/'alice'/'report.txt').write_text('รายงาน', encoding='utf-8')
    folder = tmp_path/'local'
    folder.mkdir()
    tool = LocalWorkspaceTool(cloud/'alice', store, 'alice')

    queued = await tool.execute(device['id'], 'export', 'ผลลัพธ์.txt', cloud_path='report.txt')
    assert 'Queued, NOT delivered' in queued
    assert not (folder/'ผลลัพธ์.txt').exists()

    fs = Folder(folder, writable=True)
    worker = LocalDeliveryWorker(store, cloud)

    async def agent():
        while True:
            job = store.poll(device['id'])
            if job:
                try:
                    store.result(device['id'], job['id'], fs.execute(job))
                except Exception as exc:  # noqa: BLE001 - mirrors the real agent
                    store.result(device['id'], job['id'], {'error': str(exc)})
            await asyncio.sleep(.01)

    driver = asyncio.create_task(agent())
    try:
        await asyncio.sleep(.05)  # the folder counts as back online once it polls
        await asyncio.wait_for(worker.tick(), 10)
        assert (folder/'ผลลัพธ์.txt').read_text(encoding='utf-8') == 'รายงาน'
        assert store.deliveries('alice') == []
        # Replaying an identical delivery must not create a second file.
        store.enqueue('alice', device['id'], 'report.txt', hashlib.sha256('รายงาน'.encode()).hexdigest(), 'ผลลัพธ์.txt')
        await asyncio.wait_for(worker.tick(), 10)
        assert store.deliveries('alice') == []
        assert len(list(folder.iterdir())) == 1
    finally:
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)
        os.close(fs.fd)


def test_stalled_delivery_is_reclaimed_and_retries_back_off(tmp_path):
    store = LocalWorkspaces(tmp_path)
    device = store.redeem(store.pair('alice')['code'], 'Folder', True)
    store.poll(device['id'])
    did = store.enqueue('alice', device['id'], 'out.txt', 'digest', 'out.txt')
    store.finish_delivery(store.claim_delivery()['id'], False, 'the folder refused the write')
    # Without backoff the same row is re-claimed inside the same tick and all
    # five attempts are spent on one transient failure.
    assert store.claim_delivery() is None
    with store.db() as db:
        db.execute('UPDATE delivery SET next_attempt=0 WHERE id=?', (did,))
    assert store.claim_delivery()['attempts'] == 2
    # A worker that dies mid-delivery leaves the row 'running', where it can be
    # neither retried nor cancelled until the stale claim is swept.
    assert store.claim_delivery() is None
    with pytest.raises(ValueError):
        store.cancel_delivery('alice', did)
    with store.db() as db:
        db.execute('UPDATE delivery SET claimed=?, next_attempt=0 WHERE id=?', (time.time()-DELIVERY_STALE-1, did))
    assert store.claim_delivery()['id'] == did
    # Going offline again is not the delivery's fault, so it keeps its budget.
    store.finish_delivery(did, False, 'waiting for the folder', transient=True)
    with store.db() as db:
        assert db.execute('SELECT attempts,status FROM delivery WHERE id=?', (did,)).fetchone()[0] == 2


def test_read_capped_never_loads_more_than_the_limit(tmp_path, monkeypatch):
    import sbot.core.delivery as delivery

    monkeypatch.setattr(delivery, 'MAX_BYTES', 4)
    source = tmp_path/'big.bin'
    source.write_bytes(b'0123456789')
    assert read_capped(source) == b'01234'


async def test_queued_delivery_stops_when_the_cloud_file_changed(tmp_path):
    store = LocalWorkspaces(tmp_path/'broker')
    device = store.redeem(store.pair('alice')['code'], 'Folder', True)
    cloud = tmp_path/'cloud'
    (cloud/'alice').mkdir(parents=True)
    (cloud/'alice'/'report.txt').write_text('edited after queueing')
    store.poll(device['id'])
    store.enqueue('alice', device['id'], 'report.txt', 'a'*64, 'out.txt')
    await LocalDeliveryWorker(store, cloud).tick()
    waiting = store.deliveries('alice')
    assert waiting[0]['status'] == 'failed' and 'changed' in waiting[0]['detail']


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
