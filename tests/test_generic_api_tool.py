"""GenericApiTool: the kind="api" connector's per-operation tool. No MCP
session involved — each call is a standalone httpx request built straight
from the stored connector row (see claw/tools/api.py)."""

import asyncio
import json
from types import SimpleNamespace

import httpx

import claw.core.connectors  # noqa: F401
import claw.tools.api as api_mod
from claw.core.connectors import _redact_secrets
from claw.tools.api import GenericApiTool
from claw.tools.registry import ToolRegistry

# claw.core.connectors is imported above purely for ordering: _mock_client
# below swaps the shared httpx module's AsyncClient attribute, and importing
# claw.core.connectors (which GenericApiTool.execute does lazily, for
# _redact_secrets) pulls in mcp.client.streamable_http, whose eagerly evaluated
# annotations reference httpx.AsyncClient. Importing it first keeps this file
# runnable on its own.


def _connector(**overrides):
    defaults = dict(name="myapi", url="https://api.example.com", env={})
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _operation(**overrides):
    defaults = dict(
        name="get_user",
        method="GET",
        path="/users/{id}",
        description="Fetch a user",
        parameters=[{"name": "id", "location": "path", "type": "string", "required": True}],
    )
    defaults.update(overrides)
    return defaults


def _mock_client(monkeypatch, handler, *, resolve=None):
    """Patch httpx.AsyncClient (as imported into claw.tools.api) so every
    request made during the test is served by `handler` instead of hitting
    the network.

    The SSRF guard is stubbed out too: it resolves the request host for real,
    and these hosts are deliberately unroutable, so leaving it live would make
    every test here depend on DNS. Pass `resolve` to exercise the guard itself;
    it may return one address, a list of them, or None for "IP literal, nothing
    to pin".
    """

    async def _resolve(url):
        result = resolve(url) if resolve else None
        if result is None:
            return []
        return [result] if isinstance(result, str) else list(result)

    monkeypatch.setattr(api_mod, "resolve_public_ips", _resolve)
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(api_mod.httpx, "AsyncClient", lambda **kw: real_client(transport=transport))


async def test_success_substitutes_path_param_and_returns_body(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, text='{"ok": true}')

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    result = await tool.execute(id="42")

    assert "/users/42" in captured["url"]
    assert "[200]" in result
    assert '"ok": true' in result


async def test_path_param_containing_slash_is_url_encoded_not_smuggled(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # raw_path preserves percent-encoding; .path would decode %2F back
        # to "/" and mask exactly the bug this test guards against.
        captured["raw_path"] = request.url.raw_path
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    await tool.execute(id="../admin")

    # "/" in the value must be percent-encoded, never smuggled as extra
    # path segments past the operation's declared template.
    assert captured["raw_path"] == b"/users/..%2Fadmin"


async def test_non_2xx_status_returns_error_text_with_body(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    result = await tool.execute(id="1")

    assert result.startswith("Error: GET /users/{id} failed with status 404")
    assert "not found" in result


async def test_timeout_returns_friendly_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    result = await tool.execute(id="1")

    assert result.startswith("Error:")
    assert "timed out after 5s" in result


async def test_connection_error_redacts_env_secret(monkeypatch):
    secret = "SUPERSECRETVALUE123"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed for url containing {secret}", request=request)

    _mock_client(monkeypatch, handler)
    connector = _connector(env={"HEADER_Authorization": secret})
    tool = GenericApiTool(connector, _operation(), timeout_seconds=5)

    result = await tool.execute(id="1")

    assert secret not in result
    assert "***" in result


async def test_header_and_query_params_combined_from_env_and_declared(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["query"] = dict(httpx.QueryParams(request.url.query))
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    connector = _connector(env={"HEADER_Authorization": "Bearer secret", "QUERY_apikey": "envkey"})
    op = _operation(
        name="search",
        path="/search",
        parameters=[
            {"name": "q", "location": "query", "type": "string", "required": True},
            {"name": "X-Trace", "location": "header", "type": "string", "required": False},
        ],
    )
    tool = GenericApiTool(connector, op, timeout_seconds=5)

    await tool.execute(q="hello", **{"X-Trace": "abc123"})

    assert captured["headers"]["authorization"] == "Bearer secret"
    assert captured["headers"]["x-trace"] == "abc123"
    assert captured["query"]["apikey"] == "envkey"
    assert captured["query"]["q"] == "hello"


async def test_missing_required_parameter_caught_by_registry_before_execute():
    """ToolRegistry.execute() validates params against the tool's JSON schema
    before calling execute() at all — a missing required path parameter never
    reaches GenericApiTool.execute (and therefore never fires a real/mocked
    HTTP request)."""
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    called = False

    async def spy_execute(**kwargs):
        nonlocal called
        called = True
        return "should not be reached"

    tool.execute = spy_execute

    registry = ToolRegistry()
    registry.register(tool)
    result = await registry.execute(tool.name, {})

    assert not called
    assert "invalid parameters" in result
    assert "missing required id" in result


async def test_success_body_echoing_secret_is_redacted(monkeypatch):
    """An endpoint that echoes the request back (debug/echo routes) would
    otherwise replay the connector's auth header into the transcript — success
    bodies get the same redaction errors already had."""
    secret = "SUPERSECRETVALUE123"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f'{{"echoed_auth": "{secret}"}}')

    _mock_client(monkeypatch, handler)
    connector = _connector(env={"HEADER_Authorization": secret})
    tool = GenericApiTool(connector, _operation(), timeout_seconds=5)

    result = await tool.execute(id="1")

    assert secret not in result
    assert "***" in result


async def test_non_http_error_exception_is_caught_and_redacted(monkeypatch):
    """httpx.InvalidURL does NOT subclass httpx.HTTPError, so a bad stored base
    url would otherwise escape to ToolRegistry's catch-all, which never
    redacts."""
    secret = "SUPERSECRETVALUE123"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL(f"invalid url {secret}")

    _mock_client(monkeypatch, handler)
    connector = _connector(env={"HEADER_Authorization": secret})
    tool = GenericApiTool(connector, _operation(), timeout_seconds=5)

    result = await tool.execute(id="1")

    assert result.startswith("Error:")
    assert secret not in result
    assert "***" in result


async def test_boolean_params_serialize_as_json_booleans(monkeypatch):
    """str(True) == "True"; most query parsers treat the string "False" as
    truthy, silently inverting the filter."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = dict(httpx.QueryParams(request.url.query))
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="search",
        path="/search",
        parameters=[
            {"name": "active", "location": "query", "type": "boolean", "required": True},
            {"name": "X-Debug", "location": "header", "type": "boolean", "required": False},
        ],
    )
    tool = GenericApiTool(_connector(), op, timeout_seconds=5)

    await tool.execute(active=False, **{"X-Debug": True})

    assert captured["query"]["active"] == "false"
    assert captured["headers"]["x-debug"] == "true"


async def test_body_template_substitutes_typed_values_as_json(monkeypatch):
    """A string value gets quoted, a number/boolean doesn't — the template
    itself never quotes the placeholder (see claw/api/connector_shared.py's
    ApiOperation.body docstring)."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="create_user",
        method="POST",
        path="/users",
        parameters=[
            {"name": "name", "location": "body", "type": "string", "required": True},
            {"name": "age", "location": "body", "type": "number", "required": True},
        ],
        body='{"name": {name}, "age": {age}}',
    )
    tool = GenericApiTool(_connector(), op, timeout_seconds=5)

    await tool.execute(name="Ada", age=30)

    assert json.loads(captured["body"]) == {"name": "Ada", "age": 30}
    assert captured["headers"]["content-type"] == "application/json"


async def test_no_body_template_sends_no_content(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["has_content_type"] = "content-type" in request.headers
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    await tool.execute(id="1")

    assert captured["body"] == b""
    assert not captured["has_content_type"]


async def test_body_value_containing_secret_is_redacted_on_error(monkeypatch):
    """A body param's value isn't itself redacted (it's caller-supplied, not a
    connector secret) — but a connector env secret must still be redacted
    from the error text even when a body is being sent."""
    secret = "SUPERSECRETVALUE123"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"upstream failed, header was {secret}")

    _mock_client(monkeypatch, handler)
    connector = _connector(env={"HEADER_Authorization": secret})
    op = _operation(
        name="create_user",
        method="POST",
        path="/users",
        parameters=[{"name": "name", "location": "body", "type": "string", "required": True}],
        body='{"name": {name}}',
    )
    tool = GenericApiTool(connector, op, timeout_seconds=5)

    result = await tool.execute(name="Ada")

    assert secret not in result
    assert "***" in result


async def test_redirect_to_another_origin_drops_connector_credentials(monkeypatch):
    """The whole point of following redirects by hand: httpx's own redirect
    handling strips only Authorization/Cookie cross-origin, so an api key in
    HEADER_x-api-key would be replayed verbatim to whatever host the upstream
    named."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.example.com":
            return httpx.Response(302, headers={"location": "https://attacker.example.net/steal"})
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    connector = _connector(env={"HEADER_Authorization": "Bearer tok", "HEADER_X-Api-Key": "k-123"})
    tool = GenericApiTool(connector, _operation(), timeout_seconds=5)

    result = await tool.execute(id="42")

    assert "[200]" in result
    assert seen[0].headers["x-api-key"] == "k-123"
    assert "x-api-key" not in seen[1].headers
    assert "authorization" not in seen[1].headers


async def test_same_origin_redirect_keeps_credentials(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/users/42":
            return httpx.Response(302, headers={"location": "/users/42/canonical"})
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(
        _connector(env={"HEADER_X-Api-Key": "k-123"}), _operation(), timeout_seconds=5
    )

    result = await tool.execute(id="42")

    assert "[200]" in result
    assert seen[1].url.path == "/users/42/canonical"
    assert seen[1].headers["x-api-key"] == "k-123"


async def test_same_origin_redirect_keeps_query_credentials(monkeypatch):
    """RFC 3986 reference resolution takes the query from the Location header,
    so url.join() silently drops a QUERY_*-authenticated connector's key on the
    very first hop — the second request 401s and the failure looks like a bad
    credential. HEADER_* survives same-origin; QUERY_* has to as well."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/users/42":
            return httpx.Response(302, headers={"location": "/users/42/canonical"})
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(
        _connector(env={"QUERY_api_key": "k-123"}), _operation(), timeout_seconds=5
    )

    result = await tool.execute(id="42")

    assert "[200]" in result
    assert seen[0].url.params["api_key"] == "k-123"
    assert seen[1].url.path == "/users/42/canonical"
    assert seen[1].url.params["api_key"] == "k-123"


async def test_cross_origin_redirect_drops_query_credentials(monkeypatch):
    """...and is dropped cross-origin for exactly the reason headers are: the
    upstream must not be able to name a host and be handed the key."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.example.com":
            return httpx.Response(302, headers={"location": "https://attacker.example.net/steal"})
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(
        _connector(env={"QUERY_api_key": "k-123"}), _operation(), timeout_seconds=5
    )

    result = await tool.execute(id="42")

    assert "[200]" in result
    assert "api_key" not in seen[1].url.params
    assert "k-123" not in str(seen[1].url)


async def test_a_redirect_hop_cannot_overwrite_a_query_credential(monkeypatch):
    """A redirect whose Location sets the same parameter must not be able to
    swap the connector's key for one the upstream chose."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/users/42":
            return httpx.Response(302, headers={"location": "/next?api_key=attacker&page=2"})
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(
        _connector(env={"QUERY_api_key": "k-123"}), _operation(), timeout_seconds=5
    )

    await tool.execute(id="42")

    assert seen[1].url.params["api_key"] == "k-123"
    assert seen[1].url.params["page"] == "2"


async def test_a_redirect_hop_does_not_carry_over_caller_supplied_params(monkeypatch):
    """Only the connector's own QUERY_* credentials are re-attached. An
    operation parameter is part of the request the Location supersedes, so it
    follows ordinary redirect semantics."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/users/42":
            return httpx.Response(302, headers={"location": "/users/42/canonical"})
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    op = _operation(
        parameters=[
            {"name": "id", "location": "path", "type": "string", "required": True},
            {"name": "expand", "location": "query", "type": "string"},
        ]
    )
    tool = GenericApiTool(
        _connector(env={"QUERY_api_key": "k-123"}), op, timeout_seconds=5
    )

    await tool.execute(id="42", expand="all")

    assert seen[0].url.params["expand"] == "all"
    assert "expand" not in seen[1].url.params
    assert seen[1].url.params["api_key"] == "k-123"


async def test_a_redirect_loop_is_bounded(monkeypatch):
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request.url.path)
        return httpx.Response(302, headers={"location": f"/hop{len(hops)}"})

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    result = await tool.execute(id="42")

    assert "blocked" in result
    assert "too many redirects" in result
    assert len(hops) == api_mod._MAX_REDIRECTS + 1


async def test_redirect_hop_to_a_private_address_is_blocked(monkeypatch):
    """The guard has to run per hop, not once: the first host is public and the
    connector was saved that way, but the upstream can point hop two at
    localhost or the cloud metadata endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    def resolve(url):
        if "169.254.169.254" in url:
            raise api_mod.UnsafeUrlError("URL host is a non-public address: 169.254.169.254")
        return None

    _mock_client(monkeypatch, handler, resolve=resolve)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    result = await tool.execute(id="42")

    assert "blocked" in result
    assert "non-public address" in result


async def test_request_is_pinned_to_the_validated_ip(monkeypatch):
    """Validating the hostname and then letting the socket resolve it again
    leaves a rebinding window, so the request must be aimed at the exact
    address the guard checked — with Host/SNI still carrying the real name."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        captured["host"] = request.headers["Host"]
        captured["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler, resolve=lambda url: "93.184.216.34")
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    await tool.execute(id="42")

    assert captured["url"].host == "93.184.216.34"
    assert captured["host"] == "api.example.com"
    assert captured["sni"] == "api.example.com"


async def test_an_enormous_response_body_is_capped_not_buffered(monkeypatch):
    """Only _MAX_RESPONSE_CHARS reaches the transcript either way; the byte cap
    exists so a connector pointed at a huge file can't OOM the process and take
    every other tenant's in-flight turn down with it."""
    served = 0

    async def stream():
        nonlocal served
        for _ in range(200):
            served += 1
            yield b"x" * 100_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream())

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    result = await tool.execute(id="42")

    assert result.endswith("[truncated]")
    assert len(result) < api_mod._MAX_RESPONSE_CHARS + 200
    assert served * 100_000 <= api_mod._MAX_RESPONSE_BYTES + 100_000


async def test_303_redirect_downgrades_post_to_get_and_drops_the_body(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/users":
            return httpx.Response(303, headers={"location": "/users/42"})
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="create_user",
        method="POST",
        path="/users",
        parameters=[{"name": "name", "location": "body", "type": "string", "required": True}],
        body='{"name": {name}}',
    )
    tool = GenericApiTool(_connector(), op, timeout_seconds=5)

    await tool.execute(name="Ada")

    assert seen[1].method == "GET"
    assert seen[1].read() == b""
    assert "content-type" not in seen[1].headers


async def test_connection_headers_from_env_are_not_replayed(monkeypatch):
    """A pasted "Copy as cURL" carries Content-Length/Host/Connection. httpx
    does not recompute over a caller-supplied Content-Length, so replaying a
    stale one makes every call of the connector fail outright."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    connector = _connector(
        env={
            "HEADER_Content-Length": "9999",
            "HEADER_Host": "evil.example.net",
            "HEADER_Connection": "keep-alive",
            "HEADER_X-Api-Key": "k-123",
        }
    )
    tool = GenericApiTool(connector, _operation(), timeout_seconds=5)

    result = await tool.execute(id="42")

    assert "[200]" in result
    assert captured["headers"]["x-api-key"] == "k-123"
    assert captured["headers"]["host"] == "api.example.com"
    assert captured["headers"].get("content-length") != "9999"


async def test_hyphenated_header_and_query_parameters_are_sent_verbatim(monkeypatch):
    """Header/query parameter names are used as-is, so "X-Api-Key" and
    "api-version" must survive the JSON-schema/kwargs round trip."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["url"] = request.url
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="search",
        path="/search",
        parameters=[
            {"name": "X-Api-Key", "location": "header", "type": "string", "required": True},
            {"name": "api-version", "location": "query", "type": "string", "required": True},
        ],
    )
    tool = GenericApiTool(_connector(), op, timeout_seconds=5)

    await tool.execute(**{"X-Api-Key": "k-1", "api-version": "2024-01"})

    assert captured["headers"]["x-api-key"] == "k-1"
    assert captured["url"].params["api-version"] == "2024-01"


async def test_base_url_query_string_survives_declared_query_parameters(monkeypatch):
    """httpx's `params=` REPLACES an existing query rather than merging it —
    which would silently drop a base url's own credentials on any operation
    that declares a parameter."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    connector = _connector(url="https://api.example.com/v1?api_version=2", env={"QUERY_token": "t-9"})
    op = _operation(
        name="search",
        path="/search",
        parameters=[{"name": "q", "location": "query", "type": "string", "required": True}],
    )
    tool = GenericApiTool(connector, op, timeout_seconds=5)

    await tool.execute(q="cats")

    assert captured["url"].path == "/v1/search"
    assert captured["url"].params["api_version"] == "2"
    assert captured["url"].params["token"] == "t-9"
    assert captured["url"].params["q"] == "cats"


async def test_dot_dot_path_param_is_rejected_not_normalized_away(monkeypatch):
    """quote(safe="") leaves ".." untouched and httpx.URL then resolves the dot
    segment away, so "/v1/users/{id}" with id=".." would reach "/v1" — a
    different endpoint entirely, carrying this connector's credentials."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_path"] = request.url.raw_path
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    connector = _connector(url="https://api.example.com/v1", env={"HEADER_authorization": "Bearer t-1"})
    tool = GenericApiTool(connector, _operation(), timeout_seconds=5)

    result = await tool.execute(id="..")

    assert "blocked" in result
    assert captured == {}


async def test_single_dot_path_param_is_rejected_too(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request must not be sent")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    assert "blocked" in await tool.execute(id=".")


async def test_dot_segment_lookalikes_still_pass_through(monkeypatch):
    """Only an exact "."/".." segment is a traversal — "..foo" and "%2e%2e"
    are ordinary values and must keep working."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_path"] = request.url.raw_path
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    await tool.execute(id="..foo")
    assert captured["raw_path"] == b"/users/..foo"

    await tool.execute(id="%2e%2e")
    assert captured["raw_path"] == b"/users/%252e%252e"


async def test_literal_credential_in_a_stored_body_template_is_redacted(monkeypatch):
    """`operations` is not encrypted at rest and has no BODY_* env equivalent,
    so a credential pasted into a body template can't be un-stored — but it
    must never be replayed into a transcript by an endpoint that echoes the
    request back."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=request.content.decode())

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="login",
        method="POST",
        path="/login",
        parameters=[{"name": "user", "location": "body", "type": "string", "required": True}],
        body='{"user": {user}, "client_secret": "sk-live-abcdef123456"}',
    )
    tool = GenericApiTool(_connector(operations=[op]), op, timeout_seconds=5)

    result = await tool.execute(user="ada")

    assert "sk-live-abcdef123456" not in result
    assert "***" in result
    assert "ada" in result


async def test_empty_path_param_does_not_collapse_to_the_collection_endpoint(monkeypatch):
    """"" passes Tool._validate (present, and a string), but substituting it
    turns "read one user" into "list every user" — with the connector's
    credentials attached, and on DELETE into something far worse."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    result = await tool.execute(id="")

    assert seen == []
    assert "blocked" in result


async def test_missing_path_param_is_blocked_rather_than_silently_dropped(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    result = await tool.execute()

    assert seen == []
    assert "blocked" in result


async def test_model_supplied_header_cannot_override_a_stored_credential(monkeypatch):
    """The connector's env holds the operator's credential. On an admin-global
    connector it is meant to be opaque to the calling user, so a declared
    "Authorization" parameter must not let one tool call swap it out."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    connector = _connector(env={"HEADER_Authorization": "Bearer real-operator-token"})
    op = _operation(
        parameters=[
            {"name": "id", "location": "path", "type": "string", "required": True},
            {"name": "authorization", "location": "header", "type": "string", "required": False},
        ]
    )
    tool = GenericApiTool(connector, op, timeout_seconds=5)

    await tool.execute(id="42", authorization="Bearer attacker-token")

    assert captured["headers"]["authorization"] == "Bearer real-operator-token"


async def test_model_supplied_query_param_cannot_override_a_stored_credential(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    connector = _connector(env={"QUERY_api_key": "k-real"})
    op = _operation(
        parameters=[
            {"name": "id", "location": "path", "type": "string", "required": True},
            {"name": "api_key", "location": "query", "type": "string", "required": False},
        ]
    )
    tool = GenericApiTool(connector, op, timeout_seconds=5)

    await tool.execute(id="42", api_key="k-attacker")

    assert captured["params"]["api_key"] == "k-real"


async def test_307_cross_origin_redirect_does_not_replay_the_body(monkeypatch):
    """303 drops the body before crossing, but 307/308 preserve method and
    body verbatim — and a body template can hold a literal credential, since
    operations are not encrypted at rest and the cURL importer copies whatever
    the pasted example carried."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.example.com":
            return httpx.Response(307, headers={"location": "https://evil.example.net/collect"})
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="login",
        method="POST",
        path="/login",
        parameters=[{"name": "user", "location": "body", "type": "string", "required": True}],
        body='{"user": {user}, "client_secret": "sk-live-abcdef123456"}',
    )
    tool = GenericApiTool(_connector(), op, timeout_seconds=5)

    result = await tool.execute(user="ada")

    assert [r.url.host for r in seen] == ["api.example.com"]
    assert "blocked" in result
    assert "sk-live-abcdef123456" not in result


async def test_308_same_origin_redirect_still_replays_the_body(monkeypatch):
    """The cross-origin guard must not break ordinary same-origin 308s, which
    are exactly how an API signals a moved endpoint."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/login":
            return httpx.Response(308, headers={"location": "/v2/login"})
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="login",
        method="POST",
        path="/login",
        parameters=[{"name": "user", "location": "body", "type": "string", "required": True}],
        body='{"user": {user}}',
    )
    tool = GenericApiTool(_connector(), op, timeout_seconds=5)

    result = await tool.execute(user="ada")

    assert [r.url.path for r in seen] == ["/login", "/v2/login"]
    assert seen[1].method == "POST"
    assert json.loads(seen[1].read()) == {"user": "ada"}
    assert "[200]" in result


async def test_accept_encoding_from_env_is_not_replayed(monkeypatch):
    """A devtools cURL paste advertises "br, zstd". httpx only decodes what its
    optional codec deps provide and otherwise falls back to identity, so
    replaying that header returns a 200 full of compressed bytes."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["accept_encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    connector = _connector(env={"HEADER_Accept-Encoding": "br, zstd"})
    tool = GenericApiTool(connector, _operation(), timeout_seconds=5)

    await tool.execute(id="42")

    assert "zstd" not in (captured["accept_encoding"] or "")


async def test_benign_imported_env_values_are_not_blanked_from_the_response(monkeypatch):
    """The cURL importer files every pasted header/query into env, so env is
    full of non-secrets. Redacting all of them corrupts real response data
    before the model ever reads it: QUERY_status=active would blank the word
    "active" out of every response the connector returns."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"status": "active", "accept": "application/json"}')

    _mock_client(monkeypatch, handler)
    connector = _connector(
        env={
            "QUERY_status": "active",
            "HEADER_Accept": "application/json",
            "HEADER_X-Api-Key": "sk-live-abcdef123456",
        }
    )
    tool = GenericApiTool(connector, _operation(), timeout_seconds=5)

    result = await tool.execute(id="42")

    assert '"status": "active"' in result
    assert '"accept": "application/json"' in result


async def test_credential_shaped_env_values_are_still_redacted(monkeypatch):
    """The other direction: an oddly named key still gets scrubbed when the
    value itself is token-shaped, and a short credential still gets scrubbed
    when the key names one."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=request.headers.get("x-tenant", "") + " " + request.url.query.decode())

    _mock_client(monkeypatch, handler)
    connector = _connector(
        env={"QUERY_k": "sk_live_0123456789abcdef", "HEADER_X-Tenant": "hunter2", "HEADER_X-Tenant-Password": "hunter2"}
    )
    tool = GenericApiTool(connector, _operation(), timeout_seconds=5)

    result = await tool.execute(id="42")

    assert "sk_live_0123456789abcdef" not in result
    assert "hunter2" not in result


async def test_nested_and_numeric_body_credentials_are_redacted(monkeypatch):
    """_body_secrets walks the parsed template, so a credential nested inside
    an object — or stored as a JSON number — is caught too."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=request.content.decode())

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="login",
        method="POST",
        path="/login",
        parameters=[{"name": "user", "location": "body", "type": "string", "required": True}],
        body='{"user": {user}, "auth": {"key": "SECRETVALUE12345"}, "token": 1234567890}',
    )
    tool = GenericApiTool(_connector(operations=[op]), op, timeout_seconds=5)

    result = await tool.execute(user="ada")

    assert "SECRETVALUE12345" not in result
    assert "1234567890" not in result
    assert "ada" in result


async def test_body_fields_that_merely_look_like_credentials_are_left_alone(monkeypatch):
    """Substring key matching blanked "author" (matches "auth") and
    "token_count" (matches "token") out of every echoed response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=request.content.decode())

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="publish",
        method="POST",
        path="/posts",
        parameters=[{"name": "title", "location": "body", "type": "string", "required": True}],
        body='{"title": {title}, "author": "Ada Lovelace", "token_count": "1024 tokens"}',
    )
    tool = GenericApiTool(_connector(operations=[op]), op, timeout_seconds=5)

    result = await tool.execute(title="Notes")

    assert "Ada Lovelace" in result
    assert "1024 tokens" in result


def test_query_credential_is_masked_in_a_url_whatever_its_name_or_shape():
    """The leak this exists to stop: a QUERY_* value lands in the request URL,
    which an httpx/mcp exception message quotes verbatim. Nothing about
    "pass=letmein" is guessable from the key word list or the value shape, so
    the stored parameter *name* is what does the masking."""
    connector = _connector(env={"QUERY_pass": "letmein"})

    text = _redact_secrets(
        "Connect error: GET https://api.example.com/data?pass=letmein&page=2", connector
    )

    assert "letmein" not in text
    assert "pass=***" in text
    assert "page=2" in text


def test_short_query_credential_is_still_masked_in_the_url():
    """The _MIN_REDACTABLE_LEN floor exists so a global replace of "10" can't
    shred surrounding text — it must not also skip the position-anchored URL
    mask, which has no such collateral damage. A 3-character PIN is exactly as
    much of a credential as a 30-character token."""
    connector = _connector(env={"QUERY_pin": "123"})

    text = _redact_secrets("Connect error: GET https://api.example.com/d?pin=123&page=2", connector)

    assert "pin=***" in text
    assert "pin=123" not in text
    assert "page=2" in text


def test_short_query_credential_is_not_replaced_globally():
    """...while the floor still does its job everywhere else: blanking every
    "123" in a response body would corrupt unrelated data."""
    connector = _connector(env={"QUERY_pin": "123"})

    text = _redact_secrets('{"total": 123, "id": "a123b"}', connector)

    assert text == '{"total": 123, "id": "a123b"}'


def test_oddly_named_header_credential_is_redacted_by_default():
    """HEADER_* is default-deny: an auth scheme nobody's word list predicts
    (X-Auth-Email, pw, a bare tenant token) must not be the one that leaks."""
    connector = _connector(
        env={"HEADER_X-Auth-Email": "ops@corp.example", "HEADER_pw": "s3kr1t"}
    )

    text = _redact_secrets("401 for ops@corp.example / s3kr1t", connector)

    assert "ops@corp.example" not in text
    assert "s3kr1t" not in text


def test_unprefixed_process_env_is_always_redacted():
    """A stdio MCP server's env is process config, never response data — so
    there is no cost to redacting it and a real cost to guessing wrong."""
    connector = _connector(env={"PGPASSWORD": "tr0ub4dor", "DATABASE_URL": "postgres://u:p@h/db"})

    text = _redact_secrets("psql: FATAL for postgres://u:p@h/db with tr0ub4dor", connector)

    assert "tr0ub4dor" not in text
    assert "postgres://u:p@h/db" not in text


async def test_small_numeric_body_literals_do_not_shred_the_response(monkeypatch):
    """A number is only a credential if it's long. Treating "1" as one turns
    every 1 in the response — including the "[200] " status prefix — into
    "***", handing the model unparseable JSON on every successful call."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"id": 10023, "count": 15}')

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="create",
        method="POST",
        path="/things",
        parameters=[{"name": "user", "location": "body", "type": "string", "required": True}],
        body='{"user": {user}, "private": 1, "access": 2}',
    )
    tool = GenericApiTool(_connector(operations=[op]), op, timeout_seconds=5)

    result = await tool.execute(user="ada")

    assert "[200]" in result
    assert '{"id": 10023, "count": 15}' in result


async def test_slug_shaped_body_literals_are_not_treated_as_tokens(monkeypatch):
    """"in_progress_review" clears a length+charset token test (16+ chars of
    [A-Za-z0-9_-]) but is ordinary data — blanking it corrupts every response
    that echoes the field back."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text='{"id": 7, "status": "in_progress_review", "project": "acme-website-redesign"}'
        )

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="create",
        method="POST",
        path="/tickets",
        parameters=[{"name": "title", "location": "body", "type": "string", "required": True}],
        body='{"title": {title}, "status": "in_progress_review", "project": "acme-website-redesign"}',
    )
    tool = GenericApiTool(_connector(operations=[op]), op, timeout_seconds=5)

    result = await tool.execute(title="Notes")

    assert "in_progress_review" in result
    assert "acme-website-redesign" in result


async def test_connector_ref_is_read_fresh_on_every_call(monkeypatch):
    """An admin-global connector's row can be edited (credential rotated, base
    url repointed) while tools built from the OLD row are still registered in
    another user's registry — they're only rebuilt on that user's next
    sync_tools. Without the indirection the tool keeps using the revoked
    credential until then."""
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((str(request.url), request.headers.get("x-api-key")))
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    live = {"row": _connector(url="https://old.example.com", env={"HEADER_X-Api-Key": "OLDKEY"})}
    tool = GenericApiTool(
        live["row"], _operation(), timeout_seconds=5, connector_ref=lambda: live["row"]
    )

    await tool.execute(id="1")
    live["row"] = _connector(url="https://new.example.com", env={"HEADER_X-Api-Key": "NEWKEY"})
    await tool.execute(id="2")

    assert captured[0] == ("https://old.example.com/users/1", "OLDKEY")
    assert captured[1] == ("https://new.example.com/users/2", "NEWKEY")


async def test_connector_ref_going_none_stops_the_tool_calling_out(monkeypatch):
    """Disabling or deleting a global connector must take effect immediately,
    not on each user's next sync — an MCP proxy fails safe here because its
    session is closed, but this tool has no session at all."""
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5, connector_ref=lambda: None)

    result = await tool.execute(id="1")

    assert called == []
    assert "no longer available" in result


async def test_per_user_tool_without_a_ref_still_uses_its_captured_row(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    assert "[200]" in await tool.execute(id="1")


async def test_a_slow_drip_cannot_outlive_the_timeout(monkeypatch):
    """httpx's `timeout=` is per phase and its read timeout restarts on every
    chunk, so a server drip-feeding bytes can hold a chat turn open far past
    the nominal setting. asyncio.timeout is what bounds the whole call."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, text="too late")

    _mock_client(monkeypatch, handler)
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=0.05)

    result = await asyncio.wait_for(tool.execute(id="1"), timeout=2)

    assert "timed out after 0.05s" in result


async def test_lowercase_content_type_env_header_is_not_duplicated(monkeypatch):
    """setdefault compares case-sensitively, so an imported
    "HEADER_content-type" would leave the dict holding both keys — and httpx
    sends both."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get_list("content-type")
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="create",
        method="POST",
        path="/things",
        parameters=[{"name": "title", "location": "body", "type": "string", "required": True}],
        body='{"title": {title}}',
    )
    connector = _connector(env={"HEADER_content-type": "application/vnd.api+json"})
    tool = GenericApiTool(connector, op, timeout_seconds=5)

    await tool.execute(title="hi")

    assert captured["content_type"] == ["application/vnd.api+json"]


async def test_303_downgrade_drops_a_lowercase_content_type_header(monkeypatch):
    """The body is dropped on a 303 → GET downgrade, so a surviving
    Content-Type describes a body that no longer exists."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get_list("content-type")))
        if request.url.path == "/things":
            return httpx.Response(303, headers={"location": "/things/1"})
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    op = _operation(
        name="create",
        method="POST",
        path="/things",
        parameters=[{"name": "title", "location": "body", "type": "string", "required": True}],
        body='{"title": {title}}',
    )
    connector = _connector(env={"HEADER_content-type": "application/vnd.api+json"})
    tool = GenericApiTool(connector, op, timeout_seconds=5)

    await tool.execute(title="hi")

    assert seen[1][0] == "GET"
    assert seen[1][1] == []


async def test_pinning_falls_back_to_the_next_validated_address(monkeypatch):
    """Pinning removes the resolver's own address fallback, so a dual-stack
    name whose AAAA is unreachable from this host would otherwise be
    permanently broken."""
    tried = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        if request.url.host == "2606:2800:220::1":
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler, resolve=lambda url: ["2606:2800:220::1", "93.184.216.34"])
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    result = await tool.execute(id="1")

    assert tried == ["2606:2800:220::1", "93.184.216.34"]
    assert "[200]" in result


async def test_pinning_gives_up_with_the_last_connect_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    _mock_client(monkeypatch, handler, resolve=lambda url: ["93.184.216.34", "93.184.216.35"])
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    result = await tool.execute(id="1")

    assert "failed" in result
    assert "no route to host" in result


async def test_pinned_request_keeps_the_host_header_and_sni_on_the_real_name(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host_header"] = request.headers.get("host")
        captured["dialled"] = request.url.host
        captured["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler, resolve=lambda url: "93.184.216.34")
    tool = GenericApiTool(_connector(), _operation(), timeout_seconds=5)

    await tool.execute(id="1")

    assert captured["dialled"] == "93.184.216.34"
    assert captured["host_header"] == "api.example.com"
    assert captured["sni"] == "api.example.com"


async def test_header_env_keys_differing_only_in_case_send_one_header(monkeypatch):
    # HTTP header names are case-insensitive, so HEADER_authorization and
    # HEADER_Authorization are one header — emitting both let the server pick
    # which credential applied, and the UI showed only one of them as effective.
    # Save-time validation rejects the pair now; a row written before it still
    # has to behave, and behave the same way on every call.
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get_list("authorization"))
        return httpx.Response(200, text="{}")

    _mock_client(monkeypatch, handler)
    connector = _connector(
        env={"HEADER_authorization": "Bearer lower", "HEADER_Authorization": "Bearer upper"}
    )
    tool = GenericApiTool(connector, _operation(), timeout_seconds=5)

    await tool.execute(id="1")
    await tool.execute(id="2")

    assert captured[0] == ["Bearer upper"]  # sorted-key fold: "A" < "a"
    assert captured[1] == captured[0]  # and it is stable across calls


def test_allowlisted_header_holding_a_token_shaped_value_is_still_redacted():
    """_HEADER_ALLOWLIST says a header *name* is normally boring, which is not
    the same as its value being safe to echo — a signed session in a Cookie or
    a JWT wedged into User-Agent is still a credential. The QUERY_* branch
    already pairs the name check with a value-shape check; this is the match."""
    connector = _connector(
        env={
            "HEADER_user-agent": "eyJhbGciOiJIUzI1NiJ9.dGVuYW50.c2ln",
            "HEADER_accept": "application/json",
        }
    )

    text = _redact_secrets(
        "502 from upstream: ua=eyJhbGciOiJIUzI1NiJ9.dGVuYW50.c2ln accept=application/json",
        connector,
    )

    assert "eyJhbGciOiJIUzI1NiJ9" not in text
    # ...while an ordinary allowlisted value keeps passing through, so response
    # bodies aren't shredded around "application/json".
    assert "application/json" in text


def test_a_realistic_user_agent_is_not_mistaken_for_a_token():
    connector = _connector(
        env={"HEADER_user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    )

    text = _redact_secrets("ua: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", connector)

    assert "Mozilla/5.0" in text
