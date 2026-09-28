"""Show the rate-limiter fix by running the old and new behaviour side by side.

    python tools/demo_ratelimit.py

Found by /cso on 2026-09-28. The old limiter bounded memory by clearing the
whole table; this replays the same attack against both and counts what gets
through. Nothing here touches the network -- it is the limiter in isolation.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from orchestrator.api.ratelimit import RateLimiter, client_bucket


@dataclass
class OldRateLimiter:
    """The limiter as it was before the fix, kept here only to be beaten."""

    per_minute: float
    burst: float
    clock: Callable[[], float]
    max_keys: int = 100_000
    _state: dict[str, tuple[float, float]] = field(default_factory=dict)

    def allow(self, key: str) -> bool:
        now = self.clock()
        tokens, updated = self._state.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - updated) * self.per_minute / 60.0)
        if len(self._state) >= self.max_keys and key not in self._state:
            self._state.clear()  # <-- the defect
        if tokens < 1.0:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1.0, now)
        return True


def frozen() -> float:
    """A clock that never advances, so no tokens ever refill."""
    return 1000.0


def attack(limiter: OldRateLimiter | RateLimiter, rounds: int = 200) -> int:
    """One attacker, plus unrelated traffic that pushes the table over capacity."""
    allowed = 1 if limiter.allow("attacker") else 0
    for n in range(rounds):
        limiter.allow(f"passer-by-{n}")
        if limiter.allow("attacker"):
            allowed += 1
    return allowed


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    burst, cap = 1.0, 8
    print("A caller with a burst of 1 and a clock that never advances.")
    print(f"It should get through exactly ONCE, no matter what else happens (cap {cap}).\n")

    old = attack(OldRateLimiter(per_minute=60.0, burst=burst, clock=frozen, max_keys=cap))
    new = attack(RateLimiter(per_minute=60.0, burst=burst, clock=frozen, max_keys=cap))

    print(f"  before the fix (clear the table):  {old:>3} requests allowed")
    print(f"  after  the fix (evict coldest):    {new:>3} request{'s' if new != 1 else ''} allowed")
    print(f"\n  -> unrelated traffic used to hand the attacker {old - new} extra requests.\n")

    print("Rotating IPv6 addresses inside one /64 no longer buys a fresh burst:")
    limiter = RateLimiter(per_minute=60.0, burst=1.0, clock=frozen)
    for address in ("2001:db8::1", "2001:db8::2", "2001:db8::dead:beef", "2001:db8:1::1"):
        verdict = "ALLOWED" if limiter.allow(client_bucket(address)) else "refused"
        print(f"  {address:<22} bucket {client_bucket(address):<22} {verdict}")
    print("\n  -> the first three share one bucket; the fourth is a different /64.")

    print("\nNote: behind a reverse proxy every request carries the proxy's address,")
    print("so one bucket serves everyone until the trusted-proxy work lands (TODOS.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
