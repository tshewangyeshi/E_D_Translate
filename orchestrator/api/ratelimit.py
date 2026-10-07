"""Per-key token-bucket rate limiting for the keyless public routes (FR-103).

``RateLimiter`` is in-process: each API replica limits on its own.
``SharedRateLimiter`` keeps the buckets in Redis, so the limit holds across
replicas; the API uses it whenever Redis is configured.

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
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

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


#: One token bucket, updated atomically in Redis on Redis's own clock, so every
#: replica sees the same bucket and no two can spend the same token.
_BUCKET = """
local now = redis.call('TIME')
now = tonumber(now[1]) + tonumber(now[2]) / 1000000
local rate, burst, ttl = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local state = redis.call('HMGET', KEYS[1], 't', 'u')
local tokens = tonumber(state[1]) or burst
local updated = tonumber(state[2]) or now
tokens = math.min(burst, tokens + math.max(0, now - updated) * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 't', tostring(tokens), 'u', tostring(now))
redis.call('EXPIRE', KEYS[1], ttl)
return allowed
"""


class SharedRateLimiter:
    """``RateLimiter`` across every API replica, in Redis (S2.1).

    In-process buckets let a client spend the full limit once per replica.
    Here each bucket lives in Redis, under a salted hash of its key, never
    the key itself: a client address used for limiting is never written
    down (NFR-303). A bucket expires once it would be full again.

    If Redis fails, the call falls back to this process's own limiter, so
    limiting degrades to per-replica rather than disappearing.
    """

    def __init__(
        self,
        client: Any,
        name: str,
        *,
        per_minute: float,
        burst: float,
        key_hash: Callable[[str], str],
    ) -> None:
        self.name = name
        self.per_minute = per_minute
        self.burst = burst
        self._key_hash = key_hash
        self._script = client.register_script(_BUCKET)
        self._rate = per_minute / 60.0
        self._ttl = max(1, math.ceil(burst / self._rate) + 1)
        self.fallback = RateLimiter(per_minute=per_minute, burst=burst)
        self.failures = 0

    def allow(self, key: str) -> bool:
        redis_key = f"dzweb:ratelimit:{self.name}:{self._key_hash(key)}"
        try:
            return bool(self._script(keys=[redis_key], args=[self._rate, self.burst, self._ttl]))
        except Exception as err:  # noqa: BLE001 - limiting must outlive a Redis fault
            self.failures += 1
            if self.failures == 1 or self.failures % 1000 == 0:
                log.warning("shared rate limiter %s: %s", self.name, type(err).__name__)
            return self.fallback.allow(key)

    def retry_after_seconds(self) -> int:
        return max(1, int(60.0 / self.per_minute))
