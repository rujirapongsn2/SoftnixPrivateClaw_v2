"""Every audit kind must fit audit_events.kind.

SQLite (the test database) does not enforce VARCHAR lengths, Postgres does: a
longer kind passed tests but made PUT /api/agent-clients/{id}/callback return
500 on Postgres after its change had already been committed."""

import ast
from pathlib import Path

from claw.db.models import AuditEvent

ROOT = Path(__file__).resolve().parents[1] / "claw"


def _audit_kinds():
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "log"):
                continue
            target = node.func.value
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if "audit" not in name or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                yield f"{path.relative_to(ROOT.parent)}:{node.lineno}", first.value


def test_every_literal_audit_kind_fits_the_column():
    limit = AuditEvent.__table__.c.kind.type.length
    kinds = list(_audit_kinds())
    assert len(kinds) > 20  # the scan really found the audit calls
    too_long = [(where, kind, len(kind)) for where, kind in kinds if len(kind) > limit]
    assert not too_long, f"audit kinds longer than {limit} chars: {too_long}"
