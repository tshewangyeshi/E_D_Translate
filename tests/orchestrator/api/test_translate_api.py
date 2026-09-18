"""S2.1 — POST /v1/translate end to end.

Requirements: FR-100, FR-102, FR-103, FR-104, FR-140, FR-143, FR-155, FR-156, FR-401,
FR-510, FR-512, NFR-100, NFR-304, NFR-410, NFR-412.
"""

from __future__ import annotations

import json
import statistics
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.ratelimit import RateLimiter
from orchestrator.pipeline.segment import parse
from orchestrator.testing.mock_nmt import Mode

from .conftest import LEGAL_ORIGIN, ORIGIN, Rig, body, make_client, make_rig

H = {"Origin": ORIGIN}


def _post(client: TestClient, payload: dict[str, Any], **headers: str) -> Any:
    return client.post("/v1/translate", json=payload, headers={**H, **headers})


def _segs(resp: Any) -> list[dict[str, Any]]:
    return resp.json()["segments"]


# --- happy path: live translation, storage, cache (FR-100, FR-104, FR-155) ---


def test_fr100_translates_live_then_serves_from_cache(rig: Rig, client: TestClient) -> None:
    r1 = _post(client, body("Apply online today"))
    assert r1.status_code == 200
    (s1,) = _segs(r1)
    assert s1["status"] == "translated" and s1["origin"] == "mt"
    assert s1["text"].startswith("DZ:") and len(s1["segment_key"]) == 64
    assert rig.translator.calls == 1
    r2 = _post(client, body("Apply online today"))
    assert _segs(r2)[0]["text"] == s1["text"]
    assert rig.translator.calls == 1  # served from cache/TM, not the model again


def test_fr140_each_request_gets_its_own_entity_values(rig: Rig, client: TestClient) -> None:
    a = _segs(_post(client, body("Pay Nu. 500 at the counter")))[0]
    b = _segs(_post(client, body("Pay Nu. 600 at the counter")))[0]
    assert "Nu. 500" in a["text"] and "Nu. 600" in b["text"]
    assert a["segment_key"] == b["segment_key"]
    assert rig.translator.calls == 1  # one masked entry serves both


def test_fr143_no_entity_value_is_stored(rig: Rig, client: TestClient) -> None:
    _post(client, body("CID 00012345678 must pay Nu. 500 by 2026-06-30"))
    stored = [v.masked_target for seg in rig.tm._segments for v in rig.tm.history(seg)]  # noqa: SLF001
    blob = json.dumps(stored) + json.dumps(list(rig.cache.data.values()), ensure_ascii=False)
    for value in ("00012345678", "500", "2026-06-30"):
        assert value not in blob


def test_fr401_glossary_target_appears_and_english_term_does_not(client: TestClient) -> None:
    (s,) = _segs(_post(client, body("Pay the fee at the Department of Immigration")))
    assert s["status"] == "translated"
    assert "ན་པ" in s["text"] and "ཀ་ཁ་ག་ང" in s["text"]
    assert "Department" not in s["text"]


def test_fr122_inline_markup_round_trips_in_order(client: TestClient) -> None:
    (s,) = _segs(_post(client, body("Click ⟦1⟧here⟦/1⟧ to apply.")))
    assert s["status"] == "translated"
    assert [type(m).__name__ for m in parse(s["text"]).structure()] == ["Open", "Close"]


# --- tiers (FR-510, FR-512) ---


def test_fr510_tier1_site_never_gets_machine_translation(rig: Rig) -> None:
    client = make_client(rig)
    portal = _segs(_post(client, body("Applications close soon")))[0]
    assert portal["status"] == "translated"  # Tier 2 page filled the cache
    legal = client.post(
        "/v1/translate",
        json=body("Applications close soon", site="legal"),
        headers={"Origin": LEGAL_ORIGIN},
    )
    (s,) = _segs(legal)
    assert s["status"] == "tier_blocked" and s["text"] == "Applications close soon"


def test_fr512_request_cannot_lower_the_tier(rig: Rig) -> None:
    client = make_client(rig)
    resp = client.post(
        "/v1/translate",
        json=body("Eligibility rules", site="legal", tier=3),
        headers={"Origin": LEGAL_ORIGIN},
    )
    assert _segs(resp)[0]["status"] == "tier_blocked"
    assert rig.translator.calls == 0


def test_fr512_request_and_selector_can_raise_the_tier(rig: Rig, client: TestClient) -> None:
    raised = _segs(_post(client, body("Fee table", tier=1)))[0]
    assert raised["status"] == "tier_blocked"
    payload = body("Fee schedule")
    payload["segments"][0]["selector_tier"] = 1
    assert _segs(_post(client, payload))[0]["status"] == "tier_blocked"
    assert rig.translator.calls == 0


# --- NFR-304: nothing sent to MT until N distinct clients ---


def test_nfr304_text_is_not_translated_until_seen_by_n_clients() -> None:
    rig = make_rig(clients_to_persist=3)
    first = make_client(rig, client_host="203.0.113.1")
    for _ in range(3):  # the same client again and again does not count
        assert _segs(_post(first, body("Welcome back")))[0]["status"] == "pending_mt"
    second = make_client(rig, client_host="203.0.113.2")
    assert _segs(_post(second, body("Welcome back")))[0]["status"] == "pending_mt"
    assert rig.translator.calls == 0 and rig.queue.depth() == 0
    third = make_client(rig, client_host="203.0.113.3")
    assert _segs(_post(third, body("Welcome back")))[0]["status"] == "translated"
    assert rig.translator.calls == 1


# --- budget, quota, queue (FR-155, FR-156, NFR-412) ---


def test_fr155_slow_model_returns_pending_and_enqueues() -> None:
    rig = make_rig(delay=0.5, budget=0.05)
    client = make_client(rig)
    resp = _post(client, body("Apply online today"))
    (s,) = _segs(resp)
    assert s["status"] == "pending_mt" and s["text"] == "Apply online today"
    assert rig.queue.depth() == 1
    assert resp.headers["cache-control"] == "no-store"


def test_fr156_no_live_quota_enqueues_without_calling_the_model() -> None:
    rig = make_rig(rps=0.0001)
    client = make_client(rig)
    (s,) = _segs(_post(client, body("Apply online today")))
    assert s["status"] == "pending_mt"
    assert rig.translator.calls == 0 and rig.queue.depth() == 1


def test_nfr412_full_queue_still_answers_with_source_text() -> None:
    rig = make_rig(rps=0.0001, queue_depth=0)
    client = make_client(rig)
    resp = _post(client, body("Apply online today"))
    assert resp.status_code == 200 and _segs(resp)[0]["status"] == "pending_mt"
    assert rig.service.metrics.queue_full == 1


def test_fr155_job_payload_holds_no_entity_values() -> None:
    rig = make_rig(rps=0.0001)
    client = make_client(rig)
    _post(client, body("Pay Nu. 500 by 2026-06-30"))
    job = next(iter(rig.queue.jobs.values()))
    assert "500" not in job.masked_source and "2026" not in job.masked_source


# --- failures are 200 with source text (NFR-410) ---


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        (Mode.UNAVAILABLE, "upstream_error"),
        (Mode.TIMEOUT, "upstream_error"),
        (Mode.DROP, "entity_check_failed"),
        (Mode.INVENT_NUMBER, "entity_check_failed"),
        (Mode.TIBETAN_DIGITS, "entity_check_failed"),
        (Mode.EMPTY, "entity_check_failed"),
    ],
)
def test_nfr410_model_failures_return_source_text_with_200(mode: Mode, status: str) -> None:
    rig = make_rig(modes={mode: 1.0})
    client = make_client(rig)
    text = "Pay Nu. 500 by 2026-06-30"
    resp = _post(client, body(text))
    assert resp.status_code == 200
    (s,) = _segs(resp)
    assert s["status"] == status and s["text"] == text
    assert rig.tm.history(s["segment_key"]) == []  # nothing invalid is stored


def test_nfr410_upstream_error_is_queued_for_retry() -> None:
    rig = make_rig(modes={Mode.UNAVAILABLE: 1.0})
    client = make_client(rig)
    _post(client, body("Apply online today"))
    assert rig.queue.depth() == 1


def test_nfr410_malformed_wire_is_tag_fallback_not_an_error(client: TestClient) -> None:
    resp = _post(client, body("Broken ⟦1⟧ markup", "Fine sentence"))
    a, b = _segs(resp)
    assert resp.status_code == 200
    assert a["status"] == "tag_fallback" and a["text"] == "Broken ⟦1⟧ markup"
    assert b["status"] == "translated"


def test_nfr410_client_cannot_forge_entity_tokens(client: TestClient) -> None:
    (s,) = _segs(_post(client, body("Pay ⟦CUR:1⟧ now")))
    assert s["status"] == "tag_fallback"


def test_nfr410_storage_outage_is_200_with_source_text(rig: Rig, client: TestClient) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise ConnectionError("postgres down")

    rig.service.store.lookup = boom  # type: ignore[method-assign]
    resp = _post(client, body("One", "Two"))
    assert resp.status_code == 200
    assert [s["status"] for s in _segs(resp)] == ["upstream_error", "upstream_error"]
    assert [s["text"] for s in _segs(resp)] == ["One", "Two"]


# --- request limits and access (FR-100, FR-103, NFR-301) ---


def test_fr100_more_than_64_segments_is_413(client: TestClient) -> None:
    assert _post(client, body(*["x"] * 65)).status_code == 413
    assert _post(client, body(*["x"] * 64)).status_code == 200


def test_fr100_oversized_segment_is_413(client: TestClient) -> None:
    assert _post(client, body("a" * 5001)).status_code == 413


@pytest.mark.parametrize("origin", [None, "https://evil.example", "https://legal.gov.example"])
def test_fr103_unenrolled_origin_for_this_site_is_403(
    client: TestClient, origin: str | None
) -> None:
    headers = {"Origin": origin} if origin else {}
    assert client.post("/v1/translate", json=body("Hello"), headers=headers).status_code == 403


def test_fr103_text_plain_body_works_so_browsers_skip_preflight(client: TestClient) -> None:
    resp = client.post(
        "/v1/translate",
        content=json.dumps(body("Apply online")),
        headers={**H, "Content-Type": "text/plain;charset=UTF-8"},
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ORIGIN


def test_fr103_preflight_is_allowed_only_for_enrolled_origins(client: TestClient) -> None:
    ok = client.options("/v1/translate", headers={**H, "Access-Control-Request-Method": "POST"})
    assert ok.status_code == 204 and ok.headers["access-control-allow-origin"] == ORIGIN
    bad = client.options("/v1/translate", headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403


def test_fr103_per_client_rate_limit_returns_429(rig: Rig) -> None:
    client = make_client(rig, client_limiter=RateLimiter(per_minute=1, burst=2))
    codes = [_post(client, body("Hi")).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_fr103_invalid_json_is_400(client: TestClient) -> None:
    resp = client.post("/v1/translate", content=b"{not json", headers=H)
    assert resp.status_code == 400


# --- HTTP caching (FR-102) ---


def test_fr102_final_response_has_body_etag_and_revalidates(client: TestClient) -> None:
    r1 = _post(client, body("Apply online today"))
    etag = r1.headers["etag"]
    assert r1.headers["cache-control"].startswith("private, max-age=60")
    r2 = _post(client, body("Apply online today"), **{"If-None-Match": etag})
    assert r2.status_code == 304


def test_fr102_pending_response_is_never_cached_or_304d() -> None:
    rig = make_rig(rps=0.0001)
    client = make_client(rig)
    r1 = _post(client, body("Apply online today"))
    assert r1.headers["cache-control"] == "no-store"
    r2 = _post(client, body("Apply online today"), **{"If-None-Match": r1.headers["etag"]})
    assert r2.status_code == 200


def test_fr102_etag_changes_when_the_translation_arrives() -> None:
    rig = make_rig(rps=0.0001)
    client = make_client(rig)
    pending = _post(client, body("Apply online today"))
    rig.service.quota.requests_per_second = 1000.0
    rig.service.quota.__post_init__()
    done = _post(client, body("Apply online today"))
    assert _segs(done)[0]["status"] == "translated"
    assert done.headers["etag"] != pending.headers["etag"]


# --- performance (NFR-100) ---


def test_nfr100_fully_cached_batch_of_64_p95_under_300ms(rig: Rig, client: TestClient) -> None:
    texts = [f"Service number {n} is available online" for n in range(64)]
    assert all(s["status"] == "translated" for s in _segs(_post(client, body(*texts))))
    timings = []
    for _ in range(30):
        start = time.perf_counter()
        resp = _post(client, body(*texts))
        timings.append(time.perf_counter() - start)
        assert resp.status_code == 200
    p95 = statistics.quantiles(timings, n=20)[-1]
    assert p95 < 0.300, f"p95 {p95 * 1000:.0f} ms"
