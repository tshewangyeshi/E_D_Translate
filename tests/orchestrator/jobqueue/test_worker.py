"""Background worker and shared output validation (S2.4).

Requirements: FR-155, FR-156, FR-141, FR-142, FR-143, FR-102.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.pipeline.protect import EntityCheckError, mask
from orchestrator.pipeline.segment import MODEL_FORMATS, Segment, SegmentError, parse, tokenize
from orchestrator.pipeline.validate import validate_output
from orchestrator.queue.jobs import VALIDATION_FAILURE_PREFIX, InMemoryQueue
from orchestrator.queue.worker import Worker
from orchestrator.testing.mock_nmt import Mode
from orchestrator.testing.rig import ORIGIN, FakeClock, Rig, body, make_client, make_rig
from orchestrator.upstream.quota import QuotaManager
from orchestrator.upstream.translator import MockTranslator

WIRE = MODEL_FORMATS["wire"]


def _worker(
    rig: Rig,
    *,
    modes: dict[Mode, float] | None = None,
    worker_share: float = 0.5,
    model_version: str = "mock-nllb-1",
    worker_id: str = "w1",
) -> Worker:
    return Worker(
        queue=rig.queue,
        store=rig.service.store,
        translator=MockTranslator(WIRE, modes=modes, model_version=model_version),
        model_format=WIRE,
        quota=QuotaManager(requests_per_second=100.0, worker_share=worker_share),
        worker_id=worker_id,
        lease_seconds=60,
    )


def _pending_rig(**kw: Any) -> tuple[Rig, TestClient]:
    """A rig whose live path has no quota, so misses are queued for the worker."""
    rig = make_rig(rps=0.0001, **kw)
    rig.queue.translated = lambda key: key in rig.tm.lookup([], [key])[1]
    return rig, make_client(rig)


def _post(client: TestClient, text: str) -> Any:
    return client.post("/v1/translate", json=body(text), headers={"Origin": ORIGIN})


# --- validate_output: one policy for worker and live path ---


def _masked(text: str) -> Segment:
    return mask(parse(text))[0]


@pytest.mark.parametrize(
    "output",
    [
        "DZ: ⟦CUR:1⟧ ⟦CUR:1⟧ ⟦DATE:2⟧",  # duplicate entity
        "DZ: ⟦CUR:1⟧",  # missing entity
        "DZ: ⟦CUR:1⟧ ⟦DATE:2⟧ 42",  # invented number
        "DZ: ⟦CUR:1⟧⟦DATE:2⟧",  # entities newly touching
        "DZ: ⟦DATE:1⟧ ⟦CUR:2⟧",  # kinds swapped
    ],
)
def test_fr141_validate_output_rejects_bad_model_output(output: str) -> None:
    source = _masked("Pay Nu. 500 by 2026-06-30")
    with pytest.raises(EntityCheckError):
        validate_output(Segment(tokenize(output, allow_entities=True)), source)


def test_fr122_validate_output_rejects_reordered_tags() -> None:
    source = _masked("A ⟦1⟧b⟦/1⟧ c ⟦2⟧d⟦/2⟧")
    with pytest.raises(SegmentError):
        validate_output(Segment(tokenize("⟦2⟧d⟦/2⟧ A ⟦1⟧b⟦/1⟧ c", allow_entities=True)), source)


def test_fr141_validate_output_accepts_reordered_words_with_intact_placeholders() -> None:
    source = _masked("Pay Nu. 500 by 2026-06-30")
    validate_output(parse("DZ by ⟦DATE:2⟧ pay ⟦CUR:1⟧", allow_entities=True), source)


# --- worker behaviour ---


def test_fr155_worker_translates_queued_segment_and_next_request_is_served() -> None:
    rig, client = _pending_rig()
    first = _post(client, "Apply online today")
    assert first.json()["segments"][0]["status"] == "pending_mt"
    report = asyncio.run(_worker(rig).run_once())
    assert report.outcomes["stored"] == 1 and rig.queue.depth() == 0
    second = _post(client, "Apply online today")
    (s,) = second.json()["segments"]
    assert s["status"] == "translated" and s["origin"] == "mt"
    assert second.headers["etag"] != first.headers["etag"]  # FR-102: a new answer, a new ETag


def test_fr143_worker_restores_nothing_and_each_request_gets_its_own_values() -> None:
    rig, client = _pending_rig()
    _post(client, "Pay Nu. 500 by 2026-06-30")
    asyncio.run(_worker(rig).run_once())
    a = _post(client, "Pay Nu. 500 by 2026-06-30").json()["segments"][0]
    b = _post(client, "Pay Nu. 900 by 2027-01-01").json()["segments"][0]
    assert "Nu. 500" in a["text"] and "2026-06-30" in a["text"]
    assert "Nu. 900" in b["text"] and "2027-01-01" in b["text"]


def test_fr156_worker_uses_only_its_reserved_share() -> None:
    rig, client = _pending_rig()
    _post(client, "Apply online today")
    report = asyncio.run(_worker(rig, worker_share=0.0).run_once())
    assert report.claimed == 0 and report.outcomes["no_worker_quota"] == 1
    assert rig.queue.depth() == 1


def test_fr155_upstream_error_goes_back_to_the_queue_with_backoff() -> None:
    rig, client = _pending_rig()
    _post(client, "Apply online today")
    report = asyncio.run(_worker(rig, modes={Mode.UNAVAILABLE: 1.0}).run_once())
    assert report.outcomes["upstream_retry"] == 1
    assert rig.queue.depth() == 1  # pending again, available after backoff


def test_fr141_invalid_output_is_never_stored_and_not_requeued() -> None:
    rig, client = _pending_rig()
    _post(client, "Pay Nu. 500 by 2026-06-30")
    report = asyncio.run(_worker(rig, modes={Mode.DROP: 1.0}).run_once())
    assert list(report.outcomes) == ["invalid:missing"]
    (row,) = rig.queue.rows.values()
    assert row.state == "failed" and row.last_error.startswith(VALIDATION_FAILURE_PREFIX)
    assert rig.tm.history(row.job.segment_key) == []
    _post(client, "Pay Nu. 500 by 2026-06-30")  # another page view
    assert rig.queue.depth() == 0


def test_fr150_job_for_another_model_version_is_not_stored_under_its_key() -> None:
    rig, client = _pending_rig()
    _post(client, "Apply online today")
    report = asyncio.run(_worker(rig, model_version="mock-nllb-2").run_once())
    assert report.outcomes["model_changed"] == 1
    assert all(not rig.tm.history(r.job.segment_key) for r in rig.queue.rows.values())


def test_fr155_crashed_worker_job_is_finished_by_another_worker() -> None:
    clock = FakeClock()
    rig, client = _pending_rig()
    rig.queue.clock = clock
    _post(client, "Apply online today")
    crashed = rig.queue.claim("w-dead", 1, lease_seconds=30)  # claims, then dies
    assert len(crashed) == 1
    assert asyncio.run(_worker(rig, worker_id="w2").run_once()).claimed == 0  # still leased
    clock.advance(31)
    report = asyncio.run(_worker(rig, worker_id="w2").run_once())
    assert report.swept == 1 and report.outcomes["stored"] == 1
    assert _post(client, "Apply online today").json()["segments"][0]["status"] == "translated"


# --- end to end on PostgreSQL ---


@pytest.mark.integration
def test_fr155_postgres_queue_worker_end_to_end(pg_conn: Any) -> None:
    from orchestrator.queue.postgres_queue import PostgresJobQueue
    from orchestrator.store.cache import InMemoryCache, InMemorySeenCounter, ResilientCache
    from orchestrator.store.lookup import StoreSettings, TranslationStore
    from orchestrator.store.postgres_tm import PostgresTM

    rig = make_rig(rps=0.0001)
    tm = PostgresTM(pg_conn)
    store = TranslationStore(
        tm,
        ResilientCache(InMemoryCache()),
        InMemorySeenCounter(),
        StoreSettings(distinct_clients_to_persist=1),
    )
    queue = PostgresJobQueue(pg_conn)
    rig.service.store, rig.service.queue = store, queue
    client = make_client(rig)
    assert _post(client, "Pay Nu. 500 online").json()["segments"][0]["status"] == "pending_mt"
    assert queue.depth() == 1
    worker = Worker(
        queue=queue,
        store=store,
        translator=MockTranslator(WIRE),
        model_format=WIRE,
        quota=QuotaManager(requests_per_second=100.0),
        worker_id="pg-w1",
    )
    report = asyncio.run(worker.run_once())
    assert report.outcomes["stored"] == 1 and queue.depth() == 0
    (s,) = _post(client, "Pay Nu. 750 online").json()["segments"]
    assert s["status"] == "translated" and "Nu. 750" in s["text"]
    assert _post(client, "Pay Nu. 500 online").json()["segments"][0]["status"] == "translated"
    assert queue.depth() == 0  # translated keys are not re-queued (ER-21)


def test_nfr412_in_memory_queue_type_is_the_default_for_tests() -> None:
    assert isinstance(make_rig().queue, InMemoryQueue)
