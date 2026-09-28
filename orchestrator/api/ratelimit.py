"""Per-key token-bucket rate limiting for the keyless public routes (FR-103).

In-process only: each API replica limits independently. A shared Redis limiter
can replace this behind the same interface when more than one replica runs.

Two properties matter beyond the arithmetic, both found by the 2026-09-28 audit:

* **A penalty must survive memory pressure.** The table is bounded, so something
  has to go when it is full. Evicting everything throws away exactly the entries
  worth keeping -- a caller currently over its limit -- and hands it a fresh
  burst. Eviction is therefore least-recently-used: a caller being throttled is
  by definition sending requests, so it stays at the young end and survives.

* **A bucket must be expensive to walk away from.** Keying on the full client
  address makes a new bucket as cheap as a new address, and a single IPv6 /64 is
  effectively unlimited addresses. ``client_bucket`` folds an IPv6 address to its
  /64, so one allocation is one bucket. Addresses inside a /64 are usually one
  subscriber, so this groups what is in practice one customer.

Neither property helps while every request arrives from a reverse proxy and
therefore shares one address. That is the separate trusted-proxy work in
TODOS.md, which decides what the client key means before any of this applies.
"""

from __future__ import annotations

import ipaddress
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

IPV6_BUCKET_PREFIX = 64


def client_bucket(host: str) -> str:
    """The rate-limiting identity of a peer address.

    IPv6 folds to its /64 network; IPv4 and anything unparseable are returned
    unchanged, so a hostname or the ``unknown`` placeholder still gets a bucket
    rather than silently sharing one with everybody else.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/{IPV6_BUCKET_PREFIX}", strict=False))
    return str(address)


@dataclass
class RateLimiter:
    per_minute: float
    burst: float
    clock: Callable[[], float] = time.monotonic
    max_keys: int = 100_000
    # Ordered oldest-first by last use, so eviction can take the coldest entry.
    _state: OrderedDict[str, tuple[float, float]] = field(default_factory=OrderedDict)

    def allow(self, key: str) -> bool:
        now = self.clock()
        tokens, updated = self._state.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - updated) * self.per_minute / 60.0)
        if key not in self._state:
            while len(self._state) >= self.max_keys:
                self._state.popitem(last=False)  # coldest key, never the active ones
        self._state[key] = (tokens - 1.0, now) if tokens >= 1.0 else (tokens, now)
        self._state.move_to_end(key)
        return tokens >= 1.0

    def retry_after_seconds(self) -> int:
        return max(1, int(60.0 / self.per_minute))
