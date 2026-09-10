"""Deterministic checks on immutable deliverables, separate from worker claims."""

import hashlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree

MAX_FILE_BYTES = 100 * 1024 * 1024


def check_file(path: Path, label: str) -> dict:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE_BYTES:
        raise ValueError(f"missing, empty or oversized deliverable: {label}")
    suffix = path.suffix.lower()
    if suffix in {".docx", ".xlsx", ".pptx"}:
        expected, tag = {
            ".docx": ("word/document.xml", "document"),
            ".xlsx": ("xl/workbook.xml", "workbook"),
            ".pptx": ("ppt/presentation.xml", "presentation"),
        }[suffix]
        try:
            with zipfile.ZipFile(path) as package:
                if sum(i.file_size for i in package.infolist()) > 200 * 1024 * 1024:
                    raise ValueError("Office expansion limit exceeded")
                if expected not in package.namelist() or package.testzip():
                    raise ValueError("invalid Office file")
                if package.getinfo(expected).file_size > 8 * 1024 * 1024:
                    raise ValueError("Office XML limit exceeded")
                root = ElementTree.fromstring(package.read(expected))
                if root.tag.rsplit("}", 1)[-1] != tag:
                    raise ValueError("invalid Office document structure")
        except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise ValueError(f"invalid Office file: {label}") from exc
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "kind": "file_structure",
        "path": label,
        "sha256": digest,
        "status": "passed",
        "bytes": path.stat().st_size,
        "scope": "existence_and_structure_only",
    }
