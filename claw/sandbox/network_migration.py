"""Move an existing env file to the isolated sandbox network (``none``).

The code default for ``CLAW_SANDBOX__NETWORK`` is ``none``, but an env file
written by an older install.sh says ``bridge`` explicitly, and an explicit
value wins over the default. ``claw update`` and ``install.sh`` run this
module so an updated machine ends up isolated too:

    python -m claw.sandbox.network_migration .env            # apply
    python -m claw.sandbox.network_migration .env --check    # report only

scripts/claw-serve.sh also runs it (``--quiet``) before every start, because the
first ``claw update`` that brings this change still executes the old
scripts/claw, which doesn't know about it.

Opt-out: an operator who really needs the sandbox online marks the line, and
it is left alone:

    CLAW_SANDBOX__NETWORK=bridge  # claw:keep-network

The text transform (:func:`migrate_sandbox_network`) is pure so it can be
tested without touching a file.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

KEY = "CLAW_SANDBOX__NETWORK"
TARGET = "none"
KEEP_MARKER = "claw:keep-network"

_LINE = re.compile(rf"^(?P<indent>[ \t]*)(?P<export>export[ \t]+)?{KEY}[ \t]*=(?P<rest>.*)$")


@dataclass(frozen=True, slots=True)
class Migration:
    text: str
    changed: bool
    previous: str | None  # last effective value before the change; None = key absent
    kept: bool = False  # the operator opted out with the keep marker


def _value(rest: str) -> str:
    """The value the way the shell/pydantic read it: quotes stripped, comment dropped."""
    rest = rest.strip()
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        end = rest.find(quote, 1)
        return rest[1:end] if end != -1 else rest[1:]
    return rest.split("#", 1)[0].strip()


def migrate_sandbox_network(text: str) -> Migration:
    """Set every active ``CLAW_SANDBOX__NETWORK`` line to ``none``.

    Commented-out lines are ignored. A line carrying ``# claw:keep-network``
    stops the migration for the whole file. When the key is absent it is
    appended, so the file states the value it runs with.
    """
    lines = text.splitlines(keepends=True)
    hits = [(i, m) for i, line in enumerate(lines) if (m := _LINE.match(line.rstrip("\r\n")))]
    if not hits:
        sep = "" if not text or text.endswith("\n") else "\n"
        added = f"{sep}# Tool sandbox has no network (no internet, no host services).\n{KEY}={TARGET}\n"
        return Migration(text + added, changed=True, previous=None)
    previous = _value(hits[-1][1]["rest"])
    if any(KEEP_MARKER in m["rest"] for _, m in hits):
        return Migration(text, changed=False, previous=previous, kept=True)
    changed = False
    for i, m in hits:
        if _value(m["rest"]).lower() == TARGET:
            continue
        ending = lines[i][len(lines[i].rstrip("\r\n")) :]
        lines[i] = f"{m['indent']}{m['export'] or ''}{KEY}={TARGET}{ending}"
        changed = True
    return Migration("".join(lines), changed=changed, previous=previous)


def apply(path: Path, *, check: bool = False, quiet: bool = False, now: datetime | None = None) -> int:
    """Migrate ``path`` in place (with a timestamped backup). Returns an exit code."""
    if not path.is_file():
        if quiet:
            return 0
        print(f"==> sandbox network: {path} not found — the code default ({TARGET}) applies")
        return 0
    result = migrate_sandbox_network(path.read_text(encoding="utf-8"))
    if result.kept:
        print(f"==> sandbox network: {KEY}={result.previous} kept ({KEEP_MARKER} marker in {path})")
        return 0
    if not result.changed:
        if quiet:
            return 0
        print(f"==> sandbox network: already {TARGET} in {path}")
        return 0
    before = result.previous if result.previous is not None else "(unset)"
    if check:
        print(f"==> sandbox network: would change {KEY} {before} -> {TARGET} in {path}")
        return 0
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}-sandbox-network")
    shutil.copy2(path, backup)  # keeps the 0600 mode of a secrets file
    tmp = path.with_name(f".{path.name}.sandbox-network.tmp")
    tmp.write_text(result.text, encoding="utf-8")
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)
    print(
        f"==> sandbox network: {KEY} {before} -> {TARGET} in {path} (backup {backup.name}; "
        f"to keep the old value add '# {KEEP_MARKER}' to that line)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("env_file", type=Path)
    parser.add_argument("--check", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--quiet", action="store_true", help="print only when something changes")
    args = parser.parse_args(argv)
    return apply(args.env_file, check=args.check, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
