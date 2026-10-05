"""Dependency health (backlog S2.3). Requirement: FR-610.

Each upstream is reported on its own, so an operator reading the answer knows
which thing to go and look at:

    nmt       the translation model, judged by the calls this process made
    postgres  translation memory, queue, audit trail
    redis     hot cache and distinct-client counter
    queue     depth, oldest waiting job, whether it is full
    quota     what the live path may still send to the model

Decisions worth knowing before changing this file.

* **The answer is HTTP 200.** The service keeps answering in English when its
  dependencies are down (NFR-410), so a replica with a dead database is still
  doing its job. Reporting 503 would invite a load balancer to take every
  replica out at once over a fault they all share, and turn degraded into
  down. ``status`` in the body says ``ok`` or ``degraded``. A lost database
  connection is reopened on the next call (orchestrator/store/pg.py), so
  "degraded" does not need a restart to recover.

* **The model is watched, not probed.** A probe would spend quota that belongs
  to citizens and to the worker (FR-156), so the model's health is read off
  the calls already being made. A process that has made none says ``unknown``.

* **Probing is bounded.** A probe that misses its deadline is reported down,
  but its thread cannot be stopped, so a probe still running from an earlier
  check is not started again: it is reported down as ``still_running``. At
  most one thread per probe is ever busy, in a small executor of its own, and
  the in-memory checks never wait behind them. Answers are reused for a
  couple of seconds, so a caller asking in a loop adds no load.

Nothing reported names a host, a DSN or an error message: states, counts and
the error's class name only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from orchestrator.queue.jobs import JobQueue
from orchestrator.upstream.quota import QuotaManager

PROBE_TIMEOUT_SECONDS = 2.0
REUSE_SECONDS = 2.0
#: Consecutive failed calls after which the model is reported down.
FAILURES_TO_DOWN = 3


class State(StrEnum):
    OK = "ok"
    DOWN = "down"
    UNKNOWN = "unknown"  # no evidence either way; does not degrade the overall status


@dataclass(frozen=True)
class Check:
    state: State
    detail: Mapping[str, object] = field(default_factory=dict)


Probe = Callable[[], Check]


@dataclass
class UpstreamHealth:
    """What this process has seen of the translation model."""

    calls: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    _last_ok: float | None = None

    def succeeded(self) -> None:
        self.calls += 1
        self.consecutive_failures = 0
        self._last_ok = time.monotonic()

    def failed(self, kind: str) -> None:
        self.calls += 1
        self.consecutive_failures += 1
        self.last_error = kind

    def check(self) -> Check:
        if self.calls == 0:
            return Check(State.UNKNOWN, {"calls": 0})
        down = self.consecutive_failures >= FAILURES_TO_DOWN
        detail: dict[str, object] = {
            "calls": self.calls,
            "consecutive_failures": self.consecutive_failures,
        }
        if self._last_ok is not None:
            detail["seconds_since_success"] = round(time.monotonic() - self._last_ok, 1)
        if self.consecutive_failures:
            detail["last_error"] = self.last_error
        return Check(State.DOWN if down else State.OK, detail)


def postgres_probe(conn: Any) -> Probe:
    def probe() -> Check:
        conn.execute("SELECT 1").fetchone()
        return Check(State.OK)

    return probe


def redis_probe(client: Any) -> Probe:
    def probe() -> Check:
        client.ping()
        return Check(State.OK)

    return probe


def queue_probe(queue: JobQueue) -> Probe:
    def probe() -> Check:
        depth = queue.depth()
        detail: dict[str, object] = {
            "depth": depth,
            "oldest_pending_seconds": queue.oldest_pending_seconds(),
        }
        max_depth = getattr(queue, "max_depth", None)
        if isinstance(max_depth, int):
            detail["max_depth"] = max_depth
            detail["full"] = depth >= max_depth
        return Check(State.OK, detail)

    return probe


def quota_probe(quota: QuotaManager) -> Probe:
    def probe() -> Check:
        return Check(State.OK, quota.snapshot())

    return probe


def _failed(err: BaseException) -> Check:
    return Check(State.DOWN, {"error": type(err).__name__})


class HealthChecker:
    """Runs probes: in-memory ones inline, I/O ones in bounded threads."""

    def __init__(
        self,
        *,
        inline: Mapping[str, Probe],
        blocking: Mapping[str, Probe],
        timeout: float | None = None,
        reuse_seconds: float | None = None,
    ) -> None:
        self.inline = dict(inline)
        self.blocking = dict(blocking)
        self.timeout = PROBE_TIMEOUT_SECONDS if timeout is None else timeout
        self.reuse_seconds = REUSE_SECONDS if reuse_seconds is None else reuse_seconds
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, len(self.blocking)), thread_name_prefix="dzweb-health"
        )
        self._running: dict[str, Future[Check]] = {}
        self._last: tuple[float, dict[str, Check]] | None = None
        self._lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None

    def _loop_lock(self) -> asyncio.Lock:
        """An asyncio lock belongs to one event loop; tests run several."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock[0] is not loop:
            self._lock = (loop, asyncio.Lock())
        return self._lock[1]

    async def run(self) -> dict[str, Check]:
        async with self._loop_lock():  # one check at a time; callers queue for its answer
            if self._last is not None and time.monotonic() - self._last[0] < self.reuse_seconds:
                return self._last[1]
            checks = {name: self._inline(probe) for name, probe in self.inline.items()}
            names = list(self.blocking)
            results = await asyncio.gather(*(self._blocking(n) for n in names))
            checks.update(zip(names, results, strict=True))
            self._last = (time.monotonic(), checks)
            return checks

    @staticmethod
    def _inline(probe: Probe) -> Check:
        try:
            return probe()
        except Exception as err:  # noqa: BLE001 - a failing probe is the finding, not a crash
            return _failed(err)

    async def _blocking(self, name: str) -> Check:
        running = self._running.get(name)
        if running is not None and not running.done():
            return Check(State.DOWN, {"error": "still_running"})
        future = self._executor.submit(self.blocking[name])
        self._running[name] = future
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), self.timeout)
        except TimeoutError:
            return Check(State.DOWN, {"error": "timeout"})
        except Exception as err:  # noqa: BLE001 - a failing probe is the finding, not a crash
            return _failed(err)


def report(checks: Mapping[str, Check]) -> dict[str, object]:
    degraded = any(check.state is State.DOWN for check in checks.values())
    return {
        "status": "degraded" if degraded else "ok",
        "upstreams": {
            name: {"state": check.state.value, **check.detail}
            for name, check in sorted(checks.items())
        },
    }
