"""Provider registry vision-capability metadata (claw/providers/registry.py)."""

from claw.providers.registry import supports_vision


def test_deepseek_models_do_not_support_vision():
    assert supports_vision("deepseek-chat") is False
    assert supports_vision("deepseek/deepseek-r1") is False


def test_text_only_model_behind_openai_compatible_gateway_is_detected():
    # The real production id: routed through an OpenAI-compatible gateway, so
    # find_spec() resolves it to the "openai" spec. Vision capability must
    # follow the underlying model, not the routing prefix.
    assert supports_vision("openai/deepseek-v4-flash") is False


def test_vision_checkpoint_inside_text_only_family_is_allowed():
    assert supports_vision("deepseek/deepseek-vl-7b") is True


def test_unmatched_model_defaults_to_supporting_vision():
    # Permissive default: an unmatched model is unknown, not confirmed
    # text-only, so it must not be wrongly blocked by the proactive check.
    assert supports_vision("some-brand-new-model") is True


def test_known_vision_capable_families_are_not_blocked():
    assert supports_vision("anthropic/claude-3-5-sonnet") is True
    assert supports_vision("gemini/gemini-1.5-pro") is True
