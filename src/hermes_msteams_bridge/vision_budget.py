"""Per-call vision spend cap — a sliding 60-second window.

Bounds vision-model cost across all consumers (look_at_screen, ambient push).
``max_per_minute <= 0`` means unlimited.
"""

from __future__ import annotations

import time


class VisionBudget:
    def __init__(self, max_per_minute: int = 30) -> None:
        self.max_per_minute = max_per_minute
        self._hits: list[float] = []
        # D2: the slice of the window ambient use may never eat into, so a
        # caller's explicit look_at_screen always has headroom even in a
        # screen-heavy meeting. A quarter of the cap, at least 2.
        self.explicit_reserve = max(2, max_per_minute // 4) if max_per_minute > 0 else 0

    def _used(self, now: float) -> int:
        cutoff = now - 60.0
        self._hits = [t for t in self._hits if t > cutoff]
        return len(self._hits)

    def try_consume(self, now: float | None = None) -> bool:
        """Record an EXPLICIT vision use (caller-requested) if under the cap."""
        if self.max_per_minute <= 0:
            return True
        now = time.monotonic() if now is None else now
        if self._used(now) >= self.max_per_minute:
            return False
        self._hits.append(now)
        return True

    def try_consume_ambient(self, now: float | None = None) -> bool:
        """Record an AMBIENT vision use — refuses once only the explicit
        reserve is left, so background frames can't starve look_at_screen."""
        if self.max_per_minute <= 0:
            return True
        now = time.monotonic() if now is None else now
        if self._used(now) >= self.max_per_minute - self.explicit_reserve:
            return False
        self._hits.append(now)
        return True

    def refund(self) -> None:
        """Give back the most recent hit (e.g. a consult failed before the model)."""
        if self._hits:
            self._hits.pop()
