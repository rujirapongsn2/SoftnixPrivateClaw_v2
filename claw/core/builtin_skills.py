"""Built-in ("pre-built") skills shipped with the platform.

Unlike user skills (rows in the ``skills`` table), these are defined in code so
they're always available, version-controlled, and can't be edited or deleted.
They're merged into the agent's skills list — the system-prompt summary and the
``read_skill`` tool — and surfaced read-only in Settings → Skills.

The first built-in, ``skill-creator``, teaches the agent how to author new user
skills correctly via the ``manage_skill`` tool (writing workspace files does not
create a skill, which is the trap that made agent-"created" skills never show up).
"""

from dataclasses import dataclass
from datetime import datetime, timezone

# Stable timestamp so the API payload is deterministic across restarts.
_BUILTIN_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class BuiltinSkill:
    """A code-defined skill; duck-types the ORM ``Skill`` for the summary/API."""

    name: str
    description: str
    content: str
    enabled: bool = True

    @property
    def id(self) -> str:
        return f"builtin:{self.name}"

    @property
    def updated_at(self) -> datetime:
        return _BUILTIN_TS


_SKILL_CREATOR_CONTENT = """\
A skill is a reusable procedure stored in the system and offered to you in every
future chat. A skill is NOT a workspace file: writing a `.py`/`.md` file does NOT
create a skill and it will never appear in Settings → Skills. The ONLY way to
create or update a skill is the `manage_skill` tool.

## Create a skill (manage_skill, action="save")
1. name — short, kebab-case, unique per user (e.g. `stock-analysis`, `weekly-report`).
2. description — ONE line, shown to you in every chat's system prompt. This is how
   future-you decides to use the skill, so make it specific and trigger-oriented:
   what it does AND when to use it. e.g. "Analyse a Thai/US stock over N months
   (price trend, dividend yield, PE) — use when the user asks to analyse a stock."
3. content — the full instructions, loaded on demand when you `read_skill`. Keep it
   procedural and focused:
   - The exact steps, in order.
   - Which tools to use at each step (web_search, mcp_* connectors, exec for
     computation/charts, write_file for outputs).
   - The output the user expects (table, chart PNG, PDF) and its format.
   - Sensible defaults and edge cases (default period, currency, data source).
4. Call `manage_skill` with action="save" and those fields.

## What makes a good skill
- One skill = one clear procedure. Don't bundle unrelated tasks.
- Capture the reusable *method*, not one-off data from this chat.
- Name the tools you'll call so future-you needs no guesswork.
- Keep content concise — you pay tokens to read it; link steps, don't pad.
- After saving, tell the user it's saved and now appears in Settings → Skills
  (enabled by default).

## Manage existing skills
- action="list" — list your saved skills.
- action="save" — create, or overwrite one with the same name.
- action="delete" — remove a skill by name.

Never try to "install" a skill by writing files or editing the database — always
go through `manage_skill`.
"""

_BUILTIN_SKILLS: tuple[BuiltinSkill, ...] = (
    BuiltinSkill(
        name="skill-creator",
        description=(
            "How to author a new reusable skill correctly — use whenever the user asks you to "
            "create, build, or save a skill."
        ),
        content=_SKILL_CREATOR_CONTENT,
    ),
)


def builtin_skills() -> list[BuiltinSkill]:
    """All built-in skills (always enabled)."""
    return list(_BUILTIN_SKILLS)


def get_builtin_skill(name: str) -> BuiltinSkill | None:
    for skill in _BUILTIN_SKILLS:
        if skill.name == name:
            return skill
    return None
