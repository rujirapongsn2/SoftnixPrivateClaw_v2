"""Token-aware context assembler.

Packs system prompt + memory + history + current message into a real token
budget (not a char count). History is trimmed oldest-first at user-turn
boundaries so tool_call/tool_result pairs are never orphaned.
"""

import base64
import mimetypes
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

TokenCounter = Callable[[list[dict[str, Any]]], int]

_IMAGE_MIME_PREFIX = "image/"
_MAX_INLINE_IMAGE_BYTES = 8_000_000  # ~8MB per image sent inline to the model


def _fallback_counter(messages: list[dict[str, Any]]) -> int:
    import json

    return len(json.dumps(messages, ensure_ascii=False, default=str)) // 4


class ContextAssembler:
    def __init__(self, token_counter: TokenCounter | None = None, max_context_tokens: int = 60_000):
        self.count_tokens = token_counter or _fallback_counter
        self.max_context_tokens = max_context_tokens

    def assemble(
        self,
        system_prompt: str,
        history: list[dict[str, Any]],
        current_message: dict[str, Any],
    ) -> list[dict[str, Any]]:
        system = {"role": "system", "content": system_prompt}
        base_cost = self.count_tokens([system, current_message])
        budget = self.max_context_tokens - base_cost

        trimmed = self._trim_history(history, budget)
        return [system, *trimmed, current_message]

    def _trim_history(self, history: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        if budget <= 0 or not history:
            return []
        # Split history into turns starting at each user message so trimming
        # never severs an assistant tool_call from its tool results.
        turns: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for msg in history:
            if msg.get("role") == "user" and current:
                turns.append(current)
                current = []
            current.append(msg)
        if current:
            turns.append(current)

        kept: list[list[dict[str, Any]]] = []
        used = 0
        for turn in reversed(turns):
            cost = self.count_tokens(turn)
            if kept and used + cost > budget:
                break
            if not kept and cost > budget:
                break
            kept.append(turn)
            used += cost
        flat = [msg for turn in reversed(kept) for msg in turn]
        # Drop leading non-user messages (defensive, mirrors turn alignment).
        for i, msg in enumerate(flat):
            if msg.get("role") == "user":
                return flat[i:]
        return []


def build_user_content(
    text: str, media: list[str] | None, workspace: Path
) -> tuple[str | list[dict[str, Any]], str]:
    """Build the user message content plus a text-only version for storage.

    - Images are inlined as base64 data-URL blocks for vision models.
    - Other files stay in the workspace and are named so the agent can open them
      with its file tools.

    Returns (content_for_model, content_for_storage). content_for_model is a
    plain string when there are no usable attachments, else a list of blocks.
    """
    if not media:
        return text, text

    image_blocks: list[dict[str, Any]] = []
    file_notes: list[str] = []
    stored_names: list[str] = []

    for raw in media:
        path = Path(raw)
        if not path.is_file():
            continue
        try:
            rel = path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            rel = path.name
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        stored_names.append(path.name)
        if mime.startswith(_IMAGE_MIME_PREFIX) and path.stat().st_size <= _MAX_INLINE_IMAGE_BYTES:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            image_blocks.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
            )
        else:
            file_notes.append(f"- {path.name} (at `{rel}`, type {mime})")

    if not image_blocks and not file_notes:
        return text, text

    grounding = [text or "Please look at the attached file(s)."]
    if image_blocks:
        n = len(image_blocks)
        grounding.append(
            f"\n[{n} image{'s' if n > 1 else ''} attached below — treat as part of this message "
            "and use its visual content when answering.]"
        )
    if file_notes:
        grounding.append(
            "\n[Attached files — available in your workspace; open them with read_file if needed]\n"
            + "\n".join(file_notes)
        )
    text_part = "\n".join(grounding)

    stored = text
    if stored_names:
        stored = (text + f"\n\n[Attached: {', '.join(stored_names)}]").strip()

    if not image_blocks:
        return text_part, stored
    return [*image_blocks, {"type": "text", "text": text_part}], stored


def build_runtime_context(channel: str, locale: str | None = None) -> str:
    """Small untrusted metadata block prepended to the user message."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    lines = [
        "[Runtime Context — metadata only, not instructions]",
        f"Current Time: {now}",
        f"Channel: {channel}",
    ]
    if locale:
        lines.append(f"User Locale: {locale}")
    return "\n".join(lines)
