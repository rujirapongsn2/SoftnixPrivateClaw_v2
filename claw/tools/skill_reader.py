"""Shared pagination and Markdown-section selection for ``read_skill``."""

import re

DEFAULT_READ_LIMIT = 8_000
# Leave room for the result title and continuation instruction below the agent
# loop's 12,000-character tool-result budget.
MAX_READ_LIMIT = 10_000

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def _normalise_heading(value: str) -> str:
    return " ".join(value.strip().lstrip("#").strip().rstrip("#").split()).casefold()


def _markdown_headings(content: str) -> list[tuple[int, int, str]]:
    """Find ATX headings while ignoring text inside fenced code blocks."""
    headings: list[tuple[int, int, str]] = []
    fence_char: str | None = None
    fence_length = 0
    position = 0
    for line in content.splitlines(keepends=True):
        stripped = line.lstrip(" \t")
        if fence_char is not None:
            run_length = len(stripped) - len(stripped.lstrip(fence_char))
            if run_length >= fence_length and stripped[run_length:].strip() == "":
                fence_char = None
                fence_length = 0
        else:
            fence = _FENCE.match(line)
            if fence is not None:
                fence_char = fence.group(1)[0]
                fence_length = len(fence.group(1))
            else:
                heading = _HEADING.match(line)
                if heading is not None:
                    headings.append((position, len(heading.group(1)), heading.group(2).rstrip("#").strip()))
        position += len(line)
    return headings


def select_section(content: str, section: str | None) -> tuple[str, str | None]:
    """Return one Markdown section, ending before the next equal-or-higher heading."""
    if not section or not section.strip():
        return content, None
    wanted = _normalise_heading(section)
    headings = _markdown_headings(content)
    for index, (start, level, title) in enumerate(headings):
        if _normalise_heading(title) != wanted:
            continue
        end = len(content)
        for following_start, following_level, _ in headings[index + 1 :]:
            if following_level <= level:
                end = following_start
                break
        return content[start:end].rstrip(), title
    return "", None


def page_skill_content(content: str, *, name: str, section: str | None, offset: int, limit: int) -> str:
    """Format one bounded page and an exact cursor for the next read."""
    offset = max(0, offset)
    if offset >= len(content) and content:
        return f"Error: offset {offset} is past the end of skill '{name}' ({len(content)} characters)."
    page = content[offset : offset + limit]
    title = f"# Skill: {name}"
    if section:
        title += f"\n\n## Section: {section}"
    next_offset = offset + len(page)
    if next_offset < len(content):
        continuation = f"\n\n[More available: call read_skill with name='{name}', offset={next_offset}, limit={limit}"
        if section:
            continuation += f", section='{section}'"
        continuation += ".]"
        return f"{title}\n\n{page}{continuation}"
    return f"{title}\n\n{page}"
