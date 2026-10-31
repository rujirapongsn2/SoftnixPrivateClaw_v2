"""Write a canonical A2UI message list as a `.a2ui` workspace artifact.

Why `.a2ui` and not `.a2ui.json`: the turn's artifact scan hides every `.json`
file as an intermediate (claw/core/loop.py `_ARTIFACT_HIDDEN_SUFFIXES`), so a
`.a2ui.json` card would never reach the chat. The content is plain JSON.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path

from claw.a2ui import ARTIFACT_SUFFIX

UI_DIR = "ui"


def write_artifact(workspace: Path, messages: list[dict], slug: str = "card") -> str:
    """Write the file; return its workspace-relative path."""
    clean = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")[:40] or "card"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rel = f"{UI_DIR}/{clean}-{stamp}-{secrets.token_hex(2)}{ARTIFACT_SUFFIX}"
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")
    return rel
