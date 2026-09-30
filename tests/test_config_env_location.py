"""Settings must use the checkout dotenv even when launchd starts elsewhere."""
import pytest


@pytest.mark.parametrize("mode,prefix", [("claw", "CLAW"), ("sbot", "SBOT")])
def test_load_settings_outside_project(monkeypatch, tmp_path, mode, prefix):
    import importlib
    config = importlib.import_module(f"{mode}.config")
    project = tmp_path / "project"
    package = project / mode
    package.mkdir(parents=True)
    (project / ".env").write_text(
        f"{prefix}_SEMANTIC_GUARDRAILS__PROVIDER=jev\n"
        f"{prefix}_SEMANTIC_GUARDRAILS__JEV__API_KEY=test-secret\n"
    )
    other = tmp_path / "launchd"
    other.mkdir()
    (other / ".env").write_text(f"{prefix}_SEMANTIC_GUARDRAILS__PROVIDER=off\n")
    monkeypatch.setattr(config, "__file__", str(package / "config.py"))
    monkeypatch.chdir(other)
    settings = config.load_settings()
    assert settings.semantic_guardrails.provider == "jev"
    assert settings.semantic_guardrails.jev.api_key.get_secret_value() == "test-secret"
    monkeypatch.setenv(f"{prefix}_SEMANTIC_GUARDRAILS__PROVIDER", "laya")
    assert config.load_settings().semantic_guardrails.provider == "laya"
