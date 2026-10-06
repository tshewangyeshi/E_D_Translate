"""Store work runs off the event loop (S2.4). Requirements: NFR-100.

A slow database must hold up the request that is waiting on it, not every
request the process is serving.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from orchestrator.service.translate import SegmentIn
from orchestrator.testing.rig import make_rig


def test_s24_a_slow_store_holds_up_its_own_request_only() -> None:
    rig = make_rig()
    site = rig.sites.get("portal")
    assert site is not None
    real_lookup = rig.service.store.lookup

    def slow_lookup(items: Any) -> Any:
        time.sleep(0.4)  # a slow query, blocking its thread
        return real_lookup(items)

    rig.service.store.lookup = slow_lookup  # type: ignore[method-assign]

    async def four_requests() -> float:
        started = time.perf_counter()
        await asyncio.gather(
            *(
                rig.service.translate(site, f"c{n}", [SegmentIn("s0", f"Text {n}")], "/x")
                for n in range(4)
            )
        )
        return time.perf_counter() - started

    elapsed = asyncio.run(four_requests())
    assert elapsed < 1.2, f"requests queued behind each other: {elapsed:.2f}s for 4 x 0.4s"


# --- the Redis circuit breaker (S10.4, ER-21) ---


def test_er21_after_a_redis_failure_calls_skip_it_until_the_cooldown() -> None:
    from orchestrator.store.cache import Breaker, ResilientCache

    now = [0.0]
    calls = []

    class Down:
        def get_many(self, keys: Any) -> Any:
            calls.append("get")
            raise TimeoutError("redis down")

        def set(self, *args: Any) -> None:
            calls.append("set")
            raise TimeoutError("redis down")

        def delete_many(self, keys: Any) -> None:
            calls.append("delete")

    cache = ResilientCache(Down(), breaker=Breaker(cooldown=5, clock=lambda: now[0]))  # type: ignore[arg-type]
    assert cache.get_many(["k"]) == {}
    for _ in range(64):  # one page's worth of writes: none of them waits for Redis
        cache.set("k", None, 60)  # type: ignore[arg-type]
    assert calls == ["get"]
    assert cache.failures == 65  # every skipped call still counts towards the metric

    now[0] = 5.1  # cooled down: one call is let through to test Redis
    cache.delete_many(["k"])
    assert calls == ["get", "delete"]
    cache.set("k", None, 60)  # type: ignore[arg-type]  # Redis answered: closed again
    assert calls == ["get", "delete", "set"]
