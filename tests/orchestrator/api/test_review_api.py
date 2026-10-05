"""Tier 2 forces the glossary and flags its output for review (S3.1).

Requirements: FR-511, FR-510, FR-512, FR-401, NFR-410.

Two paths store machine output: the request itself, and the worker acting on
a job the request queued. Both must flag, and the worker has never seen the
request, so the job has to carry the instruction.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient

from orchestrator.ops.prewarm import prewarm
from orchestrator.pipeline.segment import MODEL_FORMATS
from orchestrator.queue.jobs import (
    PRIORITY_LIVE_DEFERRED,
    PRIORITY_PREWARM,
    PRIORITY_REWARM,
    raised_by,
)
from orchestrator.queue.worker import Worker
from orchestrator.store.models import RaisedBy, ReviewState
from orchestrator.testing.mock_nmt import Mode
from orchestrator.testing.rig import OPEN_ORIGIN, ORIGIN, Rig, body, make_client, make_rig
from orchestrator.upstream.quota import QuotaManager
from orchestrator.upstream.translator import MockTranslator

WIRE = MODEL_FORMATS["wire"]


def _post(client: TestClient, payload: dict[str, Any], origin: str = ORIGIN) -> Any:
    return client.post("/v1/translate", json=payload, headers={"Origin": origin})


def _seg(resp: Any) -> dict[str, Any]:
    (segment,) = resp.json()["segments"]
    return segment


def _worker(rig: Rig) -> Worker:
    return Worker(
        queue=rig.queue,
        store=rig.service.store,
        translator=MockTranslator(WIRE),
        model_format=WIRE,
        quota=QuotaManager(requests_per_second=100.0),
        worker_id="w1",
    )


# --- flagged for review ---


def test_fr511_tier2_machine_output_is_flagged_for_review(rig: Rig, client: TestClient) -> None:
    segment = _seg(_post(client, body("Apply online today")))
    assert segment["status"] == "translated" and segment["origin"] == "mt"

    item = rig.tm.review_item(segment["segment_key"])
    assert item is not None
    assert item.state is ReviewState.PENDING_REVIEW
    assert item.site_id == "portal" and item.raised_by is RaisedBy.REQUEST
    shown = rig.tm.history(segment["segment_key"])[-1]
    assert item.current_version == shown.id  # the reviewer checks what the citizen was shown


def test_fr511_serving_it_again_does_not_flag_it_again(rig: Rig, client: TestClient) -> None:
    for _ in range(3):
        _post(client, body("Apply online today"))
    assert rig.tm.pending_review("portal") == 1
    assert rig.service.store.review_flags == {"created": 1}


def test_fr511_tier3_output_is_not_flagged(rig: Rig, client: TestClient) -> None:
    segment = _seg(_post(client, body("Apply online today", site="open"), OPEN_ORIGIN))
    assert segment["status"] == "translated"
    assert rig.tm.review_item(segment["segment_key"]) is None
    assert rig.tm.pending_review() == 0


def test_fr512_a_tier3_site_asking_for_tier2_is_flagged(rig: Rig, client: TestClient) -> None:
    """A request can make content stricter, and stricter includes being reviewed."""
    segment = _seg(_post(client, body("Apply online today", site="open", tier=2), OPEN_ORIGIN))
    assert segment["status"] == "translated"
    assert rig.tm.review_item(segment["segment_key"]).state is ReviewState.PENDING_REVIEW


def test_fr510_tier1_opens_no_review_item_because_nothing_was_translated(
    rig: Rig, client: TestClient
) -> None:
    segment = _seg(_post(client, body("Fee table", tier=1)))
    assert segment["status"] == "tier_blocked"
    assert rig.tm.pending_review() == 0 and rig.translator.calls == 0


def test_fr511_output_that_fails_validation_is_neither_stored_nor_flagged() -> None:
    rig = make_rig(modes={Mode.DROP: 1.0})
    segment = _seg(_post(make_client(rig), body("Pay Nu. 500 for the fee")))
    assert segment["status"] != "translated"
    assert segment["text"] == "Pay Nu. 500 for the fee"
    assert rig.tm.pending_review() == 0
    assert rig.tm.history(segment["segment_key"]) == []


# --- the cap ---


def test_fr511_past_the_cap_pages_still_translate_but_the_queue_stops_growing() -> None:
    rig = make_rig(review_cap=2)
    client = make_client(rig)
    statuses = [_seg(_post(client, body(f"Notice number {word}")))["status"] for word in "abcd"]

    assert statuses == ["translated"] * 4  # citizens are not made to wait for reviewers
    assert rig.tm.pending_review("portal") == 2
    assert rig.tm.owed("portal") == 2  # on record, out of the reviewers' queue
    assert rig.service.store.review_flags == {"created": 2, "owed": 2}


# --- the glossary is not optional ---


def test_fr511_a_request_cannot_switch_the_glossary_off(rig: Rig, client: TestClient) -> None:
    payload = body("Pay the fee online", glossary=False, use_glossary=False, termbase=None)
    payload["segments"][0]["glossary"] = False
    segment = _seg(_post(client, payload))
    assert segment["status"] == "translated"
    assert "ན་པ" in segment["text"] and "fee" not in segment["text"]


def test_fr511_tier2_output_missing_its_term_is_refused() -> None:
    """Forced means forced: no approved term, no translation."""
    rig = make_rig(modes={Mode.DROP: 1.0})
    segment = _seg(_post(make_client(rig), body("Pay the fee online")))
    assert segment["status"] == "glossary_term_missing"
    assert segment["text"] == "Pay the fee online"
    assert rig.tm.pending_review() == 0


# --- the worker flags on the request's behalf ---


def _queued_rig(**kw: Any) -> tuple[Rig, TestClient]:
    """No live quota, so every miss is queued for the worker."""
    rig = make_rig(rps=0.0001, **kw)
    rig.queue.translated = lambda key: key in rig.tm.lookup([], [key])[1]
    return rig, make_client(rig)


def test_fr511_a_job_queued_by_a_tier2_request_carries_the_flag() -> None:
    rig, client = _queued_rig()
    assert _seg(_post(client, body("Apply online today")))["status"] == "pending_mt"
    (job,) = rig.queue.jobs.values()
    assert job.review is True and job.priority == PRIORITY_LIVE_DEFERRED


def test_fr511_a_job_queued_by_a_tier3_request_does_not() -> None:
    rig, client = _queued_rig()
    _post(client, body("Apply online today", site="open"), OPEN_ORIGIN)
    (job,) = rig.queue.jobs.values()
    assert job.review is False


def test_fr511_the_worker_flags_what_it_stores_for_a_request() -> None:
    rig, client = _queued_rig()
    key = _seg(_post(client, body("Apply online today")))["segment_key"]
    assert rig.tm.review_item(key) is None  # nothing stored yet

    assert asyncio.run(_worker(rig).run_once()).outcomes["stored"] == 1
    item = rig.tm.review_item(key)
    assert item.state is ReviewState.PENDING_REVIEW
    assert item.raised_by is RaisedBy.REQUEST and item.site_id == "portal"


def test_fr511_the_worker_does_not_flag_tier3_jobs() -> None:
    rig, client = _queued_rig()
    _post(client, body("Apply online today", site="open"), OPEN_ORIGIN)
    assert asyncio.run(_worker(rig).run_once()).outcomes["stored"] == 1
    assert rig.tm.pending_review() == 0


def test_fr511_prewarm_is_flagged_as_operator_work_and_is_never_capped() -> None:
    """Pre-warming the pilot pages must not leave most of them unflagged."""
    rig, _ = _queued_rig(review_cap=1)
    site = rig.sites.get("portal")
    assert site is not None
    pages = [
        {"text": f"Service notice {word}", "path": "/services/renewal"} for word in "abcde"
    ]
    assert prewarm(rig.service, site, pages).queued == 5
    assert all(j.review and j.priority == PRIORITY_PREWARM for j in rig.queue.jobs.values())

    assert asyncio.run(_worker(rig).run_once()).outcomes["stored"] == 5
    assert rig.tm.pending_review("portal") == 5
    assert rig.service.store.review_flags == {"created": 5}


def test_fr511_prewarm_does_not_use_up_the_cap_for_page_views() -> None:
    rig, _ = _queued_rig(review_cap=1)
    site = rig.sites.get("portal")
    assert site is not None
    prewarm(
        rig.service,
        site,
        [{"text": f"Service notice {w}", "path": "/services/renewal"} for w in "abc"],
    )
    asyncio.run(_worker(rig).run_once())

    live = make_rig(review_cap=1)  # same cap, live quota available
    live.service.store = rig.service.store
    segment = _seg(_post(make_client(live), body("A page nobody pre-warmed")))
    assert segment["status"] == "translated"
    assert rig.tm.review_item(segment["segment_key"]).raised_by is RaisedBy.REQUEST


def test_fr511_only_a_page_view_counts_as_a_request() -> None:
    assert raised_by(PRIORITY_LIVE_DEFERRED) is RaisedBy.REQUEST
    assert raised_by(PRIORITY_PREWARM) is RaisedBy.OPERATOR
    assert raised_by(PRIORITY_REWARM) is RaisedBy.OPERATOR


# --- found by the pre-landing review, 2026-09-29 ---------------------------
#
# Keys carry no tier, so the same text may be stored for a Tier 3 page first.
# Flagging only at store time let every later Tier 2 serve go unreviewed.


def test_fr511_output_stored_for_tier3_is_flagged_when_tier2_is_served_it(
    rig: Rig, client: TestClient
) -> None:
    _post(client, body("Apply online today", site="open"), OPEN_ORIGIN)
    segment = _seg(_post(client, body("Apply online today")))
    assert segment["status"] == "translated" and segment["origin"] == "mt"
    item = rig.tm.review_item(segment["segment_key"])
    assert item is not None and item.state is ReviewState.PENDING_REVIEW
    assert item.site_id == "portal" and item.raised_by is RaisedBy.REQUEST


def test_fr511_a_tier2_serve_from_the_database_flags_too(rig: Rig, client: TestClient) -> None:
    _post(client, body("Apply online today", site="open"), OPEN_ORIGIN)
    rig.cache.data.clear()  # Redis restarted: the next answer comes from the TM
    segment = _seg(_post(client, body("Apply online today")))
    assert rig.tm.review_item(segment["segment_key"]).state is ReviewState.PENDING_REVIEW


def test_fr511_serving_flags_once_not_on_every_page_view(rig: Rig, client: TestClient) -> None:
    _post(client, body("Apply online today", site="open"), OPEN_ORIGIN)
    calls: list[str] = []
    real = rig.tm.flag_for_review

    def counted(**kw: Any) -> Any:
        calls.append(kw["segment_key"])
        return real(**kw)

    rig.tm.flag_for_review = counted  # type: ignore[method-assign]
    for _ in range(5):
        _post(client, body("Apply online today"))
    assert len(calls) == 1


def test_fr511_a_tier3_serve_never_flags(rig: Rig, client: TestClient) -> None:
    for _ in range(3):
        _post(client, body("Apply online today", site="open"), OPEN_ORIGIN)
    assert rig.tm.pending_review() == 0 and rig.service.store.review_flags == {}


def test_nfr410_a_review_queue_fault_does_not_fail_the_page(rig: Rig, client: TestClient) -> None:
    _post(client, body("Apply online today", site="open"), OPEN_ORIGIN)

    def unavailable(**_: Any) -> Any:
        raise ConnectionError("review queue unavailable")

    rig.tm.flag_for_review = unavailable  # type: ignore[method-assign]
    segment = _seg(_post(client, body("Apply online today")))
    assert segment["status"] == "translated"
    assert rig.service.store.review_flags == {"failed": 1}


def test_fr511_a_tier2_request_upgrades_a_job_a_tier3_request_queued() -> None:
    rig, client = _queued_rig()
    _post(client, body("Apply online today", site="open"), OPEN_ORIGIN)
    (job,) = rig.queue.jobs.values()
    assert job.review is False
    _post(client, body("Apply online today"))
    (job,) = rig.queue.jobs.values()
    assert job.review is True  # merged, not discarded

    asyncio.run(_worker(rig).run_once())
    key = _seg(_post(client, body("Apply online today")))["segment_key"]
    assert rig.tm.review_item(key).state is ReviewState.PENDING_REVIEW


def test_fr511_when_storing_fails_the_page_is_served_and_the_segment_queued() -> None:
    """D2 (2026-09-29): serve, and make sure a reviewer still hears about it."""
    rig = make_rig()

    def unavailable(**_: Any) -> Any:
        raise ConnectionError("database away")

    rig.tm.store_machine = unavailable  # type: ignore[method-assign]
    segment = _seg(_post(make_client(rig), body("Apply online today")))
    assert segment["status"] == "translated"
    assert rig.service.metrics.store_failures == 1
    (job,) = rig.queue.jobs.values()
    assert job.review is True  # the worker stores and flags it once storage is back
