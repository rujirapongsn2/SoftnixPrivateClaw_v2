"""Settings validators — currently just the web_base_url localhost fallback
that keeps emailed activation/reset links from pointing at localhost when an
operator sets CLAW_PUBLIC_BASE_URL but forgets CLAW_WEB_BASE_URL."""

from claw.config import Settings


def _settings(**kw) -> Settings:
    return Settings(secret_key="s", _env_file=None, **kw)


def test_web_base_url_falls_back_to_public_from_vite_dev_default():
    s = _settings(public_base_url="https://claw.example.com", web_base_url="http://localhost:5173")
    assert s.web_base_url == "https://claw.example.com"


def test_web_base_url_falls_back_to_public_from_api_own_port_default():
    s = _settings(public_base_url="https://claw.example.com", web_base_url="http://localhost:8700")
    assert s.web_base_url == "https://claw.example.com"


def test_web_base_url_untouched_when_public_base_url_still_default():
    # Both still at their dev defaults (Option C: API on :8700, Vite on
    # :5173) — no operator has pointed public_base_url at a real domain yet,
    # so there's nothing to fall back to; must stay the dev default.
    s = _settings(web_base_url="http://localhost:5173")
    assert s.web_base_url == "http://localhost:5173"


def test_web_base_url_explicit_value_not_overridden():
    # An operator who serves the frontend from a different host than the
    # API sets CLAW_WEB_BASE_URL explicitly — that must win over the fallback.
    s = _settings(public_base_url="https://api.example.com", web_base_url="https://app.example.com")
    assert s.web_base_url == "https://app.example.com"
