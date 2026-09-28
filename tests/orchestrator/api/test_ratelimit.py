"""Rate limiting under memory pressure and address rotation (FR-103, NFR-304).

These pin the two properties the 2026-09-28 security audit found missing. The
arithmetic of a token bucket is the easy part; what the audit showed is that a
limiter can be arithmetically correct and still fail to bound anyone.
"""

from __future__ import annotations

import pytest

from orchestrator.api.ratelimit import RateLimiter, client_bucket
from orchestrator.testing.rig import FakeClock


def limiter(**kw: object) -> RateLimiter:
    clock = FakeClock()
    defaults: dict[str, object] = {"per_minute": 60.0, "burst": 2.0, "clock": clock}
    return RateLimiter(**{**defaults, **kw})  # type: ignore[arg-type]


def exhaust(rl: RateLimiter, key: str) -> None:
    while rl.allow(key):
        pass


class TestBucket:
    def test_fr103_burst_then_refusal(self) -> None:
        rl = limiter(burst=2.0)
        assert rl.allow("a") and rl.allow("a")
        assert not rl.allow("a")

    def test_fr103_tokens_refill_over_time(self) -> None:
        clock = FakeClock()
        rl = RateLimiter(per_minute=60.0, burst=1.0, clock=clock)
        assert rl.allow("a")
        assert not rl.allow("a")
        clock.now += 1.0  # 60/min = one token per second
        assert rl.allow("a")

    def test_fr103_keys_are_independent(self) -> None:
        rl = limiter(burst=1.0)
        assert rl.allow("a")
        assert not rl.allow("a")
        assert rl.allow("b")


class TestMemoryPressure:
    """A throttled caller must not be released by unrelated traffic."""

    def test_fr103_an_active_penalty_survives_a_flood_of_new_keys(self) -> None:
        """Counted, not sampled.

        Asking "is it still throttled?" at the end proves little: a caller
        released by a wholesale clear is re-throttled by its very next request,
        so the final answer looks identical either way. What differs is how many
        requests got through in between. With a frozen clock nothing refills, so
        a correct limiter allows exactly ``burst`` requests for all time.
        """
        rl = limiter(max_keys=8, burst=1.0)  # FakeClock does not advance on its own
        allowed = 1 if rl.allow("attacker") else 0

        for n in range(200):
            rl.allow(f"passer-by-{n}")  # unrelated traffic, never seen again
            if rl.allow("attacker"):
                allowed += 1

        assert allowed == 1, (
            f"the attacker got {allowed} requests through against a burst of 1: unrelated "
            "traffic released the throttle. Eviction must be least-recently-used, "
            "never a wholesale clear."
        )

    def test_fr103_memory_stays_bounded(self) -> None:
        rl = limiter(max_keys=8, burst=1.0)
        for n in range(500):
            rl.allow(f"key-{n}")
        assert len(rl._state) <= 8

    def test_fr103_the_coldest_key_is_the_one_evicted(self) -> None:
        rl = limiter(max_keys=3, burst=1.0)
        for key in ("cold", "warm", "hot"):
            rl.allow(key)
        rl.allow("warm")
        rl.allow("hot")
        rl.allow("new")  # capacity reached: something must go
        assert "cold" not in rl._state
        assert {"warm", "hot", "new"} <= set(rl._state)

    def test_fr103_an_evicted_caller_is_not_remembered(self) -> None:
        """Honest about the residual limit: eviction does restore a burst."""
        rl = limiter(max_keys=2, burst=1.0)
        exhaust(rl, "gone")
        for n in range(5):
            rl.allow(f"other-{n}")
        assert rl.allow("gone")  # no longer tracked, so it starts fresh


class TestClientBucket:
    """A new bucket must cost more than a new address."""

    def test_nfr304_one_ipv6_allocation_is_one_client(self) -> None:
        same = [
            "2001:db8:abcd:1234::1",
            "2001:db8:abcd:1234::2",
            "2001:db8:abcd:1234:ffff:ffff:ffff:ffff",
        ]
        buckets = {client_bucket(a) for a in same}
        assert len(buckets) == 1, "addresses in one /64 must share a bucket"

    def test_nfr304_separate_allocations_stay_separate(self) -> None:
        assert client_bucket("2001:db8:abcd:1234::1") != client_bucket("2001:db8:abcd:9999::1")

    def test_fr103_rotating_within_a_64_does_not_buy_a_new_burst(self) -> None:
        rl = limiter(burst=1.0)
        assert rl.allow(client_bucket("2001:db8::1"))
        assert not rl.allow(client_bucket("2001:db8::2"))
        assert not rl.allow(client_bucket("2001:db8::dead:beef"))

    @pytest.mark.parametrize("host", ["203.0.113.10", "198.51.100.7"])
    def test_fr103_ipv4_addresses_are_their_own_bucket(self, host: str) -> None:
        assert client_bucket(host) == host

    @pytest.mark.parametrize("host", ["unknown", "proxy.internal", ""])
    def test_fr103_unparseable_hosts_keep_their_own_bucket(self, host: str) -> None:
        """Never collapse unknown peers together: that would be one shared limit."""
        assert client_bucket(host) == host
