"""Provider registry model metadata (claw/providers/registry.py)."""

from claw.providers.registry import context_window, supports_vision


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


def test_context_window_strips_routing_prefixes():
    # The production id: LiteLLM's table is keyed by the underlying model, so
    # the gateway prefix has to come off before the lookup hits.
    assert context_window("openrouter/anthropic/claude-sonnet-5") == context_window("claude-sonnet-5")
    assert context_window("gpt-4") == 8192


def test_context_window_is_none_for_unknown_models():
    # Never a guess: callers size a context budget off this and must be able to
    # tell "unknown" apart from a real window.
    assert context_window("some-private-gateway/internal-model-v9") is None
    assert context_window("") is None
    assert context_window(None) is None
