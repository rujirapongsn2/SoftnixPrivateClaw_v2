"""Per-key asyncio locks, bounded.

Several places need "one writer at a time, per session" — the turn runtime, the
message store's sequence allocation, a delegation mirroring into a bot's own
thread. Each of them was growing its own dict of locks, and a dict keyed by
session in a process that never restarts is a leak, so the bounding is here
rather than copied three times.
"""

import asyncio
from collections import OrderedDict

_DEFAULT_LIMIT = 2048


class KeyedLocks:
    def __init__(self, limit: int = _DEFAULT_LIMIT):
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._limit = limit

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._locks.move_to_end(key)
        # Only locks nobody holds may be dropped: evicting a held one hands the
        # next caller a fresh lock and the mutual exclusion silently stops.
        while len(self._locks) > self._limit:
            stale = next(
                (k for k in self._locks if k != key and not self._locks[k].locked()), None
            )
            if stale is None:
                break
            del self._locks[stale]
        return lock
