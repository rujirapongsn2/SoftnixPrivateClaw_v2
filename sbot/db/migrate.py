"""Programmatic Alembic runner used at startup."""

from pathlib import Path

from loguru import logger

_ROOT = Path(__file__).resolve().parents[2]


def run_migrations() -> None:
    """Upgrade the database to head. Safe to call on every startup."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    logger.info("Database migrated to head")
