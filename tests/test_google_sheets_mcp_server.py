"""Google Sheets MCP server client: URL/range building and token self-refresh.

Uses httpx.MockTransport (no live Google API), same pattern as test_oidc.py."""

import pytest
import httpx

from claw.integrations.google_sheets_mcp_server import GoogleSheetsClient, _extract_spreadsheet_id


def _client(handler, **kwargs) -> GoogleSheetsClient:
    return GoogleSheetsClient(token="at", transport=httpx.MockTransport(handler), **kwargs)


def _write_scope_handler(inner):
    """Wraps a handler so oauth2.googleapis.com/tokeninfo (called by
    ensure_write_scope before any mutating request) reports the spreadsheets
    write scope, and everything else is delegated to `inner`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"scope": "https://www.googleapis.com/auth/spreadsheets"})
        return inner(request)

    return handler


def test_extract_spreadsheet_id_from_full_sheets_url_with_gid_suffix():
    url = (
        "https://docs.google.com/spreadsheets/d/1Mh5MJOEwBnYULLEeyVpT6udlTduPPliC/edit"
        "?gid=1174646745#gid=1174646745"
    )
    assert _extract_spreadsheet_id(url) == "1Mh5MJOEwBnYULLEeyVpT6udlTduPPliC"


def test_extract_spreadsheet_id_handles_multi_account_url_variant():
    # Google inserts /u/<n>/ when more than one account is signed into the browser.
    url = "https://docs.google.com/spreadsheets/u/0/d/1AbCDEF12345/edit#gid=0"
    assert _extract_spreadsheet_id(url) == "1AbCDEF12345"


def test_extract_spreadsheet_id_passes_through_a_raw_id_unchanged():
    assert _extract_spreadsheet_id("SPREADSHEET_ID") == "SPREADSHEET_ID"


def test_extract_spreadsheet_id_rejects_empty_value():
    with pytest.raises(ValueError, match="required"):
        _extract_spreadsheet_id("   ")


def test_extract_spreadsheet_id_rejects_publish_to_web_link():
    url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vT.../pubhtml"
    with pytest.raises(ValueError, match="Publish to the web"):
        _extract_spreadsheet_id(url)


def test_extract_spreadsheet_id_rejects_unparseable_url():
    with pytest.raises(ValueError, match="Could not find a spreadsheet ID"):
        _extract_spreadsheet_id("https://example.com/not-a-sheets-link")


def test_read_range_accepts_a_full_sheets_url_as_spreadsheet_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"values": [["a", "b"]]})

    url = "https://docs.google.com/spreadsheets/d/SID_FROM_URL/edit?gid=0#gid=0"
    result = _client(handler).read_range(url, "Sheet1!A1:B2")

    assert result == {"values": [["a", "b"]]}
    assert "/SID_FROM_URL/values/Sheet1!A1:B2" in seen["url"]


def test_read_range_hits_values_endpoint_with_a1_range():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"values": [["a", "b"]]})

    result = _client(handler).read_range("SPREADSHEET_ID", "Sheet1!A1:B2")

    assert result == {"values": [["a", "b"]]}
    assert "/SPREADSHEET_ID/values/Sheet1!A1:B2" in seen["url"]
    assert seen["auth"] == "Bearer at"


def test_append_row_posts_to_append_endpoint_with_value_input_option():
    seen = {}

    def inner(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"updates": {"updatedRows": 1}})

    result = _client(_write_scope_handler(inner)).append_row("SID", "Sheet1!A:A", [["x"]])

    assert result == {"updates": {"updatedRows": 1}}
    assert seen["method"] == "POST"
    assert "/SID/values/Sheet1!A:A:append" in seen["url"]
    assert "valueInputOption=USER_ENTERED" in seen["url"]


def test_range_containing_hash_is_percent_encoded_not_truncated():
    # '#' is legal in a Sheets tab name but is a URL fragment delimiter — if it
    # isn't percent-encoded before being sent, everything after it (including
    # the rest of the range) is silently dropped by URL parsing.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"values": [["a"]]})

    _client(handler).read_range("SID", "Notes#1!A1:C10")

    assert "Notes%231!A1:C10" in seen["url"]
    assert "/values/Notes" not in seen["url"].split("Notes%231")[0]


def test_write_call_raises_when_token_lacks_write_scope():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"scope": "https://www.googleapis.com/auth/spreadsheets.readonly"})
        raise AssertionError("the write request should not be sent if the scope check fails")

    with pytest.raises(ValueError, match="write scope"):
        _client(handler).update_range("SID", "Sheet1!A1", [["x"]])


def test_create_spreadsheet_posts_to_bare_collection_endpoint():
    seen = {}

    def inner(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"spreadsheetId": "NEW_ID"})

    result = _client(_write_scope_handler(inner)).create_spreadsheet("My Sheet")

    # No trailing slash / id segment on the create call.
    assert seen["url"] == "https://sheets.googleapis.com/v4/spreadsheets"
    assert result["spreadsheetId"] == "NEW_ID"


def test_list_sheets_summarizes_tabs_from_spreadsheet_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "spreadsheetId": "SID",
                "properties": {"title": "My Sheet"},
                "sheets": [
                    {
                        "properties": {
                            "sheetId": 0,
                            "title": "Sheet1",
                            "index": 0,
                            "gridProperties": {"rowCount": 100, "columnCount": 26},
                        }
                    }
                ],
            },
        )

    result = _client(handler).list_sheets("SID")

    assert result["title"] == "My Sheet"
    assert result["sheets"] == [
        {"sheetId": 0, "title": "Sheet1", "index": 0, "rowCount": 100, "columnCount": 26}
    ]


def test_401_triggers_refresh_then_retries_original_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "new-token"})
        if request.headers.get("authorization") == "Bearer at":
            return httpx.Response(401, json={"error": {"message": "invalid token"}})
        return httpx.Response(200, json={"values": [["ok"]]})

    result = _client(
        handler, refresh_token="rt", client_id="cid"
    ).read_range("SID", "Sheet1!A1")

    assert result == {"values": [["ok"]]}
    # First call fails (401), then a refresh call, then a retry that succeeds.
    assert len(calls) == 3


def test_error_detail_from_google_api_error_body_is_included_in_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Unable to parse range: Bogus!A1"}})

    try:
        _client(handler).read_range("SID", "Bogus!A1")
        raise AssertionError("expected HTTPStatusError")
    except httpx.HTTPStatusError as exc:
        assert "Unable to parse range: Bogus!A1" in str(exc)


def test_error_detail_includes_structured_errors_array_reason_and_location():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid requests",
                    "errors": [{"reason": "badRequest", "location": "range"}],
                }
            },
        )

    try:
        _client(handler).read_range("SID", "Bogus!A1")
        raise AssertionError("expected HTTPStatusError")
    except httpx.HTTPStatusError as exc:
        assert "badRequest at range" in str(exc)
