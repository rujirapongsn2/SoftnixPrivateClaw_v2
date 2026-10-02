"""Portable, bounded Agent Skill bundles. Imported files are data, never executed."""

import base64
import hashlib
import io
import posixpath
import re
import stat
import uuid
import zipfile
from pathlib import Path
from pathlib import PurePosixPath

import yaml
from sqlalchemy import select

from claw.db.models import Skill, SkillBundleVersion, User

MAX_ARCHIVE = 12 * 1024 * 1024
MAX_EXPANDED = 24 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_BUNDLES_PER_USER = 20
MAX_SKILLS_PER_ARCHIVE = 20
MAX_BUNDLE_BYTES_PER_USER = 100 * 1024 * 1024
ALLOWED = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".htm",
    ".svg",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".woff",
    ".woff2",
    ".py",
    ".js",
    ".sh",
    ".mmd",
    ".excalidraw",
    ".drawio",
}

_WORKSPACE_SKILL_REF = re.compile(r"(?<![\w.-])skills/([^/\s`\"'<>()[\]]+)/", re.UNICODE)
_QUOTED_WORKSPACE_SKILL_REF = re.compile(r"[`\"']skills/([^/`\"'\r\n]+?)/", re.UNICODE)


def skill_path_references(content: str) -> set[str]:
    """Return legacy workspace skill names mentioned by instructions."""
    text = content or ""
    return {
        name.strip()
        for pattern in (_WORKSPACE_SKILL_REF, _QUOTED_WORKSPACE_SKILL_REF)
        for name in pattern.findall(text)
        if name.strip()
    }


async def reference_warnings(store, user_id: str, current_name: str, content: str) -> list[str]:
    """Warn about workspace-style references without rewriting user content."""
    warnings: list[str] = []
    available = {skill.name for skill in await store.enabled_for_user(user_id)}
    for referenced_name in sorted(skill_path_references(content))[:20]:
        if referenced_name == current_name:
            warnings.append(
                f"Reference skills/{referenced_name}/... points at the workspace. "
                "Imported package resources should use bundle-relative paths with read_skill(path=...)."
            )
        elif referenced_name not in available:
            warnings.append(
                f"Reference skills/{referenced_name}/... names a skill that is not registered or enabled."
            )
        else:
            warnings.append(
                f"Reference skills/{referenced_name}/... points at another skill; use read_skill for that skill instead."
            )
    return warnings


def plain_skill_save_error(existing, workspace: Path | None, name: str, content: str) -> str | None:
    """Reject plain-skill creation/content changes that imitate a package."""
    if existing is not None:
        if getattr(existing, "bundle_id", None) or content == existing.content:
            return None
    if workspace is not None:
        root = workspace.resolve() / "skills"
        candidate = root / name
        try:
            is_direct_directory = (
                not candidate.is_symlink()
                and candidate.is_dir()
                and candidate.resolve(strict=True).parent == root
            )
        except OSError:
            is_direct_directory = False
        if is_direct_directory:
            return (
                f"Workspace directory skills/{name} already exists. It is not a registered package; "
                "import the GitHub repository with import_github or archive the orphan in Settings."
            )
    if name in skill_path_references(content):
        return (
            f"New skill '{name}' depends on workspace/skills files. "
            "Import the package with import_github or ZIP upload instead."
        )
    return None


def skill_update_error(
    existing,
    workspace: Path | None,
    name: str,
    description: str,
    content: str,
) -> str | None:
    """Validate content separately from metadata-only skill updates."""
    if existing is not None and getattr(existing, "bundle_id", None):
        if content != existing.content or description != existing.description:
            return "Imported bundle instructions are read only"
        return None
    return plain_skill_save_error(existing, workspace, name, content)


def skill_update_values(
    existing,
    provided_fields: set[str],
    *,
    description: str,
    content: str,
    enabled: bool,
    visibility: str | None,
    connector_id: str | None,
) -> tuple[str, str, dict]:
    """Resolve immutable values and persist only fields supplied by an update."""
    creating = existing is None
    resolved_description = (
        description if creating or "description" in provided_fields else existing.description
    )
    resolved_content = content if creating or "content" in provided_fields else existing.content
    submitted = {
        "description": description,
        "content": content,
        "enabled": enabled,
        "visibility": visibility,
        "connector_id": connector_id,
    }
    updates = {
        field: value
        for field, value in submitted.items()
        if creating or field in provided_fields
    }
    return resolved_description, resolved_content, updates


async def prepare_bundle(store, user_id: str, bundle: dict) -> dict:
    """Attach non-blocking reference diagnostics before installation."""
    warnings = await reference_warnings(store, user_id, bundle["name"], bundle["content"])
    if warnings:
        # Keep the notes found while parsing (links to sibling skills) alongside the reference warnings.
        merged = [*bundle["metadata"].get("warnings", []), *warnings]
        bundle = {**bundle, "metadata": {**bundle["metadata"], "warnings": merged}}
    return bundle


def safe_path(path):
    p = PurePosixPath(path)
    if not path or "\\" in path or "\x00" in path or p.is_absolute() or ".." in p.parts or ":" in path:
        raise ValueError("Invalid bundle path")
    return str(p)


def parse_bundle(data: bytes, source="ZIP upload"):
    """Parse an archive that holds exactly one skill (what GitHub imports and the manage tool expect)."""
    return parse_bundles(data, source, single=True)[0]


def _skill_roots(skill_files: list[str]) -> list[str]:
    """Directories of the skills in an archive. A SKILL.md nested inside another skill's folder is a
    file of that skill, not a second skill; a SKILL.md at the archive root owns everything."""
    roots = sorted({str(PurePosixPath(p).parent) for p in skill_files})
    if "." in roots:
        return ["."]
    return [r for r in roots if not any(r != other and r.startswith(other + "/") for other in roots)]


def parse_bundles(data: bytes, source="ZIP upload", single: bool = False) -> list[dict]:
    """Parse an archive holding one or more skills, each in its own folder with a SKILL.md.

    The archive-wide limits (size, file count, paths, symlinks) apply to the whole ZIP; each skill is
    then validated on its own. Links from one skill to a sibling skill in the same ZIP are allowed
    (they come back as non-blocking warnings, since read_skill resolves paths per skill).

    `single=True` is the strict form for callers that mean one skill (GitHub import, the manage tool):
    any second SKILL.md in the archive, nested or not, is refused as ambiguous."""
    if len(data) > MAX_ARCHIVE:
        raise ValueError("ZIP exceeds 12 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 1500 or sum(i.file_size for i in entries) > MAX_EXPANDED:
                raise ValueError("Bundle exceeds file count or expanded size limit")
            paths = {}
            for info in entries:
                path = safe_path(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("Symlinks are not supported")
                if info.is_dir():
                    continue
                if path in paths:
                    raise ValueError("Duplicate archive path")
                paths[path] = info
            skill_files = [p for p in paths if PurePosixPath(p).name == "SKILL.md"]
            if not skill_files:
                raise ValueError("ZIP must contain a SKILL.md")
            if single and len(skill_files) != 1:
                raise ValueError("ZIP must contain exactly one SKILL.md")
            roots = _skill_roots(skill_files)
            if len(roots) > MAX_SKILLS_PER_ARCHIVE:
                raise ValueError(f"ZIP holds more than {MAX_SKILLS_PER_ARCHIVE} skills")
            digest = hashlib.sha256(data).hexdigest()
            return [_build_bundle(archive, paths, root, roots, digest, source) for root in roots]
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError("Invalid or encrypted ZIP") from exc


def _build_bundle(archive, paths: dict, root: str, roots: list[str], digest: str, source: str) -> dict:
    files = {}
    prefix = "" if root == "." else root + "/"
    for path, info in paths.items():
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix) :]
        if info.file_size > MAX_FILE:
            raise ValueError(f"File exceeds 2 MB: {relative}")
        if PurePosixPath(relative).suffix.lower() not in ALLOWED and PurePosixPath(
            relative
        ).name not in {"LICENSE", "NOTICE"}:
            raise ValueError(f"Unsupported file type: {relative}")
        files[relative] = base64.b64encode(archive.read(info)).decode("ascii")
    # Preserve the enclosing repository's redistribution notice too.
    for parent in PurePosixPath(root).parents:
        candidate = str(parent / "LICENSE")
        if "LICENSE" not in files and candidate in paths:
            if paths[candidate].file_size > MAX_FILE:
                raise ValueError("License file exceeds 2 MB")
            files["LICENSE"] = base64.b64encode(archive.read(paths[candidate])).decode("ascii")
    content = base64.b64decode(files["SKILL.md"]).decode("utf-8-sig")
    where = f" ({root})" if len(roots) > 1 else ""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.S)
    if not match:
        raise ValueError(f"SKILL.md needs YAML frontmatter with name and description{where}")
    try:
        meta = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML frontmatter in SKILL.md{where}") from exc
    if (
        not isinstance(meta, dict)
        or not isinstance(meta.get("name"), str)
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(meta.get("name", "")))
        or len(meta["name"]) > 64
    ):
        raise ValueError(f"Skill name must be kebab-case, at most 64 characters{where}")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Skill description is required{where}")
    warnings = []
    sibling_links: dict[str, set[str]] = {}  # sibling skill name -> files that link to it
    sibling_roots = [r for r in roots if r != root and r != "."]
    for path, encoded in files.items():
        if not path.endswith(".md"):
            continue
        text = base64.b64decode(encoded).decode("utf-8-sig")
        for link in re.findall(r"\]\(([^\s)]+)", text):
            link = link.split("#")[0].strip("<>")
            if not link or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", link):
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), link))
            if resolved in files:
                continue
            # A link that leaves this skill and lands on a sibling skill in the same ZIP is fine.
            outside = posixpath.normpath(posixpath.join(prefix, posixpath.dirname(path), link)) if root != "." else ""
            sibling = next((r for r in sibling_roots if outside == r or outside.startswith(r + "/")), None)
            if sibling is not None and outside in paths:
                sibling_links.setdefault(PurePosixPath(sibling).name, set()).add(path)
                continue
            warnings.append(f"{path}: missing reference {link}")
    if warnings:
        raise ValueError("; ".join(warnings[:5]))
    extra = meta.get("metadata") or {}
    if not isinstance(extra, dict) or not isinstance(extra.get("version", "1"), (str, int, float)):
        raise ValueError("Invalid skill version metadata")
    size_bytes = sum(len(base64.b64decode(encoded)) for encoded in files.values())
    metadata = {
        "source": source,
        "sha256": digest,
        "version": str(extra.get("version", "1")),
        "license": str(meta.get("license", "")),
        "files": sorted(files),
        "size_bytes": size_bytes,
    }
    nested = sorted(p for p in files if p != "SKILL.md" and PurePosixPath(p).name == "SKILL.md")
    if nested:
        # Never silently swallow what looks like another skill: say it was kept as a plain file.
        metadata.setdefault("warnings", []).append(
            f"contains {len(nested)} nested SKILL.md file(s) ({', '.join(nested[:3])}); they are kept as "
            "plain files of this skill, not imported as separate skills."
        )
    if sibling_links:
        # One short note per skill, not one per link.
        metadata.setdefault("warnings", []).append(
            "links to other skills in the same ZIP ("
            + ", ".join(sorted(sibling_links))
            + "); the relative links do not resolve at run time, so read them with read_skill(name=...)."
        )
    return dict(
        name=meta["name"],
        description=description[:500],
        content=content,
        files=files,
        metadata=metadata,
    )


async def install_bundle(store, user_id, bundle):
    """Create a new private skill; imports never silently overwrite existing skills."""
    return (await install_bundles(store, user_id, [bundle]))[0]


async def install_bundles(store, user_id, bundles: list[dict]):
    """Create several skills from one archive in a single transaction: either all are installed or none.

    One quota decision covers the whole set, and no name may clash with a built-in, an existing skill,
    or another skill in the same set."""
    from claw.core.builtin_skills import get_builtin_skill

    names = [bundle["name"] for bundle in bundles]
    for name in names:
        if get_builtin_skill(name):
            raise ValueError("This name is reserved by a built-in skill")
    if len(set(names)) != len(names):
        raise ValueError("The ZIP contains two skills with the same name")
    async with store.factory() as db:
        # Lock the owner row where supported so concurrent imports share one
        # quota decision instead of each independently passing it.
        await db.scalar(select(User).where(User.id == user_id).with_for_update())
        sources = (
            (
                await db.execute(
                    select(SkillBundleVersion.source)
                    .join(Skill, Skill.id == SkillBundleVersion.skill_id)
                    .where(Skill.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        if len(sources) + len(bundles) > MAX_BUNDLES_PER_USER:
            extra = f" (this ZIP has {len(bundles)})" if len(bundles) > 1 else ""
            raise ValueError(f"You can import up to {MAX_BUNDLES_PER_USER} skill bundles{extra}")
        used_bytes = sum(int((item or {}).get("size_bytes", 0)) for item in sources)
        incoming_bytes = sum(int(bundle["metadata"].get("size_bytes", 0)) for bundle in bundles)
        if used_bytes + incoming_bytes > MAX_BUNDLE_BYTES_PER_USER:
            limit_mb = MAX_BUNDLE_BYTES_PER_USER // (1024 * 1024)
            raise ValueError(f"Imported skill bundles exceed the {limit_mb} MB storage limit")
        taken = set(
            (await db.execute(select(Skill.name).where(Skill.user_id == user_id, Skill.name.in_(names)))).scalars()
        )
        if taken:
            if len(bundles) == 1:
                raise ValueError("A skill with this name already exists; choose a different package name")
            raise ValueError(
                "A skill with this name already exists; choose a different package name: "
                + ", ".join(sorted(taken))
            )
        skills = []
        for bundle in bundles:
            version_id = uuid.uuid4().hex
            skill = Skill(
                user_id=user_id,
                name=bundle["name"],
                description=bundle["description"],
                content=bundle["content"],
                enabled=True,
                bundle_id=version_id,
                bundle_metadata=bundle["metadata"],
            )
            db.add(skill)
            await db.flush()
            db.add(
                SkillBundleVersion(
                    id=version_id, skill_id=skill.id, files=bundle["files"], source=bundle["metadata"]
                )
            )
            skills.append(skill)
        await db.commit()
        return skills


async def resource(store, user_id, name, path, binary=False):
    skill = await store.readable_by_name(user_id, name)
    if skill is None or not skill.bundle_id:
        raise ValueError("Enabled skill bundle not found")
    async with store.factory() as db:
        version = await db.get(SkillBundleVersion, skill.bundle_id)
        if not version or version.skill_id != skill.id:
            raise ValueError("Bundle version not found")
        if not path:
            return "\n".join(sorted(version.files))
        encoded = version.files.get(safe_path(path))
        if encoded is None:
            raise ValueError("Resource not found")
        raw = base64.b64decode(encoded)
        if binary:
            return raw
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("Binary resource: use materialize=true to copy it into the workspace")
