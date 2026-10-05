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
