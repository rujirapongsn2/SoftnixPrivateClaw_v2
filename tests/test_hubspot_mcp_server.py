"""HubSpot MCP server client: request shape, error-detail extraction, and the
create_note associate flow. Uses httpx.MockTransport (no live HubSpot API),
same pattern as test_google_sheets_mcp_server.py."""

import httpx

from claw.integrations.hubspot_mcp_server import HubSpotClient


def _client(handler, **kwargs) -> HubSpotClient:
    return HubSpotClient(token="pat-na1-test", transport=httpx.MockTransport(handler), **kwargs)


def test_get_account_info_hits_expected_endpoint_with_bearer_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"portalId": 123})

    result = _client(handler).get_account_info()

    assert result == {"portalId": 123}
    assert seen["url"].endswith("/account-info/v3/details")
    assert seen["auth"] == "Bearer pat-na1-test"


def test_list_objects_requests_default_properties_for_contacts():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"results": []})

    _client(handler).list_objects("contacts", limit=5)

    assert "/crm/v3/objects/contacts" in seen["url"]
    assert "limit=5" in seen["url"]
    assert "email" in seen["url"]
    assert "firstname" in seen["url"]


def test_list_objects_passes_after_cursor_when_given():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"results": []})

    _client(handler).list_objects("deals", limit=10, after="cursor123")

    assert "after=cursor123" in seen["url"]


def test_get_object_hits_object_by_id_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "42"})

    result = _client(handler).get_object("companies", "42")

    assert result == {"id": "42"}
    assert "/crm/v3/objects/companies/42" in seen["url"]


def test_create_object_posts_properties_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "1", "properties": {"email": "a@b.com"}})

    result = _client(handler).create_object("contacts", {"email": "a@b.com"})

    assert result["id"] == "1"
    assert seen["method"] == "POST"
    assert "/crm/v3/objects/contacts" in seen["url"]
    assert b'"email":"a@b.com"' in seen["body"]


def test_update_object_patches_by_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "1"})

    _client(handler).update_object("deals", "1", {"dealstage": "closedwon"})

    assert seen["method"] == "PATCH"
    assert "/crm/v3/objects/deals/1" in seen["url"]


def test_search_objects_posts_query_and_default_properties():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"results": []})

    _client(handler).search_objects("contacts", "jane", limit=3)

    assert "/crm/v3/objects/contacts/search" in seen["url"]
    assert b'"query":"jane"' in seen["body"]
    assert b'"limit":3' in seen["body"]


def test_search_objects_omits_query_key_when_blank():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"results": []})

    _client(handler).search_objects("contacts")

    assert b"query" not in seen["body"]


def test_associate_default_puts_to_v4_default_association_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    _client(handler).associate_default("notes", "1", "contacts", "2")

    assert seen["method"] == "PUT"
    assert "/crm/v4/objects/notes/1/associations/default/contacts/2" in seen["url"]


def test_create_note_creates_object_then_associates_each_given_id():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "POST" and request.url.path.endswith("/objects/notes"):
            return httpx.Response(200, json={"id": "note-1"})
        return httpx.Response(200, json={})

    result = _client(handler).create_note(
        "call recap", contact_id="c1", company_id="co1", deal_id=None
    )

    assert result["id"] == "note-1"
    assert result["associated_with"] == [
        {"type": "contacts", "id": "c1"},
        {"type": "companies", "id": "co1"},
    ]
    # One create + two associate calls (deal_id was None, so no third associate).
    assert len(calls) == 3
    assert any("associations/default/contacts/c1" in url for _, url in calls)
    assert any("associations/default/companies/co1" in url for _, url in calls)


def test_create_note_skips_association_when_no_ids_given():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"id": "note-1"})

    result = _client(handler).create_note("standalone note")

    assert result["associated_with"] == []
    assert len(calls) == 1


def test_error_detail_from_hubspot_error_body_is_included_in_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Property values were not valid", "category": "VALIDATION_ERROR"})

    try:
        _client(handler).create_object("contacts", {"email": "not-an-email"})
        raise AssertionError("expected HTTPStatusError")
    except httpx.HTTPStatusError as exc:
        assert "Property values were not valid" in str(exc)


def test_error_detail_includes_nested_errors_array_messages():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "message": "Validation error",
                "errors": [{"message": "email must be a valid email address"}],
            },
        )

    try:
        _client(handler).create_object("contacts", {"email": "bad"})
        raise AssertionError("expected HTTPStatusError")
    except httpx.HTTPStatusError as exc:
        assert "email must be a valid email address" in str(exc)


def test_missing_token_raises_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent without a token")

    client = HubSpotClient(token="", transport=httpx.MockTransport(handler))
    try:
        client.get_account_info()
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "token" in str(exc).lower()
