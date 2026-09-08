"""Standalone Local Agent (Python 3.9+, macOS/Linux; no third-party dependencies).

Download this file from Sbot, then run python3 local_agent.py --server https://your-host
--folder /path/to/folder. Pairing code is prompted without echo. Add --write explicitly.
Only the selected folder is exposed. File operations reject symlinks and special files.
"""
import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

LIMIT = 8 * 1024 * 1024


class Folder:
    def __init__(self, root, writable=False):
        self.root = Path(root).resolve(strict=True)
        self.writable = writable
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    @contextmanager
    def parent(self, raw):
        # Walk directory descriptors: a concurrent symlink replacement cannot escape the root.
        if not isinstance(raw, str) or raw.startswith('/') or '\\' in raw or '\0' in raw:
            raise ValueError('Use a relative path inside the selected folder')
        parts = raw.split('/')
        if any(p in ('', '.', '..') for p in parts):
            raise ValueError('Invalid relative path')
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def execute(self, job):
        action, path = job['action'], job.get('path', '.')
        if action == 'list':
            if path == '.':
                fd = os.dup(self.fd)
            else:
                with self.parent(path) as (parent, name):
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                entries = []
                with os.scandir(fd) as scan:
                    for e in scan:
                        if len(entries) >= 1000:
                            break
                        if e.is_symlink():
                            continue
                        entries.append({'name': e.name, 'directory': e.is_dir(follow_symlinks=False)})
                return {'entries': entries, 'limit': 1000}
            finally:
                os.close(fd)
        if action not in ('read', 'write'):
            raise ValueError('Unsupported action; shell execution is not available')
        with self.parent(path) as (parent, name):
            if action == 'read':
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, 'rb') as f:
                    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                        raise ValueError('Only regular files are supported')
                    data = f.read(LIMIT+1)
                if len(data) > LIMIT:
                    raise ValueError('File exceeds 8 MB limit')
                return {'data': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest()}
            if not self.writable:
                raise ValueError('Folder is read-only')
            data = base64.b64decode(job['data'], validate=True)
            if len(data) > LIMIT:
                raise ValueError('File exceeds 8 MB limit')
            # New outputs only: atomic O_EXCL prevents accidental template overwrite or races.
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            return {'written': path, 'sha256': hashlib.sha256(data).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    parser.add_argument('--folder')
    parser.add_argument('--write', action='store_true', help='Permit creating new output files; never overwrite existing files')
    parser.add_argument('--state', default=str(Path.home()/'.sbot-local-agent.json'))
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('This version supports macOS/Linux only')
    url = urllib.parse.urlsplit(args.server)
    if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1')):
        parser.error('HTTPS required except for localhost')
    if url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        parser.error('--server must be the server origin, without path or credentials')
    folder = args.folder
    if not folder:
        try:
            import tkinter
            from tkinter.filedialog import askdirectory
            root = tkinter.Tk(); root.withdraw()
            folder = askdirectory(title='Choose folder for Sbot')
            root.destroy()
        except ImportError:
            parser.error('Folder picker unavailable; supply --folder /path/to/folder')
    if not folder:
        parser.error('No folder selected')
    fs = Folder(folder, args.write)
    server = args.server.rstrip('/')
    state = Path(args.state).expanduser()
    if state.resolve().is_relative_to(fs.root):
        parser.error('--state must be outside the shared folder')
    base = server + '/modes/sbot/api/local-workspaces'

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *unused):
            raise ValueError('Server redirects are not allowed')

    opener = urllib.request.build_opener(NoRedirect())

    def request(path, body=None, token=None):
        # Cloudflare's bot rule blocks Python's default `Python-urllib/x.y`
        # user agent (error 1010). A product-specific agent is both explicit
        # for operators and accepted by the protected public endpoint.
        headers = {'Content-Type': 'application/json', 'User-Agent': 'Sbot-Local-Agent/1.0'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        req = urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with opener.open(req, timeout=15) as response:
                return json.loads(response.read(12*1024*1024))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode()).get('detail')
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = None
            if exc.code == 403 and not detail:
                detail = 'Cloudflare rejected this request. Download the latest Local Agent and retry.'
            raise RuntimeError(detail or f'Server rejected the request ({exc.code})') from exc

    identity = {'server': server, 'folder': str(fs.root), 'writable': args.write}
    if state.exists():
        if state.is_symlink() or state.stat().st_mode & 0o077:
            parser.error('Agent state must be a private file (chmod 600)')
        saved = json.loads(state.read_text())
        if any(saved.get(k) != v for k, v in identity.items()):
            parser.error('This state belongs to another folder/server/permission. Choose a different --state file and pair again.')
    else:
        print('This shares files with your Sbot account:', fs.root)
        print('Permission:', 'create new files' if args.write else 'read only')
        code = getpass.getpass('Pairing code from Chat > Local folder: ')
        try:
            redeemed = request('/redeem', {'code': code, 'name': fs.root.name, 'writable': args.write})
        except RuntimeError as exc:
            parser.error(str(exc))
        saved = {**redeemed, **identity}
        fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(saved, f)
    print('Connected. Keep this process open. Ctrl+C disconnects. No shell access.')
    try:
        while True:
            try:
                job = request('/poll', {}, saved['token'])['job']
                if job:
                    try:
                        result = fs.execute(job)
                    except Exception as exc:
                        result = {'error': str(exc)}
                    # Retry delivery only, never execution. Server never redelivers a claimed job.
                    for _ in range(3):
                        try:
                            request('/result', {'id': job['id'], 'result': result}, saved['token'])
                            break
                        except (OSError, urllib.error.URLError):
                            time.sleep(1)
                time.sleep(1)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    print('Device revoked or account disabled; stopped.')
                    break
                print('Server unavailable; reconnecting…'); time.sleep(5)
            except (OSError, urllib.error.URLError):
                print('Offline; reconnecting…'); time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        os.close(fs.fd)


if __name__ == '__main__':
    main()
