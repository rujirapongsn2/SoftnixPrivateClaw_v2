"""Run against an explicitly selected live broker and installed agent; uses only a new temp folder."""
import argparse
import asyncio
import base64
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from sbot.local_workspaces import LocalWorkspaces

parser = argparse.ArgumentParser()
parser.add_argument('--broker', required=True)
parser.add_argument('--agent', required=True)
parser.add_argument('--server', required=True)
parser.add_argument('--owner-folder', required=True)
args = parser.parse_args()
store = LocalWorkspaces(Path(args.broker))
with store.db() as db:
    owners = db.execute('SELECT DISTINCT owner FROM workspace WHERE name=? AND revoked=0', (args.owner_folder,)).fetchall()
assert len(owners) == 1, 'Select an unambiguous owner folder'
owner = owners[0]['owner']
folder = Path(tempfile.mkdtemp(prefix='softnix-permission-smoke-'))
session = 'smoke-' + uuid.uuid4().hex
wid = None

def connect(writable):
    code = store.pair(owner)['code']
    cmd = [args.agent, 'connect', '--server', args.server, '--folder', str(folder)]
    if writable:
        cmd.append('--write')
    subprocess.run(cmd, input=code + '\n', text=True, check=True, capture_output=True)
    rows = [row for row in store.list(owner) if row['name'] == folder.name]
    assert len(rows) == 1, 'Reconfiguration duplicated the workspace'
    return rows[0]['id']

def online():
    for _ in range(30):
        if store.owned(owner, wid)['seen'] > time.time() - 10:
            return
        time.sleep(0.5)
    raise AssertionError('Background service did not connect')

def call(action, path, **extra):
    return asyncio.run(store.call(owner, wid, {'action': action, 'path': path, **extra}))

try:
    wid = connect(False)
    online()
    store.select(owner, session, wid)
    content = 'Softnix ทดสอบเขียนไฟล์สำเร็จ\n'.encode()
    data = base64.b64encode(content).decode()
    try:
        call('write', 'ผลทดสอบ.txt', data=data)
        raise AssertionError('Read-only write unexpectedly accepted')
    except ValueError as exc:
        assert 'read-only' in str(exc)
    assert connect(True) == wid
    assert store.selected(owner, session) == wid
    time.sleep(2)  # allow an in-flight poll to refresh local permissions
    result = call('write', 'ผลทดสอบ.txt', data=data)
    assert 'written' in result, result
    assert (folder / 'ผลทดสอบ.txt').read_bytes() == content
    assert base64.b64decode(call('read', 'ผลทดสอบ.txt')['data']) == content
    assert 'error' in call('write', 'ผลทดสอบ.txt', data=data)
    assert connect(False) == wid
    try:
        call('write', 'another.txt', data=data)
        raise AssertionError('Permission downgrade failed')
    except ValueError as exc:
        assert 'read-only' in str(exc)
    print('PASS: readonly rejected, upgrade preserved ID/session, binary service wrote/read Thai file, overwrite rejected, downgrade enforced')
    print('Test output:', folder / 'ผลทดสอบ.txt')
finally:
    if wid:
        store.revoke(owner, wid)
    with store.db() as db:
        db.execute('DELETE FROM binding WHERE session=? AND owner=?', (session, owner))
