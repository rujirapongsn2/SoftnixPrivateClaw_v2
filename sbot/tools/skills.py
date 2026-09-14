"""Skill tools.

`read_skill` — enabled skills are summarized in the system prompt; the agent
pulls full content on demand instead of paying for it every call.

`manage_skill` — lets the agent create/update/list/delete its own skills so they
persist in the store and show up in Settings → Skills. Writing a workspace file
does NOT create a skill; this tool is the only way.
"""

import asyncio
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.exc import IntegrityError

from sbot.core.builtin_skills import get_builtin_skill
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
            "materialize": {"type": "boolean", "description": "Copy a template/image/font resource to workspace instead of returning text. Requires path. Package scripts cannot be materialized."},
            "path": {"type": "string", "description": "Bundle-relative resource path (references/..., assets/...). Omit for SKILL.md."},
            "section": {"type": "string", "description": "Optional Markdown heading to read."},
            "offset": {"type": "integer", "minimum": 0, "description": "Character offset within the selected content."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_LIMIT, "description": "Characters to return (default 8000; maximum 10000)."},
        },
        "required": ["name"],
    }

    def __init__(self, store: SkillStore, user_id: str, workspace=None):
        self.store = store
        self.user_id = user_id
        self.workspace = workspace

    async def execute(self, name: str, section: str | None = None, offset: int = 0, limit: int = DEFAULT_READ_LIMIT, path: str | None = None, materialize: bool = False, **_: Any) -> str:
        name = name.strip()
        skill = await self.store.readable_by_name(self.user_id, name)
        if skill is not None and skill.enabled:
            resolved_name, content = skill.name, skill.content
        else:
            builtin = get_builtin_skill(name)
            if builtin is None:
                return f"Error: skill '{name}' not found or disabled"
            resolved_name, content = builtin.name, builtin.content
        if path is not None:
            from claw.skills.bundles import resource
            try:
                if materialize:
                    import hashlib
                    from pathlib import Path
                    if self.workspace is None or not path or Path(path).suffix.lower() in {'.py', '.js', '.sh'}:
                        return "Error: this resource cannot be materialized"
                    raw = await resource(self.store, self.user_id, name, path, binary=True)
                    root = Path(self.workspace).resolve()
                    target = root / ('skill-asset-' + hashlib.sha256(raw).hexdigest()[:24] + Path(path).suffix.lower())
                    # Never follow an existing link or overwrite a workspace file.
                    try:
                        with target.open('xb') as out:
                            out.write(raw)
                    except FileExistsError:
                        if target.is_symlink() or target.read_bytes() != raw:
                            return "Error: resource destination already exists"
                    return f"Resource copied to /workspace/{target.name}"
                content = await resource(self.store, self.user_id, name, path)
            except ValueError as exc:
                return f"Error: {exc}"
        elif skill is not None and getattr(skill, 'bundle_id', None):
            content += "\n\nBundle resources (read_skill with path; relative to bundle root):\n" + "\n".join(skill.bundle_metadata.get('files', []))
            content = "Create static HTML with inline SVG/CSS. External fonts and scripts are disabled in chat previews. Use render_diagram to validate and export a PNG. Package scripts are reference-only; no install hooks or plugin commands are executed. For brand settings, create a project-local copy; never modify shared package files.\n\n" + content
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
        "Create, update, import, list, or delete reusable skills shown in Settings → Skills. "
        "Use 'import_github' for a public GitHub skill at an exact commit. Never clone or copy a "
        "repository into workspace/skills and never use 'save' to simulate a package import. "
        "Use 'save' only for a new plain-text skill authored in chat. "
        "Read the 'skill-creator' skill first for how to author a good skill."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["save", "import_github", "list", "delete"]},
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
            "repository": {"type": "string", "description": "Public https://github.com/owner/repository URL (for import_github)"},
            "commit": {"type": "string", "description": "Exact full 40-character commit SHA (for import_github)"},
        },
        "required": ["action"],
    }

    def __init__(self, store: SkillStore, user_id: str, workspace: Path | None = None):
        self.store = store
        self.user_id = user_id
        self.workspace = Path(workspace) if workspace is not None else None

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

        if action == "import_github":
            repository = str(kwargs.get("repository") or "").strip()
            commit = str(kwargs.get("commit") or "").strip()
            if not repository or not commit:
                return "Error: import_github requires 'repository' and a full 40-character 'commit' SHA."
            from claw.skills.bundles import install_bundle, parse_bundle, prepare_bundle
            from claw.skills.github import download_bundle
            try:
                data = await download_bundle(repository, commit)
                bundle = await asyncio.to_thread(parse_bundle, data, repository + "/tree/" + commit)
                bundle = await prepare_bundle(self.store, self.user_id, bundle)
                skill = await install_bundle(self.store, self.user_id, bundle)
            except (ValueError, UnicodeError) as exc:
                return f"Error: {exc}"
            except httpx.HTTPError:
                return "Error: GitHub download failed; ask the user to import a ZIP in Settings → Skills."
            except IntegrityError:
                return "Error: a skill with this name already exists."
            warnings = (skill.bundle_metadata or {}).get("warnings", [])
            warning_text = "\nWarnings:\n" + "\n".join(f"- {item}" for item in warnings) if warnings else ""
            return f"Skill bundle '{skill.name}' imported and enabled.{warning_text}"

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
            existing = await self.store.get_by_name(self.user_id, name)
            from claw.skills.bundles import plain_skill_save_error

            save_error = plain_skill_save_error(existing, self.workspace, name, content)
            if save_error:
                return f"Error: {save_error}"
            description = str(kwargs.get("description") or "").strip()
            enabled_raw = kwargs.get("enabled")
            enabled = True if enabled_raw is None else bool(enabled_raw)
            await self.store.upsert(
                self.user_id, name, description=description, content=content, enabled=enabled
            )
            state = "enabled" if enabled else "disabled"
            from claw.skills.bundles import reference_warnings
            warnings = await reference_warnings(self.store, self.user_id, name, content)
            warning_text = "\nWarnings:\n" + "\n".join(f"- {item}" for item in warnings) if warnings else ""
            return f"Skill '{name}' saved ({state}). It now appears in Settings → Skills.{warning_text}"

        if action == "delete":
            if not name:
                return "Error: delete requires a 'name'."
            existing = await self.store.get_by_name(self.user_id, name)
            if existing is None:
                return f"Error: skill '{name}' not found."
            from claw.skills.workspace import delete_skill_with_workspace

            try:
                deleted, archived = await delete_skill_with_workspace(
                    self.store, self.workspace, self.user_id, existing
                )
            except OSError:
                return "Error: the managed workspace directory could not be archived; the skill was not deleted."
            if not deleted:
                return f"Error: skill '{name}' could not be deleted."
            suffix = " Its PrivateClaw-managed workspace directory was archived." if archived else ""
            return f"Skill '{name}' deleted.{suffix}"

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
