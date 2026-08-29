"""Logging must not leak secrets into a world-readable, unbounded file.

Left unconfigured, loguru's default sink has `diagnose=True`, which prints the
value of every local variable in every traceback frame — so one crash loop wrote
CLAW_DATABASE_URL (password included) into claw.log 8,650 times. These tests pin
the three properties that stop that: no variable dump, owner-only file mode, and
a bounded size.
"""

import gzip
import stat
import sys

from loguru import logger

from claw.config import LogSettings, Settings
from claw.logging_setup import configure_logging


def _drain(tmp_path, settings, emit):
    """Run `emit` with the given sinks installed and return the file's text."""
    try:
        configure_logging(settings, root=tmp_path)
        emit()
    finally:
        logger.remove()
    return (tmp_path / settings.file).read_text()


def test_secrets_in_locals_never_reach_the_log(tmp_path):
    def emit():
        def boom():
            database_url = "postgresql+asyncpg://claw:hunter2@localhost/claw"  # noqa: F841
            raise RuntimeError("write failed")

        try:
            boom()
        except RuntimeError:
            logger.exception("turn failed")

    text = _drain(tmp_path, LogSettings(file="claw.log"), emit)
    assert "hunter2" not in text
    # backtrace stays on, so the frame itself is still there to debug from.
    assert "boom" in text and "write failed" in text


def test_diagnose_stays_off_by_default():
    # The single field that decides whether the above test can regress.
    assert LogSettings().diagnose is False
    assert Settings(secret_key="x").log.diagnose is False


def test_log_file_is_created_owner_only(tmp_path):
    settings = LogSettings(file="nested/claw.log")
    _drain(tmp_path, settings, lambda: logger.info("hello"))
    mode = stat.S_IMODE((tmp_path / settings.file).stat().st_mode)
    assert mode == 0o600, f"log is group/world readable: {mode:o}"


def test_the_log_rotates_instead_of_growing_without_bound(tmp_path):
    settings = LogSettings(file="claw.log", rotation="4 KB", retention="1 day", compress=False)
    try:
        configure_logging(settings, root=tmp_path)
        for _ in range(400):
            logger.info("x" * 200)
    finally:
        logger.remove()
    logs = list(tmp_path.glob("claw*.log"))
    assert len(logs) > 1, "nothing rotated"
    assert all(f.stat().st_size < 64 * 1024 for f in logs)


def test_rotated_segments_are_owner_only_too(tmp_path):
    # Rotation re-opens the file; without the custom opener the new one would
    # be created at the umask's 0644 and the hardening would silently lapse.
    settings = LogSettings(file="claw.log", rotation="4 KB", retention="1 day", compress=False)
    try:
        configure_logging(settings, root=tmp_path)
        for _ in range(400):
            logger.info("y" * 200)
    finally:
        logger.remove()
    for f in tmp_path.glob("claw*.log"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


def test_compressed_archives_are_owner_only_and_readable(tmp_path):
    # loguru's built-in "gz" compressor writes at the umask (0644), so almost
    # the whole retained history would be world-readable while only the live
    # file stayed 0600 — see _compress_private.
    settings = LogSettings(file="claw.log", rotation="4 KB", retention="1 day", compress=True)
    try:
        configure_logging(settings, root=tmp_path)
        for _ in range(400):
            logger.info("secret-ish payload")
    finally:
        logger.remove()

    archives = list(tmp_path.glob("*.log.gz"))
    assert archives, "nothing was compressed"
    for f in archives:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    # The uncompressed segment must be gone, not left behind next to the .gz.
    assert not list(tmp_path.glob("claw.2*.log"))
    with gzip.open(archives[0], "rt") as fh:
        assert "secret-ish payload" in fh.read()


def test_level_filters_below_threshold(tmp_path):
    settings = LogSettings(file="claw.log", level="warning")
    text = _drain(
        tmp_path,
        settings,
        lambda: (logger.debug("noise"), logger.warning("kept")),
    )
    assert "noise" not in text and "kept" in text


def test_console_sink_is_dropped_when_a_file_sink_owns_the_output(tmp_path, capsys):
    # Under a supervisor, stderr is the append-mode file the shell holds open
    # (claw.log) — a second copy there is the one nothing can rotate. pytest
    # replaces sys.stderr with a non-tty, which is the case being pinned.
    assert not sys.stderr.isatty()
    try:
        configure_logging(LogSettings(file="claw.log"), root=tmp_path)
        logger.info("once")
    finally:
        logger.remove()
    assert "once" not in capsys.readouterr().err
    assert "once" in (tmp_path / "claw.log").read_text()


def test_console_sink_survives_when_the_file_sink_is_disabled(tmp_path, capsys):
    # Otherwise CLAW_LOG__FILE="" would silence the app entirely.
    try:
        configure_logging(LogSettings(file=""), root=tmp_path)
        logger.info("only-copy")
    finally:
        logger.remove()
    assert "only-copy" in capsys.readouterr().err


def test_an_unwritable_log_path_does_not_stop_startup(tmp_path, capsys):
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    try:
        configure_logging(LogSettings(file=str(blocked / "sub" / "claw.log")), root=tmp_path)
        logger.info("still-logging")
    finally:
        logger.remove()
        blocked.chmod(0o700)
    # Console sink must be kept in this branch, or the failure is invisible.
    err = capsys.readouterr().err
    assert "still-logging" in err and "Log file sink disabled" in err
