"""Column types that strip U+0000 before it reaches the database.

Postgres cannot store a NUL in `text` or `jsonb` under any encoding — it is not
a matter of escaping, the byte is simply not representable. Two different errors
result, so catching one is not enough: a raw NUL in a text column raises
CharacterNotInRepertoireError, while json.dumps escapes it to \\u0000, which
passes the UTF-8 byte check and is then rejected by jsonb as
UntranslatableCharacterError.

The values at risk are not ours: sandboxed command output, PyMuPDF/OCR text
extracted from user-supplied PDFs, and model-generated titles and summaries.
`bytes.decode(..., errors="replace")` does NOT filter these, because 0x00 is
*valid* UTF-8 and decodes cleanly to U+0000 — so every existing decode site is
already a potential ingress.

Stripping happens here, at the persistence boundary, rather than at each of
those sites: a missed call site is a crashed turn (or, in the background
knowledge ingest worker, a silently stuck document), and new columns get the
protection without anyone remembering to ask for it.

Note SQLite accepts NUL happily and round-trips it, so the test suite cannot
reproduce the production failure — that is precisely why this is enforced by the
column type, which runs on every dialect, instead of by a Postgres-only guard.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON as _JSON
from sqlalchemy import String as _String
from sqlalchemy import Text as _Text
from sqlalchemy.types import TypeDecorator


def _scrub(value: Any) -> Any:
    """Recursively strip NUL from the string leaves of a JSON-shaped value.
    Keys are scrubbed too — a NUL in a key is just as unstorable as one in a
    value. Non-JSON sentinels (None, JSON.NULL, numbers, bools) pass through
    untouched so the impl type's own null handling still applies."""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {_scrub(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


class _NulSafeStr(TypeDecorator):
    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if isinstance(value, str) and "\x00" in value:
            return value.replace("\x00", "")
        return value


# cache_ok says the type holds no per-instance state that changes the emitted
# SQL, so SQLAlchemy may reuse compiled-statement cache entries. It has to be
# repeated on every concrete class: SQLAlchemy reads it from the class's own
# __dict__, so an inherited value does not count and silently disables the
# compiled cache for each of the ~109 columns declared with these types.
class NulSafeString(_NulSafeStr):
    impl = _String
    cache_ok = True


class NulSafeText(_NulSafeStr):
    impl = _Text
    cache_ok = True


class NulSafeJSON(TypeDecorator):
    impl = _JSON
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return _scrub(value)


def render_migration_type(obj_type: str, obj: Any, autogen_context: Any) -> str | bool:
    """Alembic `render_item` hook: emit the plain SQLAlchemy type each of the
    types above wraps. Wired up in migrations/env.py, and living here rather
    than there because env.py runs migrations on import and so cannot be tested.

    These are bind-time-only decorators, so they emit identical DDL and a
    migration has no reason to name them. Left to itself, autogenerate renders
    `sbot.db.types.NulSafeText()` but adds no matching import — script.py.mako
    provides only `op` and `sa` — and the resulting file raises NameError.
    lifespan() runs `alembic upgrade head` at startup, so that would surface as
    a boot failure rather than an error when the migration was written.
    """
    if obj_type != "type":
        return False
    if isinstance(obj, NulSafeString):
        return f"sa.String(length={obj.impl.length})" if obj.impl.length else "sa.String()"
    if isinstance(obj, NulSafeText):
        return "sa.Text()"
    if isinstance(obj, NulSafeJSON):
        return "sa.JSON()"
    return False
