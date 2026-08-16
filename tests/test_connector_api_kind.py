"""Generic REST "api" connector kind: DB round-trip and backend validation.

kind="api" reuses the same mcp_connectors table/store as every MCP connector
(see claw/db/models.py's McpConnector docstring) — these tests cover the
parts unique to that reuse: how `operations` passes through secret_box,
`kind="mcp"` rows staying unaffected, and the validation rules in
claw/api/connector_shared.py::upsert_connector's api-kind branch.
"""

from tests.conftest_app import build_api_app, client


async def _register(c, email, password="password123"):
    r = await c.post("/api/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"], r.json()["user"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _valid_api_body(**overrides):
    body = {
        "name": "myapi",
        "kind": "api",
        "url": "https://api.example.com",
        "operations": [
            {
                "name": "get_user",
                "method": "GET",
                "path": "/users/{id}",
                "description": "Fetch a user by id",
                "parameters": [
                    {"name": "id", "location": "path", "type": "string", "required": True},
                ],
            }
        ],
    }
    body.update(overrides)
    return body


async def test_store_round_trip_encrypts_only_the_operation_body(db_factory):
    from claw.db.stores import ConnectorStore, UserStore
    from claw.security.crypto import SecretBox

    users = UserStore(db_factory)
    user = await users.create(email="apikind@x.io", password_hash="h")
    store = ConnectorStore(db_factory, secret_box=SecretBox("test-secret-key"))

    operations = [
        {
            "name": "get_user",
            "method": "GET",
            "path": "/users/{id}",
            "description": "",
            "parameters": [{"name": "id", "location": "path", "type": "string", "required": True}],
        },
        {
            "name": "create_user",
            "method": "POST",
            "path": "/users",
            "description": "",
            "parameters": [{"name": "name", "location": "body", "type": "string", "required": True}],
            # A cURL-imported body routinely carries a literal credential.
            "body": '{"name": {name}, "api_key": "sk-live-abc123"}',
        },
    ]
    row = await store.upsert(
        user.id,
        "myapi",
        kind="api",
        transport="http",
        command="",
        url="https://api.example.com",
        env={"HEADER_Authorization": "Bearer secret123"},
        operations=operations,
        enabled=True,
    )
    assert row.kind == "api"
    # Callers see plaintext for both env and the body template.
    assert row.operations == operations
    assert row.env == {"HEADER_Authorization": "Bearer secret123"}

    async with db_factory() as db:
        from claw.db.models import McpConnector

        raw = await db.get(McpConnector, row.id)
        assert raw.env != {"HEADER_Authorization": "Bearer secret123"}
        # The body template is encrypted at rest, so the literal credential
        # it carries never sits in the DB in cleartext...
        assert "sk-live-abc123" not in raw.operations[1]["body"]
        assert raw.operations[1]["body"].startswith("enc::")
        # ...while everything the admin UI and the tool-name collision check
        # read stays plain JSON.
        assert raw.operations[0] == operations[0]
        assert raw.operations[1]["name"] == "create_user"
        assert raw.operations[1]["parameters"] == operations[1]["parameters"]

    assert (await store.get_by_name(user.id, "myapi")).operations == operations


async def test_operation_body_written_before_encryption_still_reads(db_factory):
    """A row stored when bodies were plaintext must keep working — SecretBox's
    prefix marker is what makes this migration-free."""
    from claw.db.models import McpConnector
    from claw.db.stores import ConnectorStore, UserStore
    from claw.security.crypto import SecretBox

    users = UserStore(db_factory)
    user = await users.create(email="legacybody@x.io", password_hash="h")
    legacy_ops = [{"name": "post", "method": "POST", "path": "/p", "body": '{"k": "plain"}'}]
    async with db_factory() as db:
        db.add(
            McpConnector(
                owner_id=user.id,
                name="legacy",
                kind="api",
                transport="http",
                url="https://api.example.com",
                env={},
                operations=legacy_ops,
                enabled=True,
            )
        )
        await db.commit()

    store = ConnectorStore(db_factory, secret_box=SecretBox("test-secret-key"))
    assert (await store.get_by_name(user.id, "legacy")).operations == legacy_ops


async def test_store_round_trip_kind_mcp_unaffected(db_factory):
    from claw.db.stores import ConnectorStore, UserStore

    users = UserStore(db_factory)
    user = await users.create(email="mcpkind@x.io", password_hash="h")
    store = ConnectorStore(db_factory)

    row = await store.upsert(
        user.id, "mymcp", transport="http", url="https://mcp.example.com/mcp", enabled=True
    )
    assert row.kind == "mcp"  # server default, no kind passed
    assert row.operations is None


async def test_api_connector_requires_url(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u1@x.io")
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(url=""),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_malformed_url_is_a_422_not_a_500(db_factory):
    """`url` is a plain str field, so nothing upstream rejects a syntactically
    broken URL; urlsplit then raises a bare ValueError (not UnsafeUrlError) on
    an unterminated IPv6 literal, which would escape as a 500."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u1b@x.io")
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(url="http://[::1"),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_requires_at_least_one_operation(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u2@x.io")
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_duplicate_operation_names(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u3@x.io")
        op = _valid_api_body()["operations"][0]
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op, {**op, "method": "POST"}]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_invalid_method(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u4@x.io")
        op = {**_valid_api_body()["operations"][0], "method": "TRACE"}
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_path_missing_parameter(db_factory):
    """A {placeholder} in the path with no matching declared parameter."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u5@x.io")
        op = {**_valid_api_body()["operations"][0], "parameters": []}
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_declared_parameter_missing_from_path(db_factory):
    """A declared path parameter with no matching {placeholder} in the path."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u6@x.io")
        op = {
            "name": "list_users",
            "method": "GET",
            "path": "/users",
            "description": "",
            "parameters": [{"name": "id", "location": "path", "type": "string", "required": True}],
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_absolute_url_path(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u7@x.io")
        op = {
            "name": "escape",
            "method": "GET",
            "path": "https://evil.invalid/steal",
            "description": "",
            "parameters": [],
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_path_not_starting_with_slash(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u8@x.io")
        op = {"name": "bad", "method": "GET", "path": "users", "description": "", "parameters": []}
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_over_operations_cap(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u9@x.io")
        ops = [
            {"name": f"op{i}", "method": "GET", "path": "/x", "description": "", "parameters": []}
            for i in range(51)
        ]
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=ops),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_valid_minimal_operation_saves_with_command_cleared(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u10@x.io")
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(),
            headers=_bearer(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "api"
        assert body["command"] == ""
        assert body["url"] == "https://api.example.com"
        assert len(body["operations"]) == 1
        assert body["operations"][0]["name"] == "get_user"


async def test_mcp_connector_operations_force_cleared_server_side(db_factory):
    """Defense in depth: even if a caller posts `operations` alongside
    kind="mcp", the stored row's operations must stay None."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u11@x.io")
        r = await c.put(
            "/api/connectors/mymcp",
            json={
                "name": "mymcp",
                "kind": "mcp",
                "transport": "http",
                "url": "https://mcp.example.com/mcp",
                "operations": [
                    {"name": "sneaky", "method": "GET", "path": "/x", "description": "", "parameters": []}
                ],
            },
            headers=_bearer(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["operations"] == []


async def test_api_connector_rejects_optional_path_parameter(db_factory):
    """A path parameter must be required=True — a missing value silently
    substitutes as "" and hits the wrong URL (see claw/tools/api.py)."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u12@x.io")
        op = {
            **_valid_api_body()["operations"][0],
            "parameters": [{"name": "id", "location": "path", "type": "string", "required": False}],
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422
        assert "must be required" in r.text


async def test_api_connector_rejects_duplicate_parameter_names(db_factory):
    """Same parameter name declared twice within one operation, even across
    different locations, would collide in the JSON schema and get sent to
    both places at call time."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u13@x.io")
        op = {
            **_valid_api_body()["operations"][0],
            "parameters": [
                {"name": "id", "location": "path", "type": "string", "required": True},
                {"name": "id", "location": "query", "type": "string", "required": False},
            ],
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422
        assert "duplicate parameter" in r.text


async def test_connector_kind_cannot_be_changed_after_creation(db_factory):
    """Only the frontend UI hides the kind toggle after creation — the
    backend must reject a kind flip too, or existing tool references (skills,
    tool_args_exempt globs) to the old mcp_*/api_* tool names silently break."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u14@x.io")
        r = await c.put(
            "/api/connectors/flipme",
            json={"name": "flipme", "kind": "mcp", "transport": "http", "url": "https://mcp.example.com/mcp"},
            headers=_bearer(token),
        )
        assert r.status_code == 200, r.text

        r = await c.put(
            "/api/connectors/flipme",
            json=_valid_api_body(name="flipme"),
            headers=_bearer(token),
        )
        assert r.status_code == 422
        assert "cannot be changed" in r.text


async def test_connector_kind_unchanged_resave_still_allowed(db_factory):
    """Re-saving a connector with the same kind (e.g. editing its
    description) must not trip the new kind-lock check."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u15@x.io")
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(),
            headers=_bearer(token),
        )
        assert r.status_code == 200, r.text

        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(description="updated"),
            headers=_bearer(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["description"] == "updated"


async def test_kind_omitted_on_edit_inherits_existing_kind(db_factory):
    """A caller that never sends `kind` (a client predating the field, or any
    edit that simply doesn't care about it) must not be rejected by the kind
    lock — an omitted kind means "leave this connector as whatever it is"."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u16@x.io")
        r = await c.put("/api/connectors/myapi", json=_valid_api_body(), headers=_bearer(token))
        assert r.status_code == 200, r.text

        body = _valid_api_body(description="updated")
        body.pop("kind")
        r = await c.put("/api/connectors/myapi", json=body, headers=_bearer(token))
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "api"
        assert r.json()["description"] == "updated"


async def test_kind_omitted_on_create_still_defaults_to_mcp(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "u17@x.io")
        r = await c.put(
            "/api/connectors/plainmcp",
            json={"name": "plainmcp", "transport": "http", "url": "https://mcp.example.com/mcp"},
            headers=_bearer(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "mcp"


async def test_store_upsert_rejects_kind_change_in_transaction(db_factory):
    """The authoritative kind lock lives in the store, inside the write
    transaction — a separate read-then-write check in the API layer could be
    raced by a concurrent writer."""
    import pytest

    from claw.db.stores import ConnectorKindMismatch, ConnectorStore, UserStore

    users = UserStore(db_factory)
    user = await users.create(email="storekind@x.io", password_hash="h")
    store = ConnectorStore(db_factory)

    await store.upsert(user.id, "c1", kind="api", transport="http", url="https://a.example.com",
                       operations=[{"name": "op", "method": "GET", "path": "/x", "description": "",
                                    "parameters": []}], enabled=True)

    with pytest.raises(ConnectorKindMismatch):
        await store.upsert(user.id, "c1", kind="mcp", transport="http", url="https://b.example.com")

    # An omitted kind is still "leave untouched", so unrelated callers
    # (connector_oauth.py's token refresh) keep working.
    row = await store.upsert(user.id, "c1", url="https://c.example.com")
    assert row.kind == "api"
    assert row.url == "https://c.example.com"


async def test_api_connector_accepts_valid_json_body_template(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "body1@x.io")
        op = {
            "name": "create_user",
            "method": "POST",
            "path": "/users",
            "description": "",
            "parameters": [
                {"name": "name", "location": "body", "type": "string", "required": True},
                {"name": "age", "location": "body", "type": "number", "required": True},
            ],
            "body": '{"name": {name}, "age": {age}}',
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["operations"][0]["body"] == '{"name": {name}, "age": {age}}'


async def test_api_connector_rejects_body_on_get(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "body2@x.io")
        op = {
            "name": "list_users",
            "method": "GET",
            "path": "/users",
            "description": "",
            "parameters": [],
            "body": '{"x": 1}',
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_body_placeholder_missing_parameter(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "body3@x.io")
        op = {
            "name": "create_user",
            "method": "POST",
            "path": "/users",
            "description": "",
            "parameters": [],
            "body": '{"name": {name}}',
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_declared_body_parameter_missing_from_template(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "body4@x.io")
        op = {
            "name": "create_user",
            "method": "POST",
            "path": "/users",
            "description": "",
            "parameters": [{"name": "name", "location": "body", "type": "string", "required": True}],
            "body": "{}",
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_optional_body_parameter(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "body5@x.io")
        op = {
            "name": "create_user",
            "method": "POST",
            "path": "/users",
            "description": "",
            "parameters": [{"name": "name", "location": "body", "type": "string", "required": False}],
            "body": "{\"name\": {name}}",
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_invalid_json_body_template(db_factory):
    """The template's surrounding structure (outside {placeholder} tokens)
    must itself be valid JSON once placeholders are filled with dummy values."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "body6@x.io")
        op = {
            "name": "create_user",
            "method": "POST",
            "path": "/users",
            "description": "",
            "parameters": [{"name": "name", "location": "body", "type": "string", "required": True}],
            "body": '{"name": {name}',  # missing closing brace
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_body_optional_when_empty(db_factory):
    """A POST operation with no body template at all is still valid — body
    support is opt-in per operation."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "body7@x.io")
        op = {
            "name": "ping",
            "method": "POST",
            "path": "/ping",
            "description": "",
            "parameters": [],
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["operations"][0]["body"] == ""


async def test_api_connector_rejects_a_tool_name_over_the_provider_limit(db_factory):
    """Every mainstream provider rejects the entire request when any tool name
    exceeds 64 characters — so an over-long connector+operation pair would break
    every turn for that user, not just this one tool."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "toolname1@x.io")
        long_name = "c" * 40
        op = dict(_valid_api_body()["operations"][0], name="o" * 30)
        r = await c.put(
            f"/api/connectors/{long_name}",
            json=_valid_api_body(name=long_name, operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422
        assert "64" in r.text


async def test_api_connector_accepts_a_tool_name_exactly_at_the_limit(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "toolname2@x.io")
        # len("api_") + 30 + len("_") + 29 == 64
        name = "c" * 30
        op = dict(_valid_api_body()["operations"][0], name="o" * 29)
        r = await c.put(
            f"/api/connectors/{name}",
            json=_valid_api_body(name=name, operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 200, r.text


async def test_api_connector_accepts_hyphenated_header_and_query_parameters(db_factory):
    """Real APIs name headers "X-Api-Key" and query params "api-version"; the
    identifier-only rule made those impossible to declare as per-call
    parameters at all."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "param1@x.io")
        op = {
            "name": "search",
            "method": "GET",
            "path": "/search",
            "description": "",
            "parameters": [
                {"name": "X-Api-Key", "location": "header", "type": "string", "required": True},
                {"name": "api-version", "location": "query", "type": "string", "required": False},
                {"name": "filter.name", "location": "query", "type": "string", "required": False},
            ],
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 200, r.text
        assert [p["name"] for p in r.json()["operations"][0]["parameters"]] == [
            "X-Api-Key",
            "api-version",
            "filter.name",
        ]


async def test_api_connector_still_rejects_a_hyphenated_path_parameter(db_factory):
    """path/body parameters are substituted by an identifier-only placeholder
    regex, so a hyphenated one could never be filled in — it would leave a
    literal "{user-id}" in the URL."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "param2@x.io")
        op = {
            "name": "get_user",
            "method": "GET",
            "path": "/users/{user-id}",
            "description": "",
            "parameters": [
                {"name": "user-id", "location": "path", "type": "string", "required": True},
            ],
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_still_rejects_a_hyphenated_body_parameter(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "param3@x.io")
        op = {
            "name": "create",
            "method": "POST",
            "path": "/things",
            "description": "",
            "parameters": [
                {"name": "display-name", "location": "body", "type": "string", "required": True},
            ],
            "body": '{"display-name": {display-name}}',
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_connector_name_out_of_charset_is_rejected(db_factory):
    """The connector name arrives as a route path parameter, so ConnectorBody's
    name pattern never runs on it. An out-of-charset name would produce a tool
    name every provider rejects — breaking EVERY turn for that user, not just
    this connector's calls."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "badname@x.io")
        for bad in ("My.Api", "my api", "MYAPI", "my+api", "my:api"):
            r = await c.put(
                f"/api/connectors/{bad}",
                json=_valid_api_body(name=bad),
                headers=_bearer(token),
            )
            assert r.status_code == 422, f"{bad!r} should be rejected, got {r.status_code}"


async def test_connector_name_over_the_length_cap_is_rejected(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "longname@x.io")
        long_name = "a" * 65
        r = await c.put(
            f"/api/connectors/{long_name}",
            json=_valid_api_body(name=long_name),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_api_connector_rejects_dot_segments_in_path(db_factory):
    """httpx normalizes dot segments, so a base of https://host/v1/public plus
    "/../../admin" resolves to /admin — escaping the configured prefix the
    operator thought they had pinned the connector to."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "dotdot@x.io")
        for bad_path in ("/../../admin", "/v1/./admin", "/v1/../../etc"):
            op = {
                "name": "escape",
                "method": "GET",
                "path": bad_path,
                "description": "",
                "parameters": [],
            }
            r = await c.put(
                "/api/connectors/myapi",
                json=_valid_api_body(operations=[op]),
                headers=_bearer(token),
            )
            assert r.status_code == 422, f"{bad_path!r} should be rejected"


async def test_api_connector_rejects_query_or_fragment_in_path(db_factory):
    """A "?" or "#" in the stored path smuggles query/fragment past the
    declared parameter model — the caller can't see or control them, and they
    can override credentials the connector's QUERY_* env supplies."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "qsmuggle@x.io")
        for bad_path in ("/users?admin=1", "/users#frag"):
            op = {
                "name": "smuggle",
                "method": "GET",
                "path": bad_path,
                "description": "",
                "parameters": [],
            }
            r = await c.put(
                "/api/connectors/myapi",
                json=_valid_api_body(operations=[op]),
                headers=_bearer(token),
            )
            assert r.status_code == 422, f"{bad_path!r} should be rejected"


async def test_api_connector_rejects_protocol_relative_path(db_factory):
    """"//evil.invalid/x" has no "://" but is still an absolute URL to most
    resolvers."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "protorel@x.io")
        op = {
            "name": "escape",
            "method": "GET",
            "path": "//evil.invalid/steal",
            "description": "",
            "parameters": [],
        }
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(operations=[op]),
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_env_rejects_header_keys_that_differ_only_in_case(db_factory):
    # Both keys name the same HTTP header, so keeping both meant the request
    # carried two Authorization values and the server chose which credential
    # applied — while the UI listed them as two independent settings.
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "hdrcase@x.io")
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(
                env={"HEADER_Authorization": "Bearer a", "HEADER_authorization": "Bearer b"}
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 422
        assert "case-insensitive" in r.text

        # A single spelling, and a QUERY_* pair that only differs in case, are
        # both fine — query strings really are case-sensitive.
        r = await c.put(
            "/api/connectors/myapi",
            json=_valid_api_body(
                env={"HEADER_Authorization": "Bearer a", "QUERY_key": "x", "QUERY_Key": "y"}
            ),
            headers=_bearer(token),
        )
        assert r.status_code == 200
