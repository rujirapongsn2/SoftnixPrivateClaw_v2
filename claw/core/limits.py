"""Per-user fixed-window rate limiter (in-memory)."""

import time


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._windows: dict[str, tuple[int, int]] = {}  # user_id -> (window_start_minute, count)

    def allow(self, user_id: str) -> bool:
        if self.per_minute <= 0:
            return True
        minute = int(time.time() // 60)
        start, count = self._windows.get(user_id, (minute, 0))
        if start != minute:
            start, count = minute, 0
        if count >= self.per_minute:
            self._windows[user_id] = (start, count)
            return False
        self._windows[user_id] = (start, count + 1)
        # Opportunistic cleanup so the map cannot grow unbounded.
        if len(self._windows) > 10_000:
            self._windows = {u: v for u, v in self._windows.items() if v[0] == minute}
        return True
