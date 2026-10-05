"""Upstream quota manager (FR-156, ER-O7).

A token bucket sized to the measured WSO2 rate limit (S0.2). A share of the
capacity is reserved for the background worker; live request-path attempts
may only use the rest, so traffic spikes cannot starve the worker and leave
the cache permanently cold.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    rate: float  # tokens per second
    capacity: float
    tokens: float
    updated: float

    def take(self, n: int, now: float) -> bool:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def available(self, now: float) -> float:
        """Tokens that could be taken now. Looking does not refill or spend."""
        return min(self.capacity, self.tokens + (now - self.updated) * self.rate)


@dataclass
class QuotaManager:
    requests_per_second: float
    worker_share: float = 0.5  # reserved for the worker, never used live
    burst_seconds: float = 1.0
    clock: Callable[[], float] = time.monotonic
    _live: _Bucket = field(init=False)
    _worker: _Bucket = field(init=False)
    #: Requests plan in threads (store calls run off the event loop), so two
    #: can take tokens at once; without this they could both spend the last one.
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not 0.0 <= self.worker_share <= 1.0:
            raise ValueError("worker_share must be between 0 and 1")
        now = self.clock()
        live_rate = self.requests_per_second * (1 - self.worker_share)
        worker_rate = self.requests_per_second * self.worker_share
        self._live = _Bucket(
            live_rate, live_rate * self.burst_seconds, live_rate * self.burst_seconds, now
        )
        self._worker = _Bucket(
            worker_rate, worker_rate * self.burst_seconds, worker_rate * self.burst_seconds, now
        )

    def try_live(self, n: int = 1) -> bool:
        """Tokens for a live (request-path) upstream call; False means enqueue instead."""
        with self._lock:
            return self._live.take(n, self.clock())

    def try_worker(self, n: int = 1) -> bool:
        with self._lock:
            return self._worker.take(n, self.clock())

    def snapshot(self) -> dict[str, object]:
        """For health reporting (FR-610). Each process has its own buckets."""
        now = self.clock()
        return {
            "live_tokens": round(self._live.available(now), 2),
            "live_per_second": self._live.rate,
            "worker_tokens": round(self._worker.available(now), 2),
            "worker_per_second": self._worker.rate,
        }
