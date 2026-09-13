"""Stable, tenant-bound hostnames for project application ingress."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import uuid
from itertools import islice
from pathlib import Path

_PROJECT_SLUG = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")


def _owner_token(owner_id: str) -> str:
    return base64.b32encode(uuid.UUID(owner_id).bytes).decode("ascii").rstrip("=").lower()


def _decode_owner(token: str) -> str | None:
    try:
        raw = base64.b32decode(token.upper() + "=" * ((8 - len(token) % 8) % 8))
        return uuid.UUID(bytes=raw).hex
    except (ValueError, TypeError):
        return None


def _signature(secret_key: str, owner_id: str, project: str, port: int) -> str:
    message = f"project-ingress\0{owner_id}\0{project}\0{port}".encode()
    digest = hmac.new(secret_key.encode(), message, hashlib.sha256).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:16]


def project_ingress_host(settings, secret_key: str, owner_id: str, project: str) -> str | None:
    domain = settings.project_ingress_domain
    if not domain:
        return None
    if not _PROJECT_SLUG.fullmatch(project):
        return None
    # Keep the complete route in one DNS label. This is intentionally compatible
    # with one wildcard tunnel hostname and Cloudflare's *.apps.example.com TLS
    # coverage (wildcard certificates do not span multiple labels).
    prefix = project[:12].rstrip("-")
    route = f"{prefix}-{_owner_token(owner_id)}-{_signature(secret_key, owner_id, project, settings.project_ingress_port)}"
    return f"{route}.{domain}"


def project_ingress_url(settings, secret_key: str, owner_id: str, project: str) -> str | None:
    host = project_ingress_host(settings, secret_key, owner_id, project)
    return f"{settings.project_ingress_scheme}://{host}" if host else None


def resolve_project_ingress_host(
    host: str, settings, secret_key: str, workspaces_root: Path
) -> tuple[str, str] | None:
    """Resolve only a signed route whose project directory still exists."""
    domain = settings.project_ingress_domain
    hostname = host.partition(":")[0].lower().rstrip(".")
    suffix = f".{domain}"
    if not domain or not hostname.endswith(suffix):
        return None
    route = hostname[: -len(suffix)]
    try:
        prefix, owner_token, signature = route.rsplit("-", 2)
    except ValueError:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,11}", prefix):
        return None
    owner_id = _decode_owner(owner_token)
    if owner_id is None:
        return None
    root = workspaces_root.resolve()
    projects = root / owner_id / "projects"
    if projects.is_symlink() or not projects.is_dir() or not projects.resolve().is_relative_to(root):
        return None
    # Project quotas keep managed inventories small. Prefix filtering avoids
    # hashing unrelated folders while retaining support for 48-character slugs.
    for path in islice(projects.glob(f"{prefix}*"), 2000):
        project = path.name
        if (
            not project.startswith(prefix) or not _PROJECT_SLUG.fullmatch(project)
            or path.is_symlink() or not path.is_dir()
        ):
            continue
        expected = _signature(secret_key, owner_id, project, settings.project_ingress_port)
        expected_host = project_ingress_host(settings, secret_key, owner_id, project)
        if hmac.compare_digest(signature, expected) and expected_host and hmac.compare_digest(hostname, expected_host):
            return owner_id, project
    return None
