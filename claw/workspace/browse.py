"""Search, filter, sort and page a user's workspace listing for the Files view.

The listing is a walk of the workspace on disk, not a database index: sandbox
commands create files without going through the API, so an index would drift.
One walk is cached per user for a short TTL so typing in the search box or
scrolling to the next page does not walk the disk again.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import PurePosixPath

KINDS: dict[str, frozenset[str]] = {
    "document": frozenset({".pdf", ".docx", ".doc", ".md", ".txt", ".rtf", ".odt"}),
    "spreadsheet": frozenset({".xlsx", ".xls", ".csv", ".tsv", ".ods"}),
    "presentation": frozenset({".pptx", ".ppt", ".odp"}),
    "web": frozenset({".html", ".htm"}),
    "image": frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}),
    "card": frozenset({".a2ui"}),
    "media": frozenset({".mp4", ".webm", ".mov", ".m4v", ".mp3", ".wav", ".ogg", ".m4a"}),
}
OTHER = "other"
_SEPARATORS = re.compile(r"[\s._-]+")
_KIND_BY_SUFFIX = {suffix: kind for kind, suffixes in KINDS.items() for suffix in suffixes}

SORTS: dict[str, tuple[Callable[[dict], object], bool]] = {
    "recent": (lambda f: f["modified_at"], True),
    "name": (lambda f: PurePosixPath(f["path"]).name.casefold(), False),
    "size": (lambda f: f["size"], True),
}

CACHE_TTL_S = 30.0
_CACHE_USERS = 256


def kind_of(path: str) -> str:
    return _KIND_BY_SUFFIX.get(PurePosixPath(path).suffix.lower(), OTHER)


class ListingCache:
    """Last walk per (mode, owner), bounded in both age and number of users.

    One walk at a time per key: concurrent misses wait for the walk already
    running instead of each walking the disk. A walk that overlaps a forget()
    (a trash or restore) is returned to its caller but not cached, so the
    listing from before the change cannot be put back."""

    def __init__(self, ttl: float = CACHE_TTL_S, max_users: int = _CACHE_USERS):
        self._ttl = ttl
        self._max = max_users
        self._entries: OrderedDict[tuple[str, str], tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()
        # One small lock and counter per user seen; kept, not evicted, so an
        # eviction can never reset a counter a running walk compares against.
        self._walk_locks: dict[tuple[str, str], threading.Lock] = {}
        self._generations: dict[tuple[str, str], int] = {}

    def get(self, key: tuple[str, str], walk: Callable[[], dict], *, fresh: bool = False) -> dict:
        asked = time.monotonic()
        with self._lock:
            walk_lock = self._walk_locks.setdefault(key, threading.Lock())
        with walk_lock:
            with self._lock:
                hit = self._entries.get(key)
                # A walk that finished while this call waited is as fresh as one it would run.
                if hit and (hit[0] >= asked or (not fresh and time.monotonic() - hit[0] < self._ttl)):
                    self._entries.move_to_end(key)
                    return hit[1]
                generation = self._generations.get(key, 0)
            started = time.monotonic()
            listing = walk()
            with self._lock:
                if self._generations.get(key, 0) == generation:
                    self._entries[key] = (started, listing)
                    self._entries.move_to_end(key)
                    while len(self._entries) > self._max:
                        self._entries.popitem(last=False)
            return listing

    def forget(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._entries.pop(key, None)
            self._generations[key] = self._generations.get(key, 0) + 1


def _words(text: str) -> str:
    """Case-folded text with -, _, . and runs of spaces read as one space, so
    "Q3 sales" finds sales/q3-summary.html."""
    return _SEPARATORS.sub(" ", text.casefold())


def browse(
    files: list[dict],
    *,
    q: str,
    kind: str,
    sort: str,
    offset: int,
    limit: int,
    since: float | None = None,
    zone: str = "",
) -> dict:
    """One page of `files`, plus per-kind counts for the current search so the
    filter chips can say how many each would show. Entries already carry
    "kind" and are shared with the cache, so they are never mutated here.

    Every search term must appear somewhere in the path, in any order. Files
    whose own name holds every term come before ones matched through their
    folder; within each group the chosen sort applies."""
    terms = _words(q).split()
    matched = [
        f for f in files
        if (since is None or f["modified_at"] >= since)
        and (not zone or f["zone"] == zone)
        and all(t in _words(f["path"]) for t in terms)
    ]
    counts: dict[str, int] = {}
    for f in matched:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    if kind:
        matched = [f for f in matched if f["kind"] == kind]
    key, reverse = SORTS[sort]
    matched.sort(key=key, reverse=reverse)
    if terms:
        # Stable, so the chosen order holds inside each group.
        matched.sort(key=lambda f: not all(t in _words(PurePosixPath(f["path"]).name) for t in terms))
    page = matched[offset : offset + limit]
    end = offset + len(page)
    return {
        "files": page,
        "total": len(matched),
        "counts": counts,
        "next_offset": end if end < len(matched) else None,
    }
