"""Softnix ONE connector: the MCP endpoint URL must be user-overridable, since
self-hosted/on-prem deployments live at a customer-specific URL (unlike
Composio's one shared public gateway)."""

from claw.core.connector_presets import get_preset, list_presets


def test_softnix_one_url_is_configurable():
    preset = get_preset("softnix-one")
    assert preset is not None
    assert preset.url_configurable is True
    assert preset.url == "https://mcp-softnix-one.softnix.ai/mcp"  # sane default


def test_composio_url_stays_fixed():
    # Composio is a single shared public gateway — no per-tenant endpoint, so
    # it should NOT expose an editable URL field.
    preset = get_preset("composio")
    assert preset is not None
    assert preset.url_configurable is False


def test_url_configurable_serializes_to_dict():
    catalog = {p["key"]: p for p in list_presets()}
    assert catalog["softnix-one"]["url_configurable"] is True
    assert catalog["composio"]["url_configurable"] is False
