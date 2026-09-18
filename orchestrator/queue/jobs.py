"""Translation job queue (backlog S2.4). Requirements: FR-155, FR-156, FR-143, NFR-412.

    enqueue ─► pending ─claim (SKIP LOCKED, lease)─► running ─complete─► done
                  ▲                                   │  │
                  │ fail (upstream): backoff, retry ◄─┘  └─ fail (validation or
                  │                                         max attempts) ─► failed
                  └── sweep: lease expired (worker crashed) ◄── running

* At most one ACTIVE job per machine_key; finished jobs don't block re-enqueueing.
* Enqueue is skipped when a current machine translation already exists (ER-21)
  or the key failed validation recently (a deterministic model would fail again).
* Bounded depth: a full queue refuses new work (the request still answers with
  source text, NFR-412).
* Jobs carry MASKED text only (FR-143).

``InMemoryQueue`` and ``PostgresJobQueue`` pass the same contract tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

PRIORITY_LIVE_DEFERRED = 50  # a citizen is waiting for this page
PRIORITY_PREWARM = 100
PRIORITY_REWARM = 150

VALIDATION_FAILURE_PREFIX = "validation:"
FAILURE_MEMORY_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Job:
    machine_key: str
    segment_key: str
    approved_key: str
    masked_source: str  # wire format with entity and term placeholders
    gfp: str
    term_ids: tuple[str, ...]
    site_id: str
    model_version: str = ""
    priority: int = PRIORITY_LIVE_DEFERRED


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    job: Job
    attempts: int


class JobQueue(Protocol):
    """What the request path needs."""

    def enqueue(self, job: Job) -> bool:
        """False only when the queue is full. Duplicates and already-translated keys are no-ops."""
        ...

    def depth(self) -> int: ...


class WorkQueue(JobQueue, Protocol):
    """What the worker needs."""

    def claim(self, worker_id: str, limit: int, lease_seconds: float) -> list[ClaimedJob]: ...

    def complete(self, job_id: int, worker_id: str) -> None: ...

    def fail(self, job_id: int, worker_id: str, error: str, *, retry: bool) -> None: ...

    def sweep(self) -> int:
        """Return expired-lease jobs to pending (crashed workers). Returns how many."""
        ...


def backoff_seconds(attempts: int) -> float:
    return float(min(2**attempts, 600))


@dataclass
class _Row:
    id: int
    job: Job
    state: str
    attempts: int = 0
    available_at: float = 0.0
    claimed_by: str | None = None
    lease_until: float | None = None
    last_error: str | None = None
    finished_at: float | None = None


class InMemoryQueue:
    """Reference implementation for unit tests; same semantics as PostgresJobQueue."""

    def __init__(
        self,
        max_depth: int = 10_000,
        *,
        max_attempts: int = 5,
        clock: Callable[[], float] = time.monotonic,
        translated: Callable[[str], bool] = lambda key: False,
    ) -> None:
        self.max_depth = max_depth
        self.max_attempts = max_attempts
        self.clock = clock
        self.translated = translated  # machine_key -> a current translation exists
        self.rows: dict[int, _Row] = {}
        self._next = 1

    @property
    def jobs(self) -> dict[str, Job]:
        """Active jobs by machine_key (read-only view for tests and metrics)."""
        return {
            r.job.machine_key: r.job
            for r in self.rows.values()
            if r.state in ("pending", "running")
        }

    def depth(self) -> int:
        return sum(1 for r in self.rows.values() if r.state == "pending")

    def enqueue(self, job: Job) -> bool:
        key = job.machine_key
        if key in self.jobs or self.translated(key):
            return True
        now = self.clock()
        for r in self.rows.values():
            if (
                r.job.machine_key == key
                and r.state == "failed"
                and (r.last_error or "").startswith(VALIDATION_FAILURE_PREFIX)
                and r.finished_at is not None
                and now - r.finished_at < FAILURE_MEMORY_SECONDS
            ):
                return True
        if self.depth() >= self.max_depth:
            return False
        self.rows[self._next] = _Row(self._next, job, "pending", available_at=now)
        self._next += 1
        return True

    def claim(self, worker_id: str, limit: int, lease_seconds: float) -> list[ClaimedJob]:
        now = self.clock()
        ready = sorted(
            (r for r in self.rows.values() if r.state == "pending" and r.available_at <= now),
            key=lambda r: (r.job.priority, r.available_at, r.id),
        )[:limit]
        claimed = []
        for r in ready:
            r.state, r.claimed_by, r.lease_until = "running", worker_id, now + lease_seconds
            r.attempts += 1
            claimed.append(ClaimedJob(r.id, r.job, r.attempts))
        return claimed

    def _own(self, job_id: int, worker_id: str) -> _Row | None:
        r = self.rows.get(job_id)
        return r if r is not None and r.state == "running" and r.claimed_by == worker_id else None

    def complete(self, job_id: int, worker_id: str) -> None:
        if (r := self._own(job_id, worker_id)) is not None:
            r.state, r.finished_at, r.claimed_by, r.lease_until = "done", self.clock(), None, None

    def fail(self, job_id: int, worker_id: str, error: str, *, retry: bool) -> None:
        r = self._own(job_id, worker_id)
        if r is None:
            return
        now = self.clock()
        r.last_error, r.claimed_by, r.lease_until = error, None, None
        if retry and r.attempts < self.max_attempts:
            r.state, r.available_at = "pending", now + backoff_seconds(r.attempts)
        else:
            r.state, r.finished_at = "failed", now

    def sweep(self) -> int:
        now = self.clock()
        count = 0
        for r in self.rows.values():
            if r.state == "running" and r.lease_until is not None and r.lease_until < now:
                r.claimed_by, r.lease_until, r.last_error = None, None, "lease expired"
                if r.attempts >= self.max_attempts:
                    r.state, r.finished_at = "failed", now
                else:
                    r.state, r.available_at = "pending", now
                count += 1
        return count
