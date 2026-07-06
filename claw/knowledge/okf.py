"""Write a knowledge base to disk as an Open Knowledge Format (OKF) bundle.

OKF (https://github.com/GoogleCloudPlatform/knowledge-catalog) is just a
directory of markdown files with YAML frontmatter: one "concept" per document,
an optional `index.md` directory listing, and an optional `log.md` history.
It's human-readable, diffable, and portable — no bespoke format. We keep these
files as the canonical, exportable representation; the DB chunk table is a
derived search index over the same content.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

_RESERVED = {"index", "log"}


def slugify(filename: str, existing: set[str]) -> str:
    """A stable, filesystem-safe concept id from a filename, unique within the
    bundle (never collides with reserved index/log or an existing concept)."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    slug = re.sub(r"[^a-z0-9ก-๙]+", "-", stem.lower()).strip("-") or "document"
    if slug in _RESERVED:
        slug = f"{slug}-doc"
    base = slug
    n = 2
    while slug in existing:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _yaml_value(value: str) -> str:
    # Quote scalars that could confuse a YAML parser; keep it simple.
    if value and re.search(r'[:#\[\]{}"\n]', value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def render_concept(
    *,
    title: str,
    description: str,
    body: str,
    resource: str = "",
    tags: list[str] | None = None,
    timestamp: datetime | None = None,
) -> str:
    """Render one OKF concept document (YAML frontmatter + markdown body)."""
    ts = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm = ["---", "type: Document"]
    if title:
        fm.append(f"title: {_yaml_value(title)}")
    if description:
        fm.append(f"description: {_yaml_value(description)}")
    if resource:
        fm.append(f"resource: {_yaml_value(resource)}")
    if tags:
        fm.append("tags: [" + ", ".join(_yaml_value(t) for t in tags) + "]")
    fm.append(f"timestamp: {ts}")
    fm.append("---")
    return "\n".join(fm) + "\n\n" + body.strip() + "\n"


class OkfBundle:
    """Filesystem view of one knowledge base's OKF bundle directory."""

    def __init__(self, root: Path, kb_id: str):
        self.dir = (root / kb_id).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)

    def existing_concepts(self) -> set[str]:
        return {p.stem for p in self.dir.glob("*.md") if p.stem not in _RESERVED}

    def write_concept(self, concept_id: str, content: str) -> None:
        (self.dir / f"{concept_id}.md").write_text(content, "utf-8")

    def remove_concept(self, concept_id: str) -> None:
        (self.dir / f"{concept_id}.md").unlink(missing_ok=True)

    def write_index(self, entries: list[tuple[str, str, str]]) -> None:
        """entries: (concept_id, title, description). Regenerates index.md."""
        lines = ["# Knowledge base contents", ""]
        for concept_id, title, description in entries:
            desc = f" - {description}" if description else ""
            lines.append(f"* [{title or concept_id}](/{concept_id}.md){desc}")
        (self.dir / "index.md").write_text("\n".join(lines) + "\n", "utf-8")

    def append_log(self, action: str, title: str, concept_id: str) -> None:
        """Append a dated entry to log.md (newest day first, per the OKF convention)."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = f"* **{action}**: [{title or concept_id}](/{concept_id}.md)\n"
        path = self.dir / "log.md"
        existing = path.read_text("utf-8") if path.exists() else "# Update log\n"
        marker = f"## {day}\n"
        if marker in existing:
            existing = existing.replace(marker, marker + entry, 1)
        else:
            # Insert a new day section right after the title heading.
            head, _, rest = existing.partition("\n")
            existing = f"{head}\n\n{marker}{entry}{rest.lstrip()}"
        path.write_text(existing, "utf-8")
