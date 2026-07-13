"""i18n error-message helpers — mainly is_no_tool_support_error(), which
distinguishes a permanent "this model can't do tool calling at all" failure
(retrying is futile) from a transient one (error.llm's generic "try again")."""

from claw.i18n import classify_error_reason, is_no_tool_support_error, t


def test_no_tool_support_error_detected_from_openrouter_message():
    detail = (
        'litellm.NotFoundError: NotFoundError: OpenrouterException - {"error":{"message":'
        '"No endpoints found that support tool use. Try disabling \\"read_file\\".",'
        '"code":404}}'
    )
    assert is_no_tool_support_error(detail) is True


def test_no_tool_support_error_not_confused_with_other_failures():
    assert is_no_tool_support_error("connection timed out") is False
    assert is_no_tool_support_error("401 unauthorized: invalid api key") is False
    assert is_no_tool_support_error("429 rate limit exceeded") is False


def test_llm_no_tool_support_message_does_not_suggest_retrying():
    message = t("error.llm_no_tool_support", "en")
    assert "try again" not in message.lower()
    assert "different model" in message.lower()


def test_classify_error_reason_still_falls_back_to_internal():
    assert classify_error_reason("No endpoints found that support tool use.") == "reason.internal"
