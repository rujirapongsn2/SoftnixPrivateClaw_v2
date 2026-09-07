"""Portable Unicode filenames with extension and UTF-8 byte limits preserved."""

import unicodedata
from pathlib import Path


def safe_filename(name: str, fallback: str = "file") -> str:
    base = Path((name or fallback).replace("\\", "/")).name
    suffix = Path(base).suffix.lower()
    if len(suffix.encode("utf-8")) > 30:
        suffix = ""
    stem = Path(base).stem if suffix else base
    stem = (
        "".join(
            c if c.isalnum() or unicodedata.category(c).startswith("M") or c in "._-" else "_" for c in stem
        ).strip("._-")
        or fallback
    )
    stem = stem.encode("utf-8")[: 220 - len(suffix.encode("utf-8"))].decode("utf-8", "ignore")
    return stem + suffix
