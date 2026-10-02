"""Delete the files a chat created when the chat is deleted.

The chat's own record says which files it touched: artifact chips (`meta`), the
paths the agent wrote with `write_file`/`edit_file`/`generate_workbook`, and
the attachments named in the user's messages. Files made by an arbitrary shell
command are not recorded anywhere, so they stay (the retention sweep and the
quota still cover them).

A file is deleted only when it is clearly this chat's alone:
* it is inside the workspace and is a regular file (never a link or a folder);
* the chat created it (its first use of the path is a write, not an edit);
* no remaining chat of the same user shows it as a file chip or has a tool call
  that wrote or edited it;
* it was not modified after the chat's last message (a later chat that rewrote
  the same name owns the newer content).
"""

import asyncio
import json
import os
import re
from pathlib import Path

from loguru import logger

from claw.workspace.cleanup import UPLOADS_DIR

_ATTACHED_RE = re.compile(r"\[Attached: ([^\]]+)\]")
_WRITE_TOOLS = {"write_file", "edit_file"}
_MAX_CANDIDATES = 500
# Slack between the last message and the files it wrote (a file is written
# before the message that announces it).
_MODIFIED_AFTER_SLACK = 300.0


def candidate_paths(hints: dict) -> list[str]:
    """Workspace-relative paths the chat's record says it created, first seen first."""
    found: dict[str, None] = {}
    # Only files this chat CREATED. A path whose first use here is an edit already
    # existed (it is someone's earlier work), so editing it does not make it this
    # chat's to delete. That holds for the file chips too: a file the agent only
    # edited is shown as a chip as well, so a chip alone proves nothing.
    first_use: dict[str, str] = {}
    for calls in hints.get("tool_calls") or []:
        for call in calls or []:
            function = (call or {}).get("function") or {}
            name = function.get("name")
            if name not in _WRITE_TOOLS and name != "generate_workbook":
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(args, dict):
                continue
            target = args.get("output") if name == "generate_workbook" else args.get("path")
            if isinstance(target, str) and target:
                first_use.setdefault(target, name)
    only_edited = {path for path, name in first_use.items() if name == "edit_file"}
    for meta in hints.get("meta") or []:
        for artifact in (meta or {}).get("artifacts") or []:
            if isinstance(artifact, str) and artifact not in only_edited:
                found[artifact] = None
    for target in first_use:
        if target not in only_edited:
            found[target] = None
    for text in hints.get("user_text") or []:
        for group in _ATTACHED_RE.findall(text or ""):
            for filename in group.split(","):
                filename = filename.strip()
                # Upload names are "<8 hex>-<name>": unique, so never another chat's file.
                if filename and "/" not in filename:
                    found[f"{UPLOADS_DIR}/{filename}"] = None
    return list(found)[:_MAX_CANDIDATES]


def remove_files(workspace: Path, paths: list[str], *, not_modified_after: float) -> tuple[int, int]:
    """Delete `paths` under `workspace` that are safe to remove. Blocking.

    Returns (files deleted, bytes freed).
    """
    root = os.path.realpath(workspace)
    deleted = freed = 0
    for rel in paths:
        candidate = os.path.join(root, rel)
        # realpath on the parent only: the file itself must not be a link, and
        # the folder it sits in must resolve inside the workspace.
        parent = os.path.realpath(os.path.dirname(candidate))
        if parent != root and not parent.startswith(root + os.sep):
            continue
        target = os.path.join(parent, os.path.basename(candidate))
        try:
            info = os.lstat(target)
        except OSError:
            continue
        if os.path.islink(target) or not os.path.isfile(target):
            continue
        if info.st_mtime > not_modified_after:
            continue
        try:
            os.unlink(target)
        except OSError:
            continue
        deleted += 1
        freed += info.st_size
    return deleted, freed


async def purge_session_files(state, user_id: str, hints: dict, workspace: Path) -> int:
    """Remove what a just-deleted chat created. Never raises: the delete already happened."""
    try:
        candidates = candidate_paths(hints)
        if not candidates:
            return 0
        still_shown = await state.messages.paths_still_referenced(user_id, candidates)
        doomed = [path for path in candidates if path not in still_shown]
        last = hints.get("last_message_at")
        limit = (last.timestamp() if last is not None else 0.0) + _MODIFIED_AFTER_SLACK
        deleted, freed = await asyncio.to_thread(remove_files, workspace, doomed, not_modified_after=limit)
        if deleted:
            logger.info("Deleted {} workspace files ({} bytes) with their chat", deleted, freed)
            accounts = getattr(state.runtime, "workspace_accounts", None)
            if accounts is not None:
                accounts.forget(user_id, now=True)
        return deleted
    except Exception:
        logger.exception("Could not delete a deleted chat's workspace files")
        return 0
