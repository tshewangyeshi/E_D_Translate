"""Hot cache (Redis) and distinct-client counter. Requirements: FR-151, FR-143, NFR-410, NFR-304.

The cache is an optimisation, never a dependency: every call on
:class:`ResilientCache` returns a safe empty answer when the backend fails
(FR-151, NFR-410), and counts the failure for metrics.

Values carry their origin (ER-1): the lookup service rejects machine output
for Tier 1 even on a cache hit.

The distinct-client counter is scoped to one UTC day, the same day the client
hash's salt lives for. A count that outlived the salt would see one citizen
who came back the next morning as two citizens, and after three mornings
their personalised text would count as widely seen and go to the model.

The day comes from the client hash itself (``day_of``), not from the clock at
the moment of counting: a request that straddles midnight is hashed with one
day's salt and must be counted in that same day's set.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from orchestrator.store.models import Origin, Stored

log = logging.getLogger(__name__)

APPROVED_NS = "dzweb:a:"
MACHINE_NS = "dzweb:m:"
SEEN_NS = "dzweb:seen:"


def utc_today() -> date:
    return datetime.now(UTC).date()


def day_of(client_hash: str, today: Callable[[], date] = utc_today) -> date:
    """The UTC day a client hash was made on (``YYYY-MM-DD:<hex>``), or today."""
    prefix, sep, _ = client_hash.partition(":")
    if sep:
        try:
            return date.fromisoformat(prefix)
        except ValueError:
            pass
    return today()


def encode(value: Stored) -> str:
    return json.dumps(
        {
            "v": value.version_id,
            "o": value.origin.value,
            "t": value.masked_target,
            "r": value.review,
        },
        ensure_ascii=False,
    )


def decode(raw: str | bytes) -> Stored | None:
    try:
        data = json.loads(raw)
        # An entry written before "r" existed reads as not-yet-flagged: a
        # Tier 2 serve then flags it once and rewrites the entry.
        return Stored(int(data["v"]), Origin(data["o"]), str(data["t"]), data.get("r") is True)
    except (ValueError, KeyError, TypeError):
        return None  # corrupt entries are misses, never errors


class Cache(Protocol):
    def get_many(self, keys: Sequence[str]) -> dict[str, Stored]: ...

    def set(self, key: str, value: Stored, ttl_seconds: int | None) -> None: ...

    def delete_many(self, keys: Sequence[str]) -> None: ...


class SeenCounter(Protocol):
    def observe(self, segment_key: str, client_hash: str) -> int:
        """Record a client for a segment; return how many distinct clients saw it today."""
        ...


class InMemoryCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get_many(self, keys: Sequence[str]) -> dict[str, Stored]:
        out = {}
        for k in keys:
            if k in self.data and (v := decode(self.data[k])) is not None:
                out[k] = v
        return out

    def set(self, key: str, value: Stored, ttl_seconds: int | None) -> None:
        self.data[key] = encode(value)

    def delete_many(self, keys: Sequence[str]) -> None:
        for k in keys:
            self.data.pop(k, None)


class InMemorySeenCounter:
    def __init__(self, today: Callable[[], date] = utc_today) -> None:
        self.today = today
        self.seen: dict[str, set[str]] = {}
        self._day: date | None = None

    def observe(self, segment_key: str, client_hash: str) -> int:
        day = day_of(client_hash, self.today)
        if day != self._day:
            self._day, self.seen = day, {}  # yesterday's hashes used yesterday's salt
        clients = self.seen.setdefault(segment_key, set())
        clients.add(client_hash)
        return len(clients)


class RedisCache:
    """Redis backend. Wrap in :class:`ResilientCache`; this class may raise."""

    def __init__(self, client: Any) -> None:  # redis.Redis; Any keeps redis optional at import
        self.client = client

    def get_many(self, keys: Sequence[str]) -> dict[str, Stored]:
        if not keys:
            return {}
        raws = self.client.mget(list(keys))
        out = {}
        for k, raw in zip(keys, raws, strict=True):
            if raw is not None and (v := decode(raw)) is not None:
                out[k] = v
        return out

    def set(self, key: str, value: Stored, ttl_seconds: int | None) -> None:
        self.client.set(key, encode(value), ex=ttl_seconds)

    def delete_many(self, keys: Sequence[str]) -> None:
        if keys:
            self.client.delete(*keys)


class RedisSeenCounter:
    def __init__(
        self,
        client: Any,
        ttl_seconds: int = 86_400,
        today: Callable[[], date] = utc_today,
    ) -> None:
        self.client = client
        self.ttl = ttl_seconds
        self.today = today

    def observe(self, segment_key: str, client_hash: str) -> int:
        # The day is part of the key, so refreshing the TTL on every sighting
        # cannot carry a count across the salt's rotation.
        key = f"{SEEN_NS}{day_of(client_hash, self.today).isoformat()}:{segment_key}"
        pipe = self.client.pipeline()
        pipe.sadd(key, client_hash)
        pipe.expire(key, self.ttl)
        pipe.scard(key)
        return int(pipe.execute()[-1])


@dataclass
class ResilientCache:
    """Never raises (FR-151, NFR-410). Failures read as misses and are counted."""

    inner: Cache
    failures: int = 0

    def _failed(self, op: str, err: Exception) -> None:
        self.failures += 1
        log.warning("cache %s failed; degrading to TM/live: %s", op, type(err).__name__)

    def get_many(self, keys: Sequence[str]) -> dict[str, Stored]:
        try:
            return self.inner.get_many(keys)
        except Exception as err:  # noqa: BLE001 - any backend failure degrades
            self._failed("get", err)
            return {}

    def set(self, key: str, value: Stored, ttl_seconds: int | None) -> None:
        try:
            self.inner.set(key, value, ttl_seconds)
        except Exception as err:  # noqa: BLE001
            self._failed("set", err)

    def delete_many(self, keys: Sequence[str]) -> None:
        try:
            self.inner.delete_many(keys)
        except Exception as err:  # noqa: BLE001
            self._failed("delete", err)


@dataclass
class ResilientSeenCounter:
    """When the counter is unavailable, report 0 so nothing is persisted (fail safe, NFR-304)."""

    inner: SeenCounter
    failures: int = 0

    def observe(self, segment_key: str, client_hash: str) -> int:
        try:
            return self.inner.observe(segment_key, client_hash)
        except Exception as err:  # noqa: BLE001
            self.failures += 1
            log.warning("seen-counter failed; not persisting: %s", type(err).__name__)
            return 0
