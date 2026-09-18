"""Per-key token-bucket rate limiting for the keyless public routes (FR-103).

In-process only: each API replica limits independently. A shared Redis limiter
can replace this behind the same interface when more than one replica runs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    per_minute: float
    burst: float
    clock: Callable[[], float] = time.monotonic
    max_keys: int = 100_000
    _state: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> (tokens, updated)

    def allow(self, key: str) -> bool:
        now = self.clock()
        tokens, updated = self._state.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - updated) * self.per_minute / 60.0)
        if len(self._state) >= self.max_keys and key not in self._state:
            self._state.clear()  # bounded memory under a flood of distinct keys
        if tokens < 1.0:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1.0, now)
        return True

    def retry_after_seconds(self) -> int:
        return max(1, int(60.0 / self.per_minute))
