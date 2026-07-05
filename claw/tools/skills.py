"""Skill tools.

`read_skill` — enabled skills are summarized in the system prompt; the agent
pulls full content on demand instead of paying for it every call.

`manage_skill` — lets the agent create/update/list/delete its own skills so they
persist in the store and show up in Settings → Skills. Writing a workspace file
does NOT create a skill; this tool is the only way.
"""

from typing import Any

from claw.core.builtin_skills import builtin_skills, get_builtin_skill
from claw.db.stores import SkillStore
from claw.tools.base import Tool

_MAX_NAME_LEN = 64


class ReadSkillTool(Tool):
    name = "read_skill"
    description = "Read the full content of one of your available skills by name."
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Skill name from the skills list"}},
        "required": ["name"],
    }

    def __init__(self, store: SkillStore, user_id: str):
        self.store = store
        self.user_id = user_id

    async def execute(self, name: str, **_: Any) -> str:
        name = name.strip()
        skill = await self.store.get_by_name(self.user_id, name)
        if skill is not None and skill.enabled:
            return f"# Skill: {skill.name}\n\n{skill.content}"
        builtin = get_builtin_skill(name)
        if builtin is not None:
            return f"# Skill: {builtin.name}\n\n{builtin.content}"
        return f"Error: skill '{name}' not found or disabled"


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


def build_skills_summary(skills: list) -> str:
    """System-prompt section listing enabled skills (names + descriptions only)."""
    if not skills:
        return ""
    lines = [
        "# Skills",
        "",
        "These skills extend your capabilities. To use one, read its full content "
        "with the read_skill tool first.",
        "",
    ]
    lines += [f"- {s.name}: {s.description or '(no description)'}" for s in skills]
    return "\n".join(lines)
