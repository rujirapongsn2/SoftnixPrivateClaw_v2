"""Skill tools.

`read_skill` — enabled skills are summarized in the system prompt; the agent
pulls full content on demand instead of paying for it every call.

`manage_skill` — lets the agent create/update/list/delete its own skills so they
persist in the store and show up in Settings → Skills. Writing a workspace file
does NOT create a skill; this tool is the only way.
"""

from typing import Any

from sbot.core.builtin_skills import builtin_skills, get_builtin_skill
from sbot.db.stores import SkillStore
from sbot.tools.base import Tool
from claw.tools.skill_reader import DEFAULT_READ_LIMIT, MAX_READ_LIMIT, page_skill_content, select_section

_MAX_NAME_LEN = 64


class ReadSkillTool(Tool):
    name = "read_skill"
    description = (
        "Read one available skill by name. Large skills are paginated: use offset and limit to "
        "continue, or section to read one Markdown heading and its contents."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name from the skills list"},
            "section": {"type": "string", "description": "Optional Markdown heading to read."},
            "offset": {"type": "integer", "minimum": 0, "description": "Character offset within the selected content."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_LIMIT, "description": "Characters to return (default 8000; maximum 10000)."},
        },
        "required": ["name"],
    }

    def __init__(self, store: SkillStore, user_id: str):
        self.store = store
        self.user_id = user_id

    async def execute(self, name: str, section: str | None = None, offset: int = 0, limit: int = DEFAULT_READ_LIMIT, **_: Any) -> str:
        name = name.strip()
        skill = await self.store.readable_by_name(self.user_id, name)
        if skill is not None and skill.enabled:
            resolved_name, content = skill.name, skill.content
        else:
            builtin = get_builtin_skill(name)
            if builtin is None:
                return f"Error: skill '{name}' not found or disabled"
            resolved_name, content = builtin.name, builtin.content
        selected, selected_section = select_section(content, section)
        if section and selected_section is None:
            return f"Error: section '{section}' was not found in skill '{resolved_name}'."
        try:
            page_limit = min(max(1, int(limit)), MAX_READ_LIMIT)
            page_offset = max(0, int(offset))
        except (TypeError, ValueError):
            return "Error: offset and limit must be integers."
        return page_skill_content(
            selected, name=resolved_name, section=selected_section, offset=page_offset, limit=page_limit
        )


class ManageSkillTool(Tool):
    name = "manage_skill"
    description = (
        "Create, update, list, or delete your reusable skills — the ones shown in Settings → Skills "
        "and offered to you in future chats. Use action 'save' to persist a skill: this is the ONLY way "
        "to create one (writing a file does not). Use 'list' to see them and 'delete' to remove one. "
        "Read the 'skill-creator' skill first for how to author a good skill."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["save", "list", "delete"]},
            "name": {
                "type": "string",
                "description": "Skill name in kebab-case (required for save/delete)",
            },
            "description": {
                "type": "string",
                "description": "One-line description shown to you in every chat (for save)",
            },
            "content": {
                "type": "string",
                "description": "Full instructions, loaded on demand when the skill is used (for save)",
            },
            "enabled": {
                "type": "boolean",
                "description": "Whether the skill is active (for save; defaults to true)",
            },
        },
        "required": ["action"],
    }

    def __init__(self, store: SkillStore, user_id: str):
        self.store = store
        self.user_id = user_id

    async def execute(self, action: str, **kwargs: Any) -> str:
        action = str(action or "").strip()
        if action == "list":
            skills = await self.store.list_for_user(self.user_id)
            if not skills:
                return "You have no saved skills yet. Use action 'save' to create one."
            lines = [
                f"- {s.name} ({'enabled' if s.enabled else 'disabled'}): "
                f"{s.description or '(no description)'}"
                for s in skills
            ]
            return "Your skills:\n" + "\n".join(lines)

        name = str(kwargs.get("name") or "").strip()

        if action == "save":
            if not name:
                return "Error: save requires a 'name'."
            if len(name) > _MAX_NAME_LEN:
                return f"Error: name must be at most {_MAX_NAME_LEN} characters."
            if get_builtin_skill(name) is not None:
                return f"Error: '{name}' is a built-in skill name and is reserved. Choose another name."
            content = str(kwargs.get("content") or "").strip()
            if not content:
                return "Error: save requires 'content' (the skill instructions)."
            description = str(kwargs.get("description") or "").strip()
            enabled_raw = kwargs.get("enabled")
            enabled = True if enabled_raw is None else bool(enabled_raw)
            await self.store.upsert(
                self.user_id, name, description=description, content=content, enabled=enabled
            )
            state = "enabled" if enabled else "disabled"
            return f"Skill '{name}' saved ({state}). It now appears in Settings → Skills."

        if action == "delete":
            if not name:
                return "Error: delete requires a 'name'."
            existing = await self.store.get_by_name(self.user_id, name)
            if existing is None:
                return f"Error: skill '{name}' not found."
            await self.store.delete(self.user_id, existing.id)
            return f"Skill '{name}' deleted."

        return f"Error: unknown action '{action}'."


def scope_skills(skills: list, skill_ids: list[str] | None) -> list:
    """Narrow the owner's enabled skills to the ones assigned to one bot (PRD §3.1).

    `None` means every skill, which is what a bot created without a skill list
    gets — so this only ever subtracts once someone has actually curated the
    bot. An empty list means none: a bot can legitimately be defined as one
    that reasons and writes without any skill.

    Matched on id *or* name because built-in skills are code-defined and have
    no row to point at. This is focus, not isolation: every skill in play
    belongs to the same owner, and `read_skill` can still open one by name.
    Its job is to stop a specialist's prompt filling up with instructions for
    work it was never meant to do.
    """
    if skill_ids is None:
        return skills
    wanted = {str(s) for s in skill_ids}
    return [s for s in skills if getattr(s, "id", None) in wanted or s.name in wanted]


def build_skills_summary(skills: list, tool_names_by_skill: dict[str, list[str]] | None = None) -> str:
    """System-prompt section listing enabled skills (names + descriptions only).

    ``tool_names_by_skill`` (skill name -> its linked connector's CURRENT
    registered tool names, resolved live per-turn) lets a skill's own text
    stay generic about "the connected knowledge base" instead of hardcoding a
    connector name that can be renamed later — the exact names to call are
    appended here instead, always up to date."""
    if not skills:
        return ""
    lines = [
        "# Skills",
        "",
        "These skills extend your capabilities. To use one, read its full content "
        "with the read_skill tool first — but only when you are actually going to do "
        "the thing it describes. If it says more content is available, call read_skill "
        "again with the offset shown there. "
        "Writing content in the chat never needs a skill; the "
        "document skills apply when the user asked for a real file, not when they "
        "asked for the text that would go in one.",
        "",
    ]
    for s in skills:
        line = f"- {s.name}: {s.description or '(no description)'}"
        names = (tool_names_by_skill or {}).get(s.name)
        if names:
            line += f" (call these exact tools: {', '.join(names)})"
        lines.append(line)
    return "\n".join(lines)
