"""Per-user fixed-window rate limiter (in-memory)."""

import time


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._windows: dict[str, tuple[int, int]] = {}  # user_id -> (window_start_minute, count)

    def allow(self, user_id: str, per_minute: int | None = None) -> bool:
        """Consume one slot for `user_id`. `per_minute` overrides the default
        cap for this call (used to apply a per-user usage-plan limit on top of
        the shared limiter); None uses the limiter's configured default. A cap
        <= 0 means unlimited."""
        cap = self.per_minute if per_minute is None else per_minute
        if cap <= 0:
            return True
        minute = int(time.time() // 60)
        start, count = self._windows.get(user_id, (minute, 0))
        if start != minute:
            start, count = minute, 0
        if count >= cap:
            self._windows[user_id] = (start, count)
            return False
        self._windows[user_id] = (start, count + 1)
        # Opportunistic cleanup so the map cannot grow unbounded.
        if len(self._windows) > 10_000:
            self._windows = {u: v for u, v in self._windows.items() if v[0] == minute}
        return True
