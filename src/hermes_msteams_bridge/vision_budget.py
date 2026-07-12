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

    def try_consume(self, now: float | None = None) -> bool:
        """Record a vision use if under the per-minute limit; return success."""
        if self.max_per_minute <= 0:
            return True
        now = time.monotonic() if now is None else now
        cutoff = now - 60.0
        self._hits = [t for t in self._hits if t > cutoff]
        if len(self._hits) >= self.max_per_minute:
            return False
        self._hits.append(now)
        return True

    def refund(self) -> None:
        """Give back the most recent hit (e.g. a consult failed before the model)."""
        if self._hits:
            self._hits.pop()
