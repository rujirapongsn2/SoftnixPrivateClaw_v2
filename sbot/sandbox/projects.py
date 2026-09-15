"""Persistent project environments. Docker is the durable inventory, not RAM.

The optional nested daemon is for trusted PoC workloads on a dedicated Docker
host. It never receives the host Docker socket. Privileged DinD is NOT a tenant
security boundary; see docs/software-development.md.
"""
import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
import uuid
from pathlib import Path

from sbot.core.keyed_locks import KeyedLocks
from sbot.sandbox.ephemeral import SandboxResult

_CAP = 20_000
_SLUG = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")


async def run_process(argv: list[str], timeout: float = 120) -> SandboxResult:
    """Drain both pipes while retaining only a bounded tail."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    async def drain(stream):
        tail = bytearray()
        while chunk := await stream.read(8192):
            tail.extend(chunk)
            del tail[:-_CAP]
        return tail.decode('utf-8', 'replace')

    out = asyncio.create_task(drain(proc.stdout))
    err = asyncio.create_task(drain(proc.stderr))
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except TimeoutError:
        timed_out = True
        proc.kill()
        await proc.wait()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    finally:
        stdout, stderr = await asyncio.gather(out, err)
    return SandboxResult(proc.returncode, stdout, stderr, timed_out)


class ProjectEnvironments:
    def __init__(self, settings):
        self.settings = settings
        self._locks = KeyedLocks()
        self._proxy_networks: set[str] = set()
        self._proxy_targets: dict[tuple[str, str, int], tuple[float, str]] = {}

    def identity(self, workspace: Path, project: str) -> tuple[str, Path]:
        if not _SLUG.fullmatch(project):
            raise ValueError('project must be a lowercase slug (1–48 characters, starting with a letter)')
        root = workspace.resolve()
        path = root / 'projects' / project
        if not path.resolve().is_relative_to(root) or path.is_symlink() or (root / 'projects').is_symlink():
            raise ValueError('project path escapes workspace or is a symlink')
        key = hashlib.sha256(f'{root}\0{project}'.encode()).hexdigest()[:24]
        return f'sbot-project-{key}', path

    async def _docker(self, *args, timeout=120):
        return await run_process(['docker', *args], timeout)

    async def _inspect(self, name):
        result = await self._docker('container', 'inspect', name)
        if result.exit_code:
            if 'No such' in result.stderr:
                return None
            raise RuntimeError(result.render())
        return json.loads(result.stdout)[0]

    async def _owned(self, name):
        state = await self._inspect(name)
        if state and state['Config'].get('Labels', {}).get('sbot.project') != name:
            raise ValueError('container name is occupied by an unmanaged container')
        return state

    def _network_name(self, name: str) -> str:
        suffix = name.removeprefix('sbot-project-')
        return f'{self.settings.project_network}-{suffix}'

    async def _ensure_network(self, network: str) -> bool:
        internal = self.settings.network == 'none'
        inspected = await self._docker('network', 'inspect', network)
        if not inspected.exit_code:
            state = json.loads(inspected.stdout)[0]
            if bool(state.get('Internal')) != internal:
                raise RuntimeError(
                    f'project network {network} has incompatible egress policy; '
                    'remove the stopped project container and network before retrying'
                )
            return False

        listed = await self._docker(
            'network', 'ls', '--filter', 'label=sbot.project.network=true', '--format', '{{.Name}}'
        )
        if listed.exit_code:
            raise RuntimeError(listed.render())
        count = len([line for line in listed.stdout.splitlines() if line.strip()])
        if count >= self.settings.project_network_limit:
            raise RuntimeError('project network capacity reached; remove unused project networks')

        pool = ipaddress.ip_network(self.settings.project_network_pool)
        prefix = self.settings.project_network_prefix
        subnet_size = 1 << (32 - prefix)
        slots = 1 << (prefix - pool.prefixlen)
        digest = hashlib.sha256(network.encode()).digest()
        start = int.from_bytes(digest[:8], 'big') % slots
        # The pool contains a power-of-two number of slots. An odd stride walks
        # the entire pool and avoids wasting all probes inside one large CIDR
        # that happens to overlap part of the configured range.
        stride = (int.from_bytes(digest[8:16], 'big') % slots) | 1
        last_error = None
        # Probe enough alternate slots to survive ordinary collisions without
        # turning one request into an unbounded Docker command loop.
        for offset in range(min(slots, 256)):
            slot = (start + offset * stride) % slots
            subnet = ipaddress.ip_network((int(pool.network_address) + slot * subnet_size, prefix))
            args = [
                'network', 'create', '--driver', 'bridge', '--subnet', str(subnet),
                '--label', 'sbot.project.network=true',
            ]
            if internal:
                args.append('--internal')
            created = await self._docker(*args, network)
            if not created.exit_code:
                return True
            last_error = created
            # A second process may have created this exact named network.
            inspected = await self._docker('network', 'inspect', network)
            if not inspected.exit_code:
                state = json.loads(inspected.stdout)[0]
                if bool(state.get('Internal')) != internal:
                    raise RuntimeError(f'project network {network} has incompatible egress policy')
                return False
            if 'overlap' not in created.stderr.lower():
                break
        raise RuntimeError(last_error.render() if last_error is not None else 'project network unavailable')

    async def _connect_network(self, name: str, state: dict) -> dict:
        network = self._network_name(name)
        if network not in (state['NetworkSettings'].get('Networks') or {}):
            await self._ensure_network(network)
            connected = await self._docker('network', 'connect', network, name)
            if connected.exit_code and 'already exists' not in connected.stderr.lower():
                raise RuntimeError(connected.render())
            state = await self._owned(name)
        if Path('/.dockerenv').exists() and network not in self._proxy_networks:
            proxy = self.settings.project_proxy_container.strip() or socket.gethostname()
            connected = await self._docker('network', 'connect', network, proxy)
            if connected.exit_code and 'already exists' not in connected.stderr.lower():
                raise RuntimeError(connected.render())
            self._proxy_networks.add(network)
        return state

    async def proxy_target(self, workspace: Path, project: str, port: int) -> str | None:
        """Return a managed running target; never accepts an arbitrary host."""
        if port not in self.settings.project_ports:
            return None
        cache_key = (str(workspace.resolve()), project, port)
        if len(self._proxy_targets) >= 2048:
            now = time.monotonic()
            self._proxy_targets = {
                key: value for key, value in self._proxy_targets.items() if value[0] > now
            }
            if len(self._proxy_targets) >= 2048:
                self._proxy_targets.pop(next(iter(self._proxy_targets)))
        cached = self._proxy_targets.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        name, _ = self.identity(workspace, project)
        state = await self._owned(name)
        if state is None or not state['State']['Running']:
            return None
        if Path('/.dockerenv').exists():
            state = await self._connect_network(name, state)
            if self._network_name(name) not in (state['NetworkSettings'].get('Networks') or {}):
                return None
            target = f'http://{name}:{port}'
            self._proxy_targets[cache_key] = (time.monotonic() + 5, target)
            return target
        bindings = (state['NetworkSettings'].get('Ports') or {}).get(f'{port}/tcp') or []
        for binding in bindings:
            if binding.get('HostPort'):
                host_ip = binding.get('HostIp') or self.settings.project_host_bind_ip
                target = f"http://{host_ip}:{binding['HostPort']}"
                self._proxy_targets[cache_key] = (time.monotonic() + 5, target)
                return target
        return None

    async def list(self, workspace: Path) -> list[dict]:
        """Return only Sbot-managed project containers below this workspace.

        Source folders alone do not count: a user may create or delete files at
        will, while a project quota is specifically a limit on Docker
        environments that have actually been provisioned.
        """
        root = workspace.resolve()
        projects = root / "projects"
        if not projects.exists():
            return []
        if projects.is_symlink() or not projects.is_dir() or not projects.resolve().is_relative_to(root):
            raise ValueError("project path escapes workspace or is a symlink")
        rows: list[dict] = []
        for path in sorted(projects.iterdir(), key=lambda item: item.name):
            if not path.is_dir() or path.is_symlink() or not _SLUG.fullmatch(path.name):
                continue
            name, _ = self.identity(root, path.name)
            state = await self._owned(name)
            if state is None:
                continue
            rows.append({
                "project": path.name,
                "container": name,
                "state": state["State"]["Status"],
                "ports": state["NetworkSettings"].get("Ports", {}),
            })
        return rows

    async def _ensure(self, name, path):
        state = await self._owned(name)
        if state is None:
            path.mkdir(parents=True, exist_ok=True)
            s = self.settings
            network = self._network_name(name)
            network_created = await self._ensure_network(network)
            args = [
                'run', '-d', '--name', name, '--label', f'sbot.project={name}',
                '--restart', 'unless-stopped', '--cpus', str(s.project_cpu_limit),
                '--memory', s.project_memory_limit, '--pids-limit', str(s.project_pids_limit),
                '--network', network, '--workdir', '/workspace',
                '--mount', f'type=bind,source={path.resolve()},target=/workspace',
                '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
            ]
            for port in s.project_ports:
                args += ['-p', f'{s.project_host_bind_ip}::{port}']
            if s.project_docker_enabled:
                args += ['--privileged', '--mount', f'type=volume,source={name}-docker,target=/var/lib/docker',
                         '-e', 'DOCKER_TLS_CERTDIR=', '-e', 'DOCKER_HOST=unix:///var/run/docker.sock']
            else:
                args += ['--entrypoint', '/bin/sh']
            args += [s.project_image]
            if s.project_docker_enabled:
                # A Unix-only daemon: no unauthenticated TCP listener on 2375.
                args += ['dockerd', '--host=unix:///var/run/docker.sock']
            else:
                args += ['-c', 'exec sleep infinity']
            created = await self._docker(*args)
            if created.exit_code:
                # A different API process may have won creation. Docker's name
                # uniqueness is the cross-process arbiter.
                state = await self._owned(name)
                if state is None:
                    if network_created:
                        # Safe and recoverable: this network was created by this
                        # failed request and has never hosted a managed container.
                        await self._docker('network', 'rm', network)
                    raise RuntimeError(created.render())
            state = await self._owned(name)
        else:
            state = await self._connect_network(name, state)
        if not state['State']['Running']:
            result = await self._docker('start', name)
            if result.exit_code:
                raise RuntimeError(result.render())
        return await self._owned(name)

    async def execute(self, workspace: Path, project: str, action: str, command: str = '',
                      timeout_seconds: int = 90, max_projects: int | None = None) -> str:
        if not self.settings.enabled or not self.settings.projects_enabled:
            return 'Error: persistent projects are disabled. Set SBOT_SANDBOX__PROJECTS_ENABLED=true.'
        if action not in {'start', 'status', 'stop', 'exec', 'compose_up', 'compose_ps', 'compose_logs', 'compose_down'}:
            raise ValueError('unknown project action')
        if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 1800:
            raise ValueError('timeout_seconds must be between 1 and 1800')
        name, path = self.identity(workspace, project)
        if action in {'start', 'stop'}:
            for key in tuple(self._proxy_targets):
                if key[:2] == (str(workspace.resolve()), project):
                    self._proxy_targets.pop(key, None)
        # A quota check and a new project creation must share an owner-wide
        # lock. Per-project locks alone allow two simultaneous first requests
        # for different slugs to both see spare capacity.
        capacity_key = f"project-capacity:{workspace.resolve()}"
        if max_projects is not None and action not in {"status", "stop"}:
            async with self._locks.get(capacity_key):
                return await self._execute(name, path, workspace, project, action, command, timeout_seconds, max_projects)
        return await self._execute(name, path, workspace, project, action, command, timeout_seconds, max_projects)

    async def _execute(self, name, path, workspace, project, action, command, timeout_seconds, max_projects) -> str:
        # Serialize commands/lifecycle for one project. Different projects run
        # concurrently. Team members should use git worktrees for parallel edits.
        async with self._locks.get(name):
            if action in {'status', 'stop'}:
                state = await self._owned(name)
                if state is None:
                    return json.dumps({'project': project, 'state': 'not_created'})
                if action == 'stop':
                    return (await self._docker('stop', '--time', '10', name)).render()
            else:
                if max_projects is not None and await self._owned(name) is None:
                    if len(await self.list(workspace)) >= max_projects:
                        return f"Error: project container limit reached ({max_projects})."
                state = await self._ensure(name, path)
            if action in {'start', 'status'}:
                return json.dumps({'project': project, 'container': name,
                                   'state': state['State']['Status'],
                                   'files': f'projects/{project}', 'shell_cwd': '/workspace',
                                   'host_bind_ip': self.settings.project_host_bind_ip,
                                   'public_ingress_port': self.settings.project_ingress_port,
                                   'public_bind': f'0.0.0.0:{self.settings.project_ingress_port}',
                                   'ports': state['NetworkSettings'].get('Ports', {})})
            if action.startswith('compose_'):
                if not self.settings.project_docker_enabled:
                    return 'Error: Compose requires SBOT_SANDBOX__PROJECT_DOCKER_ENABLED=true.'
                commands = {'compose_up': 'up -d --build --wait --wait-timeout 120',
                            'compose_ps': 'ps --all --format json',
                            'compose_logs': 'logs --no-color --tail 100',
                            'compose_down': 'down --remove-orphans'}
                command = ('i=0; until docker info >/dev/null 2>&1; do i=$((i+1)); '
                           'if [ $i -ge 30 ]; then echo "Docker daemon not ready" >&2; exit 1; fi; '
                           f'sleep 1; done; docker compose -p app {commands[action]}')
            if not command.strip():
                raise ValueError('exec requires command')
            return (await self._exec(name, command, timeout_seconds)).render()

    async def _exec(self, name, command, seconds):
        # The timeout lives INSIDE the container; killing the Docker CLI alone
        # leaves docker-exec work running. A pid file lets cancellation stop only
        # this command's process group, without stopping long-lived services.
        token = uuid.uuid4().hex
        pidfile = f'/tmp/sbot-exec-{token}.pid'
        script = f'echo $$ > {pidfile}; exec timeout -k 5 {seconds} /bin/sh -lc "$1"'
        try:
            result = await self._docker('exec', '-w', '/workspace', name,
                                        'setsid', '--wait', '/bin/sh', '-c', script, 'sbot', command,
                                        timeout=seconds + 15)
            if result.exit_code in {124, 137}:
                result.timed_out = True
            return result
        finally:
            # Also handles host-side deadline/cancellation. Never interpolate
            # user input in cleanup; token is generated by this process.
            await asyncio.shield(self._docker(
                'exec', name, '/bin/sh', '-c',
                f'if [ -f {pidfile} ]; then p=$(cat {pidfile}); '
                f'kill -KILL -- -"$p" 2>/dev/null || true; rm -f {pidfile}; fi',
                timeout=10,
            ))
