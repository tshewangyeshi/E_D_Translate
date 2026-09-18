"""Job queue contract: InMemoryQueue and PostgresJobQueue behave identically (S2.4).

Requirements: FR-155, FR-156, FR-143, NFR-412.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from orchestrator.queue.jobs import (
    PRIORITY_LIVE_DEFERRED,
    PRIORITY_PREWARM,
    PRIORITY_REWARM,
    VALIDATION_FAILURE_PREFIX,
    Job,
)


def _job(n: int, priority: int = PRIORITY_LIVE_DEFERRED) -> Job:
    return Job(
        machine_key=f"{n:064d}",
        segment_key=f"s{n:063d}",
        approved_key=f"a{n:063d}",
        masked_source=f"Pay ⟦CUR:1⟧ item {chr(65 + n % 26)}",
        gfp="0" * 64,
        term_ids=("T-0005",) if n % 2 else (),
        site_id="portal",
        model_version="mock-nllb-1",
        priority=priority,
    )


def test_fr155_active_jobs_are_deduplicated(work_queue: Any) -> None:
    q, _ = work_queue
    assert q.enqueue(_job(1)) and q.enqueue(_job(1)) and q.enqueue(_job(2))
    assert q.depth() == 2


def test_fr155_enqueue_is_skipped_when_a_translation_already_exists(work_queue: Any) -> None:
    q, tm = work_queue
    job = _job(3)
    tm.store_machine(
        segment_key=job.segment_key,
        masked_source=job.masked_source,
        machine_key=job.machine_key,
        masked_target="DZ: ⟦CUR:1⟧",
        gfp=job.gfp,
        model_version="mock-nllb-1",
        term_ids=(),
        tag_integrity=True,
    )
    assert q.enqueue(job) is True  # not "full": nothing to do
    assert q.depth() == 0


def test_nfr412_full_queue_refuses_new_work_but_accepts_duplicates(work_queue: Any) -> None:
    q, _ = work_queue
    for n in range(100):
        assert q.enqueue(_job(n))
    assert q.enqueue(_job(100)) is False
    assert q.enqueue(_job(5)) is True  # already queued
    assert q.depth() == 100


def test_fr155_claims_follow_priority_and_limit(work_queue: Any) -> None:
    q, _ = work_queue
    q.enqueue(_job(1, PRIORITY_REWARM))
    q.enqueue(_job(2, PRIORITY_PREWARM))
    q.enqueue(_job(3, PRIORITY_LIVE_DEFERRED))
    claimed = q.claim("w1", 2, 60)
    assert [c.job.machine_key for c in claimed] == [_job(3).machine_key, _job(2).machine_key]
    assert all(c.attempts == 1 for c in claimed)
    assert q.depth() == 1


def test_fr143_claimed_job_round_trips_masked_payload(work_queue: Any) -> None:
    q, _ = work_queue
    q.enqueue(_job(7))
    (c,) = q.claim("w1", 1, 60)
    assert c.job == _job(7)


def test_fr155_completed_key_can_be_enqueued_again(work_queue: Any) -> None:
    q, _ = work_queue
    q.enqueue(_job(1))
    (c,) = q.claim("w1", 1, 60)
    q.complete(c.id, "w1")
    assert q.depth() == 0
    assert q.enqueue(_job(1)) and q.depth() == 1  # e.g. after an invalidation


def test_fr155_upstream_failure_retries_after_backoff(work_queue: Any) -> None:
    q, _ = work_queue
    q.enqueue(_job(1))
    (c,) = q.claim("w1", 1, 60)
    q.fail(c.id, "w1", "upstream:UpstreamUnavailable", retry=True)
    assert q.depth() == 1
    assert q.claim("w1", 1, 60) == []  # not before the backoff


def test_fr141_validation_failure_is_final_and_not_requeued_by_page_views(work_queue: Any) -> None:
    q, _ = work_queue
    q.enqueue(_job(1))
    (c,) = q.claim("w1", 1, 60)
    q.fail(c.id, "w1", VALIDATION_FAILURE_PREFIX + "missing", retry=False)
    assert q.depth() == 0
    assert q.enqueue(_job(1)) is True
    assert q.depth() == 0  # remembered for 24 h


def test_fr155_only_the_claiming_worker_can_finish_a_job(work_queue: Any) -> None:
    q, _ = work_queue
    q.enqueue(_job(1))
    (c,) = q.claim("w1", 1, 60)
    q.complete(c.id, "someone-else")
    q.fail(c.id, "someone-else", "x", retry=False)
    assert q.claim("w2", 1, 60) == []  # still running under w1
    q.complete(c.id, "w1")


def test_fr155_crashed_worker_job_is_swept_back_to_pending(work_queue: Any) -> None:
    q, _ = work_queue
    q.enqueue(_job(1))
    (c,) = q.claim("w1", 1, -1)  # lease already expired: the worker "crashed"
    assert q.sweep() == 1
    (again,) = q.claim("w2", 1, 60)
    assert again.job == c.job and again.attempts == 2
    q.complete(c.id, "w1")  # the crashed worker waking up late changes nothing
    q.complete(again.id, "w2")
    assert q.depth() == 0


@pytest.mark.integration
def test_fr155_concurrent_workers_never_claim_the_same_job(pg_conn: Any) -> None:
    import psycopg

    from orchestrator.queue.postgres_queue import PostgresJobQueue

    schema = pg_conn.execute("SELECT current_schema()").fetchone()[0]
    q = PostgresJobQueue(pg_conn, max_depth=1000)
    for n in range(200):
        q.enqueue(_job(n))
    claimed: dict[str, list[int]] = {}
    barrier = threading.Barrier(4)

    def worker(name: str) -> None:
        dsn = pg_conn.info.dsn
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"SET search_path TO {schema}")
            wq = PostgresJobQueue(conn)
            barrier.wait()
            ids: list[int] = []
            while batch := wq.claim(name, 7, 60):
                ids.extend(c.id for c in batch)
            claimed[name] = ids

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    all_ids = [i for ids in claimed.values() for i in ids]
    assert len(all_ids) == 200 and len(set(all_ids)) == 200
    assert sum(1 for ids in claimed.values() if ids) > 1  # work really was shared
