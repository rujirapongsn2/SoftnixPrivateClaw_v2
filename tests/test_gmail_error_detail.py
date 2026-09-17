import httpx

from claw.integrations.gmail_mcp_server import _extract_response_detail as gmail_detail
from claw.integrations.google_sheets_mcp_server import _extract_response_detail as sheets_detail


def _response():
    return httpx.Response(
        400,
        json={
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
            "error_subtype": "invalid_rapt",
        },
    )


def test_gmail_extracts_flat_oauth_token_error():
    assert gmail_detail(_response()) == (
        "invalid_grant; Token has been expired or revoked.; invalid_rapt"
    )


def test_google_sheets_extracts_flat_oauth_token_error():
    assert sheets_detail(_response()) == (
        "invalid_grant; Token has been expired or revoked.; invalid_rapt"
    )
