from claw.providers.litellm_provider import LiteLLMProvider


def test_no_env_key_leak_to_keyless_custom_provider():
    """A DB-configured provider (api_base set, no api_key) must not receive
    the operator's global env key — it gets the local placeholder instead."""
    provider = LiteLLMProvider(api_key="sk-env-global-secret", api_base="")
    key, base = provider._effective_credentials(None, "http://localhost:8000/v1")
    assert key == "sk-local"
    assert base == "http://localhost:8000/v1"


def test_per_call_key_wins_over_placeholder():
    provider = LiteLLMProvider(api_key="sk-env-global-secret", api_base="")
    key, base = provider._effective_credentials("sk-provider-own-key", "http://localhost:8000/v1")
    assert key == "sk-provider-own-key"
    assert base == "http://localhost:8000/v1"


def test_no_api_base_override_still_uses_env_defaults():
    """No per-call api_base (e.g. env-default model) keeps old behavior."""
    provider = LiteLLMProvider(api_key="sk-env-global-secret", api_base="https://env-base.example.com")
    key, base = provider._effective_credentials(None, None)
    assert key == "sk-env-global-secret"
    assert base == "https://env-base.example.com"
