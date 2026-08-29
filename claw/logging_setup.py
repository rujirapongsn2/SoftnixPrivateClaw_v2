"""Replace loguru's default sink with a configured, rotating, non-leaking one."""

import gzip
import os
import shutil
import sys
from pathlib import Path

from loguru import logger

from claw.config import LogSettings

# Owner-only. The file holds prompts, message text and connector errors for
# every tenant on the box, so it must not be world-readable the way a plain
# shell redirect leaves it (0644 under the usual 022 umask).
_LOG_FILE_MODE = 0o600


def _open_private(path, mode: str):
    """loguru `opener` — create the log 0600 rather than chmod'ing it after
    the fact, so there is no window where a fresh (or freshly rotated) file is
    readable by other local accounts."""
    return os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, _LOG_FILE_MODE)


def _compress_private(path: str) -> None:
    """loguru `compression` — gzip a rotated segment, replacing it.

    loguru's own "gz" compressor creates the archive at the umask (0644), which
    would undo _open_private for every segment except the live one — i.e. for
    almost the entire retained history.
    """
    archive = path + ".gz"
    fd = os.open(archive, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, _LOG_FILE_MODE)
    # GzipFile.close() only closes a fileobj it opened itself from a filename
    # — handed an existing fileobj (raw, below), it flushes into it but never
    # closes it. Naming that fileobj and closing it via its own `with` makes
    # the close explicit instead of relying on refcounting GC to do it when
    # the anonymous object is collected.
    with open(path, "rb") as src, os.fdopen(fd, "wb") as raw, gzip.open(raw, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(path)


def configure_logging(settings: LogSettings, *, root: Path | None = None) -> None:
    """Install the application's sinks. Idempotent — safe to call per app."""
    logger.remove()
    common = {
        "level": settings.level.upper(),
        "backtrace": settings.backtrace,
        "diagnose": settings.diagnose,
    }
    console = logger.add(sys.stderr, **common)
    if not settings.file.strip():
        return

    path = Path(settings.file)
    if not path.is_absolute():
        path = (root or Path.cwd()) / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            path,
            rotation=settings.rotation,
            retention=settings.retention,
            compression=_compress_private if settings.compress else None,
            opener=_open_private,
            **common,
        )
    except OSError:
        # A read-only or missing data dir must not stop the app from booting —
        # stderr is still attached, so logs keep reaching the supervisor.
        logger.opt(exception=True).warning("Log file sink disabled: cannot write {}", path)
        return

    if not sys.stderr.isatty():
        # Under a supervisor, stderr is a file the shell holds open in append
        # mode (claw.log / claw.err.log) or journald — writing there as well
        # would put every record in two places, and that second copy is the
        # one nothing can rotate. Keep the console sink only when a human is
        # actually watching it.
        logger.remove(console)
