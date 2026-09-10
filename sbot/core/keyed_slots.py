"""Per-key capacity; retain entries while held OR awaited and discard at idle."""

import asyncio
from contextlib import asynccontextmanager


class KeyedSlots:
    def __init__(self, capacity):
        self.capacity = capacity
        self._entries = {}

    @asynccontextmanager
    async def hold(self, key):
        entry = self._entries.setdefault(key, [asyncio.Semaphore(self.capacity), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self._entries[key]
