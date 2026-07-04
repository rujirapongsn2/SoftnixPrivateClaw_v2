"""Skill access tool: enabled skills are summarized in the system prompt;
the agent pulls full content on demand instead of paying for it every call."""

from typing import Any

from claw.db.stores import SkillStore
from claw.tools.base import Tool


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
        skill = await self.store.get_by_name(self.user_id, name.strip())
        if skill is None or not skill.enabled:
            return f"Error: skill '{name}' not found or disabled"
        return f"# Skill: {skill.name}\n\n{skill.content}"


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
