"""Token-aware context assembler.

Packs system prompt + memory + history + current message into a real token
budget (not a char count). History is trimmed oldest-first at user-turn
boundaries so tool_call/tool_result pairs are never orphaned, and the system
prompt itself is trimmed section by section from the lowest priority up.
"""

import base64
import mimetypes
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

TokenCounter = Callable[[list[dict[str, Any]]], int]

_IMAGE_MIME_PREFIX = "image/"
_MAX_INLINE_IMAGE_BYTES = 8_000_000  # ~8MB per image sent inline to the model

_SECTION_SEPARATOR = "\n\n---\n\n"

# Share of the window left over after the current message that the system
# prompt may never eat into, so a bot with a long charter and a big memory
# document cannot squeeze the conversation out of its own context. Also the
# share history keeps even when the prompt overran anyway (see assemble):
# without it an oversized prompt drives the history budget negative and every
# earlier message is silently dropped — the model then answers a follow-up
# question having forgotten the question.
_HISTORY_RESERVE = 0.25

# Share of the window the system prompt keeps regardless of how large the
# current message measures. `reserved` is subtracted from the window, so a
# single enormous message would otherwise take the budget to zero and drop
# every droppable section. That measurement is the least trustworthy input
# here: the fallback counter is `len(json.dumps(...)) // 4`, which reads an
# inlined base64 image as ~2M tokens where the real cost is under a hundred, so
# one attached photo could strip the bot's memory and skills. A message genuinely
# that large overflows the window whatever we do — keeping the prompt coherent
# costs nothing then, and rescues the far commoner case of a miscount.
_PROMPT_FLOOR = 0.5

_OMITTED_NOTE = (
    "# Omitted this turn\n\nThese sections were left out to fit the context "
    "budget: {names}. They list things you can still reach with your tools or "
    "by asking, so say so rather than assuming they are empty."
)


def _omitted_note(dropped: list[str]) -> str:
    return _OMITTED_NOTE.format(names=", ".join(sorted(dropped)))


@dataclass(frozen=True, slots=True)
class PromptSection:
    """One block of the system prompt, in priority order (PRD §4.5).

    `pinned` marks a section that is never dropped. The charter is pinned
    because everything below it describes what the bot *has*, while the charter
    is what the bot *is* — a turn that runs without it is a different bot
    answering, which is worse than a turn that has to re-read a skill.
    """

    name: str
    text: str
    pinned: bool = False


def _fallback_counter(messages: list[dict[str, Any]]) -> int:
    import json

    return len(json.dumps(messages, ensure_ascii=False, default=str)) // 4


class ContextAssembler:
    def __init__(self, token_counter: TokenCounter | None = None, max_context_tokens: int = 60_000):
        self.count_tokens = token_counter or _fallback_counter
        self.max_context_tokens = max_context_tokens

    def assemble(
        self,
        system_sections: Sequence[PromptSection],
        history: list[dict[str, Any]],
        current_message: dict[str, Any],
    ) -> list[dict[str, Any]]:
        system = {
            "role": "system",
            "content": self.fit_system_prompt(
                system_sections, reserved=self.count_tokens([current_message])
            ),
        }
        base_cost = self.count_tokens([system, current_message])
        # Floored at history's own reserve rather than taken as whatever is left.
        # Pinned sections are kept whole even when they alone exceed the budget,
        # so `base_cost` can overrun the window — and then this subtraction went
        # negative and _trim_history returned nothing, wiping the conversation
        # silently on every turn. A 40k-token charter answers each message as if
        # it were the first. The request overflows either way in that case; the
        # difference is a provider error that names the problem versus a bot that
        # quietly has no memory of the last thing it was told.
        budget = max(self.max_context_tokens - base_cost, int(self.max_context_tokens * _HISTORY_RESERVE))

        trimmed = self._trim_history(history, budget)
        return [system, *trimmed, current_message]

    def fit_system_prompt(
        self, sections: Sequence[PromptSection], reserved: int = 0
    ) -> str:
        """Render the system prompt, dropping low-priority sections until it fits.

        `sections` is ordered most- to least-important, so trimming works from
        the end: knowledge and connector listings go before skills, skills
        before the plan, and the charter never goes at all. Pinned sections are
        kept whole even when they alone exceed the budget — a charter that big
        is a configuration problem, and truncating it mid-sentence would hide
        that instead of surfacing it.
        """
        kept = [s for s in sections if s.text.strip()]
        # The history reserve is a share of what is left AFTER the current
        # message, not of the whole window: reserving a slice of the full window
        # on top of a large message double-counts, and a long paste alone was
        # then enough to drive the budget below zero and drop every droppable
        # section — memory, plan and skills gone on exactly the turn the user
        # pastes a document and says "use my usual preferences", with most of
        # the window still unused.
        remaining = max(self.max_context_tokens - reserved, 0)
        budget = max(
            remaining - int(remaining * _HISTORY_RESERVE),
            int(self.max_context_tokens * _PROMPT_FLOOR),
        )
        dropped: list[str] = []
        note_cost = 0

        # The note is part of what has to fit, so its cost is charged as soon as
        # there is something to report — otherwise trimming to the budget and
        # then appending to the result can leave the prompt over it again.
        while self._cost(kept) + note_cost > budget:
            index = next((i for i in reversed(range(len(kept))) if not kept[i].pinned), None)
            if index is None:
                break
            dropped.append(kept.pop(index).name)
            note_cost = self.count_tokens(
                [{"role": "system", "content": _SECTION_SEPARATOR + _omitted_note(dropped)}]
            )

        text = _SECTION_SEPARATOR.join(s.text for s in kept)
        if dropped:
            text += _SECTION_SEPARATOR + _omitted_note(dropped)
        return text

    def _cost(self, sections: Sequence[PromptSection]) -> int:
        content = _SECTION_SEPARATOR.join(s.text for s in sections)
        return self.count_tokens([{"role": "system", "content": content}])

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


def _image_grounding(n: int) -> str:
    return (
        f"\n[{n} image{'s' if n > 1 else ''} attached below — treat as part of this message "
        "and use its visual content when answering.]"
    )


def vision_note(model: str, described: int, total: int, truncated: bool) -> str:
    """The sentence that stands in for images a model can't see.

    `described` is how many of the message's images the reader actually saw. It
    can be fewer than `total` — saying which is what stops the chat model
    answering confidently about an image nobody read.
    """
    plural = "s" if total > 1 else ""
    if described < total:
        scope = (
            f"{total} image{plural} attached, but only the first {described} could be read"
        )
        caveat = " Say so if asked about the ones that were not read."
    else:
        scope = f"{total} image{plural} attached"
        caveat = ""
    if truncated:
        caveat += " The description was cut off before it finished."
    return (
        f"\n[{scope}. The model answering this cannot see images, so {model} looked at "
        f"them and described them below — treat that description as the image "
        f"content.{caveat}]"
    )


def swap_images_for_description(
    content: list[dict[str, Any]],
    description: str,
    model: str,
    *,
    described: int,
    truncated: bool = False,
) -> str:
    """Turn a multimodal message into a text-only one carrying a description of
    its images, for a chat model that cannot accept image blocks.

    The "attached below" grounding sentence build_user_content() wrote is
    replaced rather than appended to: leaving it in would point the model at
    images that are no longer in the message. Lives next to the sentence it
    rewrites so the two can't drift apart.
    """
    total = sum(1 for b in content if b.get("type") == "image_url")
    text = "".join(b.get("text") or "" for b in content if b.get("type") == "text")
    note = vision_note(model, described, total, truncated) + "\n" + description.strip()
    if total and _image_grounding(total) in text:
        return text.replace(_image_grounding(total), note)
    return text + note


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
        grounding.append(_image_grounding(len(image_blocks)))
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


_PLAN_MARKS = {"done": "[x]", "in_progress": "[→]", "pending": "[ ]", "waiting_for_user": "[waiting for user]", "blocked": "[blocked]"}


def render_plan(plan: dict[str, Any] | None) -> str:
    """Render the session's working plan as a pinned system-prompt block.

    Empty string when there's no plan yet — so a fresh chat pays no token cost
    for it. The block is placed high in the system prompt (see runtime), above
    the skills and knowledge listings, which is the whole point: the goal and
    open steps stay in front of the model even after early turns scroll out.
    """
    if not plan:
        return ""
    goal = str(plan.get("goal") or "").strip()
    steps = plan.get("steps") or []
    if not goal and not steps:
        return ""
    lines = [
        "# Current Plan",
        "(You maintain this with the update_plan tool. It is pinned here every "
        "turn — it stays even if earlier messages scroll out of context, so use "
        "it to keep the thread and finish what you started.)",
    ]
    if goal:
        lines.append(f"\n**Goal:** {goal}")
    if steps:
        lines.append("")
        for s in steps:
            if not isinstance(s, dict):
                continue
            mark = _PLAN_MARKS.get(s.get("status", "pending"), "[ ]")
            lines.append(f"- {mark} {str(s.get('step') or '').strip()}")
    return "\n".join(lines)


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
