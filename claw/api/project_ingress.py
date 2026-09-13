"""Host-based reverse proxy for signed, multi-tenant project URLs."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect as websocket_connect

from sbot.sandbox.project_ingress import resolve_project_ingress_host


_HOP_HEADERS = {
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"te", b"trailer", b"transfer-encoding", b"upgrade",
}
_WS_HEADERS = _HOP_HEADERS | {
    b"host", b"sec-websocket-accept", b"sec-websocket-extensions",
    b"sec-websocket-key", b"sec-websocket-protocol", b"sec-websocket-version",
}


class ProjectIngressMiddleware:
    def __init__(self, app, *, settings, projects, workspaces_root, secret_key: str):
        self.app = app
        self.settings = settings
        self.projects = projects
        self.workspaces_root = workspaces_root
        self.secret_key = secret_key
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=10),
            trust_env=False,
            limits=httpx.Limits(
                max_connections=settings.project_ingress_max_connections,
                max_keepalive_connections=min(64, settings.project_ingress_max_connections),
            ),
        )
        self._global_connections = asyncio.Semaphore(settings.project_ingress_max_connections)
        self._project_connections: dict[tuple[str, str], asyncio.Semaphore] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            try:
                await self.app(scope, receive, send)
            finally:
                await self.client.aclose()
            return
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode("latin-1")
        domain = self.settings.project_ingress_domain
        hostname = host.partition(":")[0].lower().rstrip(".")
        if not domain or not hostname.endswith(f".{domain}"):
            await self.app(scope, receive, send)
            return
        if not (
            self.settings.enabled
            and self.settings.projects_enabled
            and self.settings.project_public_ingress_enabled
        ):
            await self._reject(scope, send, 404, "Project ingress is disabled")
            return
        route = await asyncio.to_thread(
            resolve_project_ingress_host, host, self.settings, self.secret_key, self.workspaces_root
        )
        if route is None:
            await self._reject(scope, send, 404, "Project route not found")
            return
        owner_id, project = route
        try:
            target = await self.projects.proxy_target(
                self.workspaces_root / owner_id, project, self.settings.project_ingress_port
            )
        except (RuntimeError, ValueError):
            target = None
        if target is None:
            await self._reject(scope, send, 503, "Project application is not running")
            return
        project_limit = self._project_connections.setdefault(
            (owner_id, project),
            asyncio.Semaphore(self.settings.project_ingress_max_connections_per_project),
        )
        acquired_global = acquired_project = False
        try:
            async with asyncio.timeout(0.1):
                await self._global_connections.acquire()
                acquired_global = True
                await project_limit.acquire()
                acquired_project = True
        except TimeoutError:
            if acquired_project:
                project_limit.release()
            if acquired_global:
                self._global_connections.release()
            await self._reject(scope, send, 503, "Project ingress is busy")
            return
        try:
            if scope["type"] == "websocket":
                await self._websocket(scope, receive, send, target, host)
            else:
                await self._http(scope, receive, send, target, host)
        finally:
            if acquired_project:
                project_limit.release()
            if acquired_global:
                self._global_connections.release()

    @staticmethod
    async def _reject(scope, send, status: int, message: str) -> None:
        if scope["type"] == "websocket":
            code = 1013 if status == 503 and message == "Project ingress is busy" else 1008
            await send({"type": "websocket.close", "code": code, "reason": message})
            return
        body = message.encode()
        await send({
            "type": "http.response.start", "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cache-control", b"no-store")],
        })
        await send({"type": "http.response.body", "body": body})

    def _forward_headers(self, scope, public_host: str) -> list[tuple[bytes, bytes]]:
        incoming_headers = scope.get("headers") or []
        connection_tokens = {
            token.strip().lower()
            for key, value in incoming_headers if key.lower() == b"connection"
            for token in value.split(b",") if token.strip()
        }
        headers = [
            (key, value) for key, value in incoming_headers
            if key.lower() not in _HOP_HEADERS | connection_tokens | {
                b"forwarded", b"host", b"x-forwarded-for", b"x-forwarded-host",
                b"x-forwarded-port", b"x-forwarded-prefix", b"x-forwarded-proto",
            }
        ]
        incoming = dict(incoming_headers)
        forwarded_for = incoming.get(b"cf-connecting-ip")
        client = scope.get("client")
        if forwarded_for is None and client:
            forwarded_for = str(client[0]).encode()
        if forwarded_for:
            headers.append((b"x-forwarded-for", forwarded_for))
        proto = self.settings.project_ingress_scheme.encode()
        public_host_bytes = public_host.encode("latin-1")
        headers.extend([
            (b"host", public_host_bytes),
            (b"x-forwarded-host", public_host_bytes),
            (b"x-forwarded-proto", proto),
        ])
        return headers

    async def _http(self, scope, receive, send, target: str, public_host: str) -> None:
        path = scope.get("raw_path") or scope.get("path", "/").encode()
        query = scope.get("query_string", b"")
        url = f"{target}{path.decode('latin-1')}" + (f"?{query.decode('latin-1')}" if query else "")

        async def request_body():
            more = True
            while more:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                if event["type"] == "http.request":
                    if event.get("body"):
                        yield event["body"]
                    more = event.get("more_body", False)

        response = None
        try:
            request = self.client.build_request(
                scope["method"], url, headers=self._forward_headers(scope, public_host), content=request_body()
            )
            response = await self.client.send(request, stream=True)
            response_headers = [
                (key, value) for key, value in response.headers.raw if key.lower() not in _HOP_HEADERS
            ]
            await send({"type": "http.response.start", "status": response.status_code, "headers": response_headers})
            async for chunk in response.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
        except httpx.HTTPError:
            if response is None:
                await self._reject(scope, send, 502, "Project application is unavailable")
        finally:
            if response is not None:
                await response.aclose()

    async def _websocket(self, scope, receive, send, target: str, public_host: str) -> None:
        event = await receive()
        if event["type"] != "websocket.connect":
            return
        path = scope.get("raw_path") or scope.get("path", "/").encode()
        query = scope.get("query_string", b"")
        upstream = urlsplit(target)
        # Use the public URL to generate the handshake Host header, while the
        # host/port overrides below keep DNS and TCP strictly on the managed
        # container target returned by ProjectEnvironments.
        url = f"ws://{public_host}{path.decode('latin-1')}" + (
            f"?{query.decode('latin-1')}" if query else ""
        )
        headers = [
            (key.decode("latin-1"), value.decode("latin-1"))
            for key, value in self._forward_headers(scope, public_host)
            if key.lower() not in _WS_HEADERS
        ]
        accepted = False
        client_disconnected = False
        try:
            async with websocket_connect(
                url, additional_headers=headers, subprotocols=scope.get("subprotocols") or None,
                open_timeout=10, max_size=self.settings.project_ingress_ws_max_bytes, proxy=None,
                host=upstream.hostname, port=upstream.port,
            ) as upstream_socket:
                await send({"type": "websocket.accept", "subprotocol": upstream_socket.subprotocol})
                accepted = True

                async def to_upstream() -> None:
                    nonlocal client_disconnected
                    while True:
                        message = await receive()
                        if message["type"] == "websocket.disconnect":
                            client_disconnected = True
                            await upstream_socket.close(code=message.get("code", 1000))
                            return
                        if message["type"] == "websocket.receive":
                            data = message.get("text")
                            await upstream_socket.send(data if data is not None else message.get("bytes", b""))

                async def to_client() -> None:
                    async for message in upstream_socket:
                        key = "text" if isinstance(message, str) else "bytes"
                        await send({"type": "websocket.send", key: message})

                tasks = {asyncio.create_task(to_upstream()), asyncio.create_task(to_client())}
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if not client_disconnected:
                    await send({
                        "type": "websocket.close",
                        "code": upstream_socket.close_code or 1000,
                        "reason": upstream_socket.close_reason or "",
                    })
        except Exception:
            if not client_disconnected:
                await send({
                    "type": "websocket.close", "code": 1011,
                    "reason": "Project application is unavailable" if not accepted else "Upstream connection failed",
                })
