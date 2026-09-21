from sbot.core.tool_preview import safe_args_preview, safe_text_preview
import pytest


def test_tool_preview_keeps_audit_details_and_redacts_credentials():
    preview = safe_args_preview({
        "url": "https://example.com/source?id=42&token=hidden",
        "headers": {"Authorization": "Bearer also-hidden"},
        "query": "SDLC in the AI era",
    }, 1000)

    assert "https://example.com/source?id=42&token=[redacted]" in preview
    assert "SDLC in the AI era" in preview
    assert "hidden" not in preview
    assert '"Authorization": "[redacted]"' in preview


def test_plain_tool_result_redacts_labelled_secrets_but_keeps_result_context():
    preview = safe_text_preview(
        "Fetched 12 rows from /reports; api_key=hidden; Authorization: Bearer abc.def",
        1000,
    )

    assert preview.startswith("Fetched 12 rows from /reports")
    assert "hidden" not in preview and "abc.def" not in preview
    assert preview.count("[redacted]") == 2


@pytest.mark.parametrize('label', [
    'access_token', 'refresh_token', 'client_secret', 'OPENAI_API_KEY',
    'GOOGLE_APPLICATION_CREDENTIAL', 'user-password',
])
def test_plain_tool_result_redacts_prefixed_and_environment_secret_names(label):
    preview = safe_text_preview(f'command output: {label}="top secret value"; ok', 1000)
    assert 'top secret value' not in preview
    assert f'{label}=[redacted]' in preview
