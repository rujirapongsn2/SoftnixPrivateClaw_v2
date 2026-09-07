"""Short-lived codes that link an external channel account to a Claw user.

A logged-in web user requests a code, then sends it from the channel (e.g.
`/link ABC123` to the Telegram bot). Codes are single-use and expire, held in
memory — losing them on restart is harmless since they're short-lived.
"""

import secrets
import time

# Typeable alphabet without ambiguous characters (no 0/O/1/I).
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _generate(length: int = 6) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


class LinkCodeService:
    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = ttl_seconds
        self._codes: dict[str, tuple[str, float]] = {}  # code -> (user_id, expires_at)

    def create(self, user_id: str) -> str:
        self._prune()
        code = _generate()
        self._codes[code] = (user_id, time.time() + self.ttl_seconds)
        return code

    def consume(self, code: str) -> str | None:
        """Return the user_id for a valid, unexpired code and invalidate it."""
        entry = self._codes.pop((code or "").strip().upper(), None)
        if entry is None:
            return None
        user_id, expires_at = entry
        if time.time() > expires_at:
            return None
        return user_id

    def _prune(self) -> None:
        now = time.time()
        for code in [c for c, (_, exp) in self._codes.items() if exp < now]:
            self._codes.pop(code, None)
