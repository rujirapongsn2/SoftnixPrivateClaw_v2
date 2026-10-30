"""A2UI (spec v0.9.1) support for claw mode — POC.

Feature flag: ``Settings.a2ui_enabled`` (env ``CLAW_A2UI_ENABLED``, default on).
``main.create_app`` calls :func:`configure` once so code without access to the
settings object (the built-in skill list) reads the same switch.
"""

A2UI_VERSION = "v0.9"
SOFTNIX_CATALOG_ID = "https://softnix.ai/a2ui/catalogs/privateclaw/v0.9.json"
ARTIFACT_SUFFIX = ".a2ui"

_enabled = True


def configure(enabled: bool) -> None:
    global _enabled
    _enabled = bool(enabled)


def is_enabled() -> bool:
    return _enabled
