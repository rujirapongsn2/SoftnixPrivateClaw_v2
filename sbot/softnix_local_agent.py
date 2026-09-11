"""Softnix Local Agent service and CLI for macOS."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import ssl
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from sbot.local_agent import Folder
except ImportError:
    from local_agent import Folder

LABEL = 'ai.softnix.local-agent'
DATA = Path.home() / 'Library/Application Support/Softnix Local Agent'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


class RequestError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def origin(value):
    url = urllib.parse.urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/')):
        raise ValueError('A valid HTTPS server origin is required')
    return value.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ValueError('Server redirects are not allowed')


def request(server, path, body, token=None):
    headers = {'Content-Type': 'application/json', 'User-Agent': 'Softnix-Local-Agent/2.0'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(server + '/modes/sbot/api/local-workspaces' + path,
                                 data=json.dumps(body).encode(), headers=headers)
    context = ssl.create_default_context()
    if getattr(sys, 'frozen', False):
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context)).open(req, timeout=15) as response:
            return json.loads(response.read(12 * 1024 * 1024))
    except urllib.error.HTTPError as exc:
        detail = 'Request rejected by server'
        try:
            value = json.loads(exc.read(4096)).get('detail')
            if isinstance(value, str):
                detail = value
        except (ValueError, AttributeError):
            pass
        raise RequestError(exc.code, detail) from exc


def private_dir():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    if DATA.is_symlink():
        raise ValueError('Agent data directory cannot be a symlink')
    DATA.chmod(0o700)


def connect(server, code, folder, writable):
    server = origin(server)
    fs = Folder(folder, writable)
    try:
        if DATA.resolve().is_relative_to(fs.root):
            raise ValueError('Select a project folder, not a parent of the agent credential directory')
        private_dir()
        previous = None
        for path in DATA.glob('*.json'):
            if path.is_symlink() or path.stat().st_mode & 0o077:
                continue
            record = json.loads(path.read_text())
            if record.get('server') == server and record.get('folder') == str(fs.root):
                try:
                    request(server, '/validate', {}, record['token'])
                    previous = record['token']
                    break
                except RequestError as exc:
                    if exc.status != 401:
                        raise
        # Code comes from the native app via stdin, never a persistent URL or process argument.
        result = request(server, '/redeem', {'code': code.strip(), 'name': fs.root.name, 'path': str(fs.root),
                                            'writable': writable, 'previous_token': previous})
        saved = {**result, 'server': server, 'folder': str(fs.root), 'writable': writable}
        name = hashlib.sha256((server + result['id']).encode()).hexdigest() + '.json'
        fd, temporary = tempfile.mkstemp(dir=DATA, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as file:
                json.dump(saved, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, DATA / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    finally:
        os.close(fs.fd)


def run_folder(saved, stop, state_path=None):
    delay = 1
    while not stop.is_set():
        try:
            if state_path is not None:
                current = json.loads(state_path.read_text())
                if all(current.get(k) == saved[k] for k in ('server', 'token', 'folder')):
                    saved = current
            poll_body = {'path': saved['folder']} if saved.get('folder') else {}
            job = request(saved['server'], '/poll', poll_body, saved['token'])['job']
            if job:
                try:
                    if str(Path(saved['folder']).resolve(strict=True)) != saved['folder']:
                        raise ValueError('Folder location changed; reconnect it from Bot Mode')
                    fs = Folder(saved['folder'], saved['writable'])
                    try:
                        result = fs.execute(job)
                    finally:
                        os.close(fs.fd)
                except Exception as exc:
                    result = {'error': str(exc)}
                # A job is executed once. Retry only reporting its result.
                for attempt in range(3):
                    try:
                        request(saved['server'], '/result', {'id': job['id'], 'result': result}, saved['token'])
                        break
                    except (RequestError, OSError, ValueError):
                        stop.wait(attempt + 1)
            delay = 1
        except RequestError as exc:
            if exc.status == 401:
                return  # revoked: do not reconnect this credential
            delay = min(delay * 2, 30)
        except (OSError, ValueError, KeyError):
            delay = min(delay * 2, 30)
        stop.wait(delay)


def serve():
    private_dir()
    lock = os.open(DATA / 'service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        raise ValueError('softnix-local-agent is already running')
    stop = threading.Event()
    seen = set()
    try:
        while True:
            for path in DATA.glob('*.json'):
                if path in seen or path.is_symlink() or path.stat().st_mode & 0o077:
                    continue
                try:
                    saved = json.loads(path.read_text())
                    origin(saved['server'])
                    if not all(k in saved for k in ('token', 'folder', 'writable')):
                        continue
                except (ValueError, KeyError, OSError):
                    continue
                seen.add(path)
                threading.Thread(target=run_folder, args=(saved, stop, path), daemon=True).start()
            stop.wait(2)
    except KeyboardInterrupt:
        stop.set()
    finally:
        os.close(lock)


def service(action):
    domain = 'gui/' + str(os.getuid())
    target = domain + '/' + LABEL
    def loaded():
        return subprocess.run(['launchctl', 'print', target], capture_output=True).returncode == 0

    def bootstrap():
        # launchd may still be unloading a previous definition. A concurrent
        # installer can also load it first; verify instead of treating that as failure.
        for attempt in range(5):
            if loaded():
                return
            result = subprocess.run(['launchctl', 'bootstrap', domain, str(PLIST)],
                                    capture_output=True, text=True)
            if result.returncode == 0 or loaded():
                return
            if attempt < 4:
                time.sleep(0.25 * (attempt + 1))
        raise ValueError('Could not start Softnix Local Agent. Check System Settings → General → Login Items & Extensions, allow Softnix, then open the app again. Details: ' + (result.stderr or '').strip())

    if action == 'install':
        private_dir()
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        executable = str(Path(sys.executable).resolve())
        if not getattr(sys, 'frozen', False):
            raise ValueError('Install the bundled binary from the Softnix Local Agent app')
        definition = {'Label': LABEL, 'ProgramArguments': [executable, 'serve'],
                      'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                      'ProcessType': 'Background', 'Umask': 0o077}
        if PLIST.is_symlink():
            raise ValueError('Service plist cannot be a symlink')
        unchanged = PLIST.exists() and PLIST.read_bytes() == plistlib.dumps(definition)
        PLIST.write_bytes(plistlib.dumps(definition))
        PLIST.chmod(0o600)
        bindir = Path.home() / '.local/bin'
        bindir.mkdir(parents=True, exist_ok=True)
        command = bindir / 'softnix-local-agent'
        if not command.exists() and not command.is_symlink():
            command.symlink_to(executable)
        if not unchanged and loaded():
            subprocess.run(['launchctl', 'bootout', target], check=True, capture_output=True)
            for _ in range(20):
                if not loaded():
                    break
                time.sleep(0.1)
            else:
                raise ValueError('Local Agent is still stopping. Open the app again to retry.')
        bootstrap()
    elif action == 'stop':
        subprocess.run(['launchctl', 'bootout', target], check=True)
    elif action in ('start', 'restart'):
        status = subprocess.run(['launchctl', 'print', target], capture_output=True)
        if status.returncode:
            bootstrap()
        if action == 'restart':
            subprocess.run(['launchctl', 'kickstart', '-k', target], check=True)
    elif action == 'status':
        result = subprocess.run(['launchctl', 'print', target], capture_output=True, text=True)
        print('softnix-local-agent: ' + ('running/loaded' if result.returncode == 0 else 'stopped'))
    print('softnix-local-agent: ' + action + ' completed')


def main():
    parser = argparse.ArgumentParser(prog='softnix-local-agent')
    parser.add_argument('command', choices=['install', 'start', 'stop', 'restart', 'status', 'serve', 'connect'])
    parser.add_argument('--server')
    parser.add_argument('--folder')
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'connect':
            if not args.server or not args.folder:
                raise ValueError('--server and --folder are required')
            connect(args.server, sys.stdin.readline().strip(), args.folder, args.write)
        elif args.command == 'serve':
            serve()
        else:
            service(args.command)
    except (ValueError, OSError, RequestError, subprocess.CalledProcessError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
