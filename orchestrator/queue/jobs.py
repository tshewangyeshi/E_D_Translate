"""Translation job queue interface and in-memory stand-in (FR-155).

Jobs carry MASKED text only (FR-143): never entity values. The PostgreSQL
``SKIP LOCKED`` implementation with a visibility-timeout sweeper and
uniqueness on active jobs is S2.4; it implements this same interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Job:
    machine_key: str
    segment_key: str
    approved_key: str
    masked_source: str  # wire format with entity and term placeholders
    gfp: str
    term_ids: tuple[str, ...]
    site_id: str


class JobQueue(Protocol):
    def enqueue(self, job: Job) -> bool:
        """Queue a job. Returns False when the queue is full (bounded depth, NFR-412).
        Enqueueing a key that is already queued is a no-op that returns True."""
        ...

    def depth(self) -> int: ...


class InMemoryQueue:
    def __init__(self, max_depth: int = 10_000) -> None:
        self.max_depth = max_depth
        self.jobs: dict[str, Job] = {}

    def enqueue(self, job: Job) -> bool:
        if job.machine_key in self.jobs:
            return True
        if len(self.jobs) >= self.max_depth:
            return False
        self.jobs[job.machine_key] = job
        return True

    def depth(self) -> int:
        return len(self.jobs)
