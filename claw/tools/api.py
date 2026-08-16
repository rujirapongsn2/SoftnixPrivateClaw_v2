"""Generic REST API connector tool (connector kind="api").

Unlike every other connector, which speaks the MCP protocol over a subprocess
or streamable-HTTP session, an api-kind connector is just a plain REST base
URL plus a list of declared operations (claw/api/connector_shared.py's
ApiOperation) — there is no handshake, no session, no subprocess. One
GenericApiTool instance is built per operation, straight from the stored
connector row, and each call is a standalone httpx request (see
claw/core/connectors.py's _build_api_tools).

Auth reuses the exact same HEADER_*/QUERY_* env convention an http-transport
MCP connector already uses (see claw/core/connectors.py's _connect) — read
here at call time instead of at connect time, so no new auth syntax is
needed in the Connectors UI.
"""

import asyncio
import json
import re
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from claw.security.ssrf import UnsafeUrlError, resolve_public_ips
from claw.tools.base import Tool

_MAX_RESPONSE_CHARS = 30_000
# Only _MAX_RESPONSE_CHARS ever reaches the transcript, so this exists purely to
# bound what a call may allocate: the base URL and path are user-controlled, and
# a buffering read of an endpoint that serves a multi-GB file would OOM the
# process and take every other tenant's in-flight turn down with it.
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_REDIRECTS = 5
_DEFAULT_PORTS = {"http": 80, "https": 443}
_PATH_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Mirrors claw/core/connectors.py's _HEADER_ENV_PREFIX/_QUERY_ENV_PREFIX
# (not imported directly to avoid a claw.tools <-> claw.core import cycle at
# module load time — connectors.py imports GenericApiTool lazily instead).
_HEADER_ENV_PREFIX = "HEADER_"
_QUERY_ENV_PREFIX = "QUERY_"

_JSON_TYPES = {"string": "string", "number": "number", "boolean": "boolean"}

# Headers that describe *this* connection rather than the request, and so must
# be computed per-hop rather than replayed from stored env. A pasted browser
# "Copy as cURL" carries all of them, and the Connectors UI imports every
# header it finds (web/src/Settings.tsx's CurlImportPanel). A stale
# Content-Length is the sharpest edge: httpx does not recompute over a
# caller-supplied one, so every call of that connector would die with a
# LocalProtocolError. Host is dropped for a second reason — it is what pins
# TLS/SNI to the validated hostname (see _build_pinned_request).
# Accept-Encoding belongs here too: a browser's "Copy as cURL" advertises
# "br, zstd", but httpx can only decode what its optional codec deps provide
# (brotli/zstandard are not installed here) and silently falls back to an
# identity decoder — so the response arrives compressed and the model reads
# binary garbage under a 200. Letting httpx set it means it only ever
# advertises what it can actually decode.
_CONNECTION_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "accept-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _format_value(value: Any) -> str:
    """Python's str() renders bools as "True"/"False"; HTTP APIs expect the
    JSON spelling, and a stray "False" is truthy to most query parsers."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _quote_path_value(name: str, value: Any) -> str:
    formatted = _format_value(value)
    # An empty value collapses the segment: "/v1/users/{id}" with id="" is
    # "/v1/users/", the *collection* endpoint, reached with this connector's
    # credentials attached — a "read one user" tool silently becomes "list
    # every user" (or, on DELETE, something far worse). Tool._validate only
    # checks key presence and JSON type, so "" passes as a required string.
    if not formatted or formatted in (".", ".."):
        raise UnsafeUrlError(f"path parameter {name!r} must be a non-empty path segment")
    return quote(formatted, safe="")


def _build_json_schema(parameters: list[dict]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in parameters:
        properties[p["name"]] = {
            "type": _JSON_TYPES.get(p.get("type", "string"), "string"),
            "description": p.get("description") or f"{p['location']} parameter",
        }
        if p.get("required"):
            required.append(p["name"])
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


class GenericApiTool(Tool):
    """Stateless — holds no session, just the connector row's static base url/
    env plus one operation's static definition. Safe to rebuild on every
    sync_tools/sync_global pass with zero I/O (see claw/core/connectors.py's
    _build_api_tools)."""

    def __init__(
        self,
        connector: Any,
        operation: dict,
        *,
        timeout_seconds: float,
        connector_ref: Callable[[], Any] | None = None,
    ):
        self._connector = connector
        # `connector_ref`, when given, is looked up fresh on every call instead
        # of using the captured `connector` — used only for admin-global
        # connectors (see claw/core/connectors.py's sync_global), and the exact
        # counterpart of McpToolProxy's `session_ref`. A global connector's row
        # can be edited (credential rotated, base url repointed) or disabled
        # while a tool built from the OLD row is still registered in some other
        # user's registry — it's only re-registered on that user's next
        # sync_tools. An MCP proxy fails safe there because its session is
        # closed; this tool has no session at all, so without the indirection
        # it would keep issuing live requests with the revoked credential, or
        # keep serving a connector the admin has already turned off.
        self._connector_ref = connector_ref
        self._method = operation["method"]
        self._path_template = operation["path"]
        self._parameters = operation.get("parameters") or []
        self._body_template = operation.get("body") or ""
        self._timeout_seconds = timeout_seconds
        self.name = f"api_{connector.name}_{operation['name']}"
        self.description = f"[{connector.name}] {operation.get('description') or operation['name']}"
        self.parameters = _build_json_schema(self._parameters)

    async def execute(self, **kwargs: Any) -> str:
        # Deferred to avoid a claw.tools <-> claw.core import cycle — see the
        # module docstring. By call time both modules are already fully
        # loaded (claw.core.connectors is what constructs this tool).
        from claw.core.connectors import _redact_secrets

        connector = self._connector_ref() if self._connector_ref is not None else self._connector
        if connector is None:
            return f"Error: {self.name} is no longer available — the connector was changed or disabled"
        env = connector.env or {}

        query_names = {p["name"] for p in self._parameters if p["location"] == "query"}
        header_names = {p["name"] for p in self._parameters if p["location"] == "header"}

        # quote(..., safe="") escapes "/" in a value too, so a parameter
        # value can never smuggle extra path segments (or a different host,
        # via "://") past the operation's declared, validated path template.
        # "." and ".." survive quoting untouched though, and httpx.URL then
        # resolves them away — an id of ".." on "/v1/users/{id}" would reach
        # "/v1" instead, with this connector's credentials attached.
        try:
            path = _PATH_PARAM_RE.sub(
                lambda m: _quote_path_value(m.group(1), kwargs.get(m.group(1), "")),
                self._path_template,
            )
        except UnsafeUrlError as exc:
            return f"Error: {self._method} {self._path_template} blocked: {exc}"
        # Split rather than concatenate, so a base url that carries its own
        # query string (e.g. "https://api.example.com/v1?legacy=1") gets the
        # operation's path inserted BEFORE that query string — plain
        # concatenation would produce ".../v1?legacy=1/users/42", a different
        # endpoint entirely.
        base = urlsplit(connector.url)
        url = urlunsplit(
            (base.scheme, base.netloc, base.path.rstrip("/") + path, base.query, base.fragment)
        )

        # Env-configured headers/query are the connector's credentials, so they
        # win over anything the model passes in. Without this a declared
        # "Authorization" (or "api_key") parameter lets a single tool call
        # replace the stored secret with an attacker-chosen one — the sharp
        # case being an admin-global connector, where the operator's credential
        # is meant to be opaque to the user whose turn is calling it. Header
        # names are compared case-insensitively; query keys are not, because
        # query strings are case-sensitive.
        #
        # Two env keys differing only in case ("HEADER_authorization" and
        # "HEADER_Authorization") name the same HTTP header, and emitting both
        # would leave the server to choose which credential applies. Save-time
        # validation (claw/api/connector_shared.py) rejects that pair now, but a
        # row written before it — or by a preset — can still hold one, so keys
        # are folded here too, deterministically by sorted key so the same row
        # always sends the same header rather than whatever the dict order was.
        headers: dict[str, str] = {}
        claimed: set[str] = set()
        for key in sorted(env):
            value = env[key]
            if not key.startswith(_HEADER_ENV_PREFIX) or not value:
                continue
            name = key[len(_HEADER_ENV_PREFIX):]
            lowered = name.lower()
            if lowered in _CONNECTION_HEADERS or lowered in claimed:
                continue
            claimed.add(lowered)
            headers[name] = value
        reserved_headers = set(claimed)
        for name in header_names:
            lowered = name.lower()
            if lowered in _CONNECTION_HEADERS or lowered in reserved_headers:
                continue
            if name in kwargs:
                headers[name] = _format_value(kwargs[name])

        env_query = {
            key[len(_QUERY_ENV_PREFIX):]: value
            for key, value in env.items()
            if key.startswith(_QUERY_ENV_PREFIX) and value
        }
        query = dict(env_query)
        for name in query_names:
            if name in kwargs and name not in env_query:
                query[name] = _format_value(kwargs[name])

        body_content: str | None = None
        if self._body_template:
            # Each {placeholder} is replaced with json.dumps() of the caller's
            # value, never the raw string — that's what keeps the template
            # valid JSON regardless of the substituted type (a bare `{limit}`
            # in `{"limit": {limit}}` becomes `20`, a string becomes `"abc"`
            # with correct quoting/escaping). See connector_shared.py's
            # upsert_connector for the matching save-time validation.
            body_content = _PATH_PARAM_RE.sub(
                lambda m: json.dumps(kwargs.get(m.group(1))), self._body_template
            )
            # setdefault would compare case-sensitively, so an imported
            # "HEADER_content-type" leaves the dict holding BOTH keys — and
            # httpx sends both, which some servers reject outright and others
            # resolve to whichever they see first.
            if not _has_header(headers, "content-type"):
                headers["Content-Type"] = "application/json"

        # copy_merge_params, not httpx's `params=`: passing params to a request
        # whose URL already has a query string REPLACES that query rather than
        # merging it, which would silently drop a base url's own credentials
        # (".../v1?api_version=2") on any operation that declares a parameter.
        target = httpx.URL(url)
        if query:
            target = target.copy_merge_params(query)

        try:
            # follow_redirects stays off so each hop can be re-validated and
            # re-pinned here; httpx's own redirect handling strips Authorization
            # cross-origin but re-sends every other header, and this connector's
            # credentials usually live in HEADER_x-api-key-style headers.
            #
            # httpx's `timeout=` is per phase (connect, then read, then write),
            # not per request, and the read timeout restarts on every chunk.
            # A server that drips one byte just inside the read timeout, across
            # up to _MAX_REDIRECTS + 1 hops and _MAX_RESPONSE_BYTES of body,
            # can hold a chat turn open for hours under a nominal 60s setting.
            # asyncio.timeout is the only thing here that bounds the whole call.
            async with asyncio.timeout(self._timeout_seconds):
                async with httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=False) as client:
                    status_code, text = await self._send(
                        client, target, headers, body_content, env_query
                    )
        except (httpx.TimeoutException, TimeoutError):
            return f"Error: {self._method} {self._path_template} timed out after {self._timeout_seconds}s"
        except UnsafeUrlError as exc:
            return f"Error: {self._method} {self._path_template} blocked: {exc}"
        except Exception as exc:
            # Deliberately broader than httpx.HTTPError: httpx.InvalidURL (a bad
            # stored base url) does NOT subclass it, and anything not caught
            # here lands in ToolRegistry.execute's catch-all, which does no
            # redaction at all.
            return _redact_secrets(
                f"Error: {self._method} {self._path_template} failed: {exc}", connector
            )

        if status_code >= 400:
            return _redact_secrets(
                f"Error: {self._method} {self._path_template} failed with status {status_code}\n\n{text}",
                connector,
            )
        # Success bodies are redacted too, not just errors: an endpoint that
        # echoes the request back (debug/echo/webhook-registration routes are
        # common) would otherwise replay this connector's auth header or api
        # key verbatim into the transcript.
        return _redact_secrets(
            f"[{status_code}] {self._method} {self._path_template}\n\n{text}", connector
        )

    async def _send(
        self,
        client: httpx.AsyncClient,
        url: httpx.URL,
        headers: dict[str, str],
        body: str | None,
        query_credentials: dict[str, str],
    ) -> tuple[int, str]:
        """Follow redirects by hand, re-validating and re-pinning every hop."""
        method = self._method
        origin = _origin_of(url)

        for _ in range(_MAX_REDIRECTS + 1):
            response = await _send_pinned(client, method, url, headers, body)
            try:
                location = response.headers.get("location")
                if response.is_redirect and location:
                    url = url.join(location)
                    # Method/body downgrade first, so the legitimate
                    # "POST here, 303, GET there" pattern still crosses
                    # origins — by then it carries no body to leak.
                    if response.status_code == 303 or (
                        response.status_code in (301, 302) and method not in ("GET", "HEAD")
                    ):
                        method = "GET"
                        body = None
                        _drop_header(headers, "content-type")
                    if _origin_of(url) != origin:
                        # 307/308 (and a 301/302 on a GET) preserve method and
                        # body verbatim. A rendered body template can hold a
                        # literal credential — the cURL importer copies
                        # whatever the pasted example carried — so replaying
                        # it at an origin the
                        # operator never configured is an exfiltration channel
                        # that stripping headers alone does not close.
                        if body is not None:
                            raise UnsafeUrlError(
                                f"refusing cross-origin redirect to {url.host!r}: "
                                "it would replay the request body"
                            )
                        # Every connector-supplied header is a potential
                        # credential (HEADER_authorization, HEADER_x-api-key,
                        # a tenant id...), so none survive the hop either.
                        headers = {}
                        query_credentials = {}
                        origin = _origin_of(url)
                    elif query_credentials:
                        # RFC 3986 reference resolution takes the query from the
                        # Location header, so a connector authenticating with
                        # QUERY_* loses its credentials on the very first hop —
                        # even a same-origin "/v1/x" -> "/v2/x" one. Re-attach
                        # them, mirroring how HEADER_* survives same-origin.
                        url = url.copy_merge_params(query_credentials)
                    continue
                return response.status_code, await _read_capped(response)
            finally:
                await response.aclose()

        raise UnsafeUrlError(f"too many redirects (>{_MAX_REDIRECTS})")


def _origin_of(url: httpx.URL) -> tuple[str, str, int]:
    return (url.scheme, url.host, url.port or _DEFAULT_PORTS.get(url.scheme, 0))


def _has_header(headers: dict[str, str], name: str) -> bool:
    return any(key.lower() == name for key in headers)


def _drop_header(headers: dict[str, str], name: str) -> None:
    for key in [key for key in headers if key.lower() == name]:
        del headers[key]


async def _send_pinned(
    client: httpx.AsyncClient,
    method: str,
    url: httpx.URL,
    headers: dict[str, str],
    body: str | None,
) -> httpx.Response:
    """Validate `url`'s host and send the request to the exact IP that was
    validated, so the socket cannot re-resolve the name to a private address in
    between (see claw/security/ssrf.py).

    Pinning removes the resolver's own address fallback, so the fallback is
    reimplemented here: try each validated address until one connects. Only a
    connect-level failure moves on — once the peer has answered, its response
    (or its error) is the answer.
    """
    addresses = await resolve_public_ips(str(url))
    if not addresses:
        # Host is already an IP literal; there is nothing to pin.
        return await client.send(client.build_request(method, url, headers=headers, content=body), stream=True)

    # The URL now carries an IP, so both the Host header and TLS validation
    # would otherwise follow the IP rather than the name the operator
    # configured. sni_hostname keeps certificate verification pinned to the
    # real hostname; without it every https connector would fail to verify.
    request_headers = dict(headers)
    request_headers["Host"] = url.netloc.decode("ascii")
    last_error: httpx.TransportError | None = None
    for address in addresses:
        request = client.build_request(
            method,
            url.copy_with(host=address),
            headers=request_headers,
            content=body,
            extensions={"sni_hostname": url.host},
        )
        try:
            return await client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = exc
    raise last_error  # type: ignore[misc]


async def _read_capped(response: httpx.Response) -> str:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= _MAX_RESPONSE_BYTES:
            break
    text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    if len(text) > _MAX_RESPONSE_CHARS:
        return text[:_MAX_RESPONSE_CHARS] + "\n\n[truncated]"
    return text
