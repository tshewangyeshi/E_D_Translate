"""Health and metrics (S2.3). Requirements: FR-610, FR-611, NFR-410, NFR-303.

Both endpoints exist to be read when something is wrong, so most of these
tests break something first and then ask the endpoint what it sees.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.ops import health
from orchestrator.ops.health import Check, State, UpstreamHealth, postgres_probe, redis_probe
from orchestrator.ops.metrics import Family, Histogram, render
from orchestrator.testing.mock_nmt import Mode
from orchestrator.testing.rig import OPS, ORIGIN, FakeClock, Rig, body, make_client, make_rig
from orchestrator.upstream.quota import QuotaManager

H = {"Origin": ORIGIN}


@pytest.fixture(autouse=True)
def fresh_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests change something and ask again at once; reuse is tested on its own."""
    from orchestrator.ops import metrics

    monkeypatch.setattr(health, "REUSE_SECONDS", 0.0)
    monkeypatch.setattr(metrics, "GAUGE_REUSE_SECONDS", 0.0)


def _translate(client: TestClient, *texts: str, **extra: Any) -> Any:
    return client.post("/v1/translate", json=body(*texts, **extra), headers=H)


def _health(client: TestClient) -> dict[str, Any]:
    response = client.get("/v1/health", headers=OPS)
    assert response.status_code == 200
    return response.json()


def _metrics(client: TestClient) -> dict[str, float]:
    """The exposition parsed into {sample: value}."""
    response = client.get("/v1/metrics", headers=OPS)
    assert response.status_code == 200
    samples = {}
    for line in response.text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            samples[name] = float(value)
    return samples


def _ok() -> Check:
    return Check(State.OK)


def _down() -> Check:
    raise ConnectionError("password authentication failed for user dzweb at db-internal-7")


# --- health: each upstream on its own (FR-610) ---


def test_fr610_every_upstream_is_reported_separately(rig: Rig) -> None:
    client = make_client(rig, health={"postgres": _ok, "redis": _ok})
    report = _health(client)
    assert report["status"] == "ok"
    assert set(report["upstreams"]) == {"nmt", "postgres", "redis", "queue", "quota"}


def test_fr610_one_upstream_down_does_not_hide_the_others(rig: Rig) -> None:
    client = make_client(rig, health={"postgres": _down, "redis": _ok})
    report = _health(client)
    assert report["status"] == "degraded"
    assert report["upstreams"]["postgres"]["state"] == "down"
    assert report["upstreams"]["redis"]["state"] == "ok"
    assert report["upstreams"]["queue"]["state"] == "ok"


def test_nfr410_health_answers_200_even_when_everything_is_down(rig: Rig) -> None:
    """A replica that still serves English is still doing its job."""
    rig.service.queue.depth = _down  # type: ignore[method-assign]
    client = make_client(rig, health={"postgres": _down, "redis": _down})
    response = client.get("/v1/health", headers=OPS)
    assert response.status_code == 200
    states = {name: u["state"] for name, u in response.json()["upstreams"].items()}
    assert states["postgres"] == states["redis"] == states["queue"] == "down"


def test_fr610_a_failure_is_named_by_kind_never_by_message(rig: Rig) -> None:
    """Error messages carry hostnames, users and DSNs; the class name carries none."""
    client = make_client(rig, health={"postgres": _down})
    text = client.get("/v1/health", headers=OPS).text
    assert '"error":"ConnectionError"' in text.replace(" ", "")
    for leaked in ("password", "dzweb", "db-internal-7"):
        assert leaked not in text


def test_fr610_a_probe_that_hangs_is_reported_down_not_waited_for() -> None:
    """Timed inside the event loop: that is where a request would be waiting.

    (Timing the whole test would also count the interpreter waiting for the
    abandoned thread at shutdown, which no request ever does.)
    """

    def hang() -> Check:
        time.sleep(0.6)
        return Check(State.OK)

    checker = health.HealthChecker(
        inline={}, blocking={"postgres": hang, "redis": _ok}, timeout=0.05
    )

    async def timed() -> tuple[dict[str, Check], float]:
        started = time.perf_counter()
        checks = await checker.run()
        return checks, time.perf_counter() - started

    checks, elapsed = asyncio.run(timed())
    assert health.report(checks)["upstreams"] == {
        "postgres": {"state": "down", "error": "timeout"},
        "redis": {"state": "ok"},
    }
    assert elapsed < 0.4


def test_fr610_a_probe_still_running_is_not_started_again() -> None:
    """Asked in a loop while the database hangs, the checker must not pile up threads."""
    started: list[int] = []
    release = threading.Event()

    def hang() -> Check:
        started.append(1)
        release.wait(5)
        return Check(State.OK)

    checker = health.HealthChecker(
        inline={}, blocking={"postgres": hang}, timeout=0.05, reuse_seconds=0.0
    )
    try:
        first = asyncio.run(checker.run())
        again = [asyncio.run(checker.run()) for _ in range(5)]
    finally:
        release.set()
    assert first["postgres"].detail == {"error": "timeout"}
    assert {c["postgres"].detail["error"] for c in again} == {"still_running"}
    assert len(started) == 1


def test_fr610_in_memory_checks_never_wait_behind_a_hung_one() -> None:
    release = threading.Event()

    def hang() -> Check:
        release.wait(5)
        return Check(State.OK)

    checker = health.HealthChecker(
        inline={"quota": _ok}, blocking={"postgres": hang}, timeout=0.05, reuse_seconds=0.0
    )
    try:
        for _ in range(3):
            assert asyncio.run(checker.run())["quota"].state is State.OK
    finally:
        release.set()


def test_fr610_an_answer_is_reused_for_a_moment() -> None:
    calls: list[int] = []

    def probe() -> Check:
        calls.append(1)
        return Check(State.OK)

    checker = health.HealthChecker(inline={}, blocking={"postgres": probe}, reuse_seconds=60)
    for _ in range(10):
        asyncio.run(checker.run())
    assert len(calls) == 1


def test_fr610_health_is_not_readable_from_a_web_page(rig: Rig) -> None:
    """No CORS header: a script on another origin cannot read the answer."""
    response = make_client(rig).get("/v1/health", headers={**H, **OPS})
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_fr610_the_queue_reports_depth_age_and_whether_it_is_full() -> None:
    rig = make_rig(rps=0.0001, queue_depth=2)  # no live quota: misses are queued
    client = make_client(rig)
    assert _health(client)["upstreams"]["queue"] == {
        "state": "ok",
        "depth": 0,
        "oldest_pending_seconds": None,
        "max_depth": 2,
        "full": False,
    }
    _translate(client, "Apply online today", "Renew your passport")
    queue = _health(client)["upstreams"]["queue"]
    assert queue["depth"] == 2 and queue["full"] is True
    assert queue["oldest_pending_seconds"] >= 0


def test_fr610_quota_shows_what_the_live_path_may_still_send() -> None:
    rig = make_rig(rps=4.0)  # half is reserved for the worker (FR-156)
    client = make_client(rig)
    before = _health(client)["upstreams"]["quota"]
    assert before["live_per_second"] == 2.0 and before["worker_per_second"] == 2.0
    _translate(client, "Apply online today", "Renew your passport")
    after = _health(client)["upstreams"]["quota"]
    assert after["live_tokens"] < before["live_tokens"]
    assert after["state"] == "ok"


def test_fr610_looking_at_the_quota_does_not_spend_it() -> None:
    clock = FakeClock()
    quota = QuotaManager(10.0, worker_share=0.5, clock=clock)
    for _ in range(50):
        quota.snapshot()
    assert [quota.try_live() for _ in range(6)] == [True] * 5 + [False]


# --- the model is watched, not probed ---


def test_fr610_the_model_is_unknown_until_it_has_been_called(rig: Rig) -> None:
    client = make_client(rig)
    report = _health(client)
    assert report["upstreams"]["nmt"] == {"state": "unknown", "calls": 0}
    assert report["status"] == "ok"  # no evidence is not a fault
    assert rig.translator.calls == 0  # asking about health spends no quota


def test_fr610_a_working_model_is_reported_ok(rig: Rig) -> None:
    client = make_client(rig)
    _translate(client, "Apply online today")
    nmt = _health(client)["upstreams"]["nmt"]
    assert nmt["state"] == "ok" and nmt["calls"] == 1 and nmt["consecutive_failures"] == 0


def test_fr610_a_failing_model_is_reported_down_and_recovers() -> None:
    rig = make_rig(modes={Mode.UNAVAILABLE: 1.0})
    client = make_client(rig)
    _translate(client, "Apply online", "Renew a passport", "Pay a bill")
    report = _health(client)
    assert report["status"] == "degraded"
    assert report["upstreams"]["nmt"]["state"] == "down"
    assert report["upstreams"]["nmt"]["consecutive_failures"] == 3

    rig.service.upstream.succeeded()
    assert _health(client)["upstreams"]["nmt"]["state"] == "ok"


def test_fr610_one_bad_call_is_not_an_outage() -> None:
    model = UpstreamHealth()
    model.succeeded()
    model.failed("UpstreamUnavailable")
    model.failed("UpstreamUnavailable")
    assert model.check().state is State.OK
    model.failed("timeout")
    check = model.check()
    assert check.state is State.DOWN and check.detail["last_error"] == "timeout"


def test_fr610_a_model_too_slow_to_answer_counts_as_failing() -> None:
    rig = make_rig(delay=0.3, budget=0.02)
    client = make_client(rig)
    _translate(client, "Apply online", "Renew a passport", "Pay a bill")
    nmt = _health(client)["upstreams"]["nmt"]
    assert nmt["state"] == "down" and nmt["last_error"] == "timeout"


class _Conn:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.statements = fail, []

    def execute(self, sql: str) -> Any:
        if self.fail:
            raise ConnectionError("server closed the connection")
        self.statements.append(sql)
        return self

    def fetchone(self) -> tuple[int]:
        return (1,)

    def ping(self) -> bool:
        if self.fail:
            raise ConnectionError("connection refused")
        return True


def test_fr610_the_database_and_cache_probes_ask_the_real_thing() -> None:
    conn = _Conn()
    assert postgres_probe(conn)().state is State.OK and conn.statements == ["SELECT 1"]
    assert redis_probe(_Conn())().state is State.OK
    with pytest.raises(ConnectionError):
        postgres_probe(_Conn(fail=True))()
    with pytest.raises(ConnectionError):
        redis_probe(_Conn(fail=True))()


@pytest.mark.integration
def test_fr610_probes_against_real_services(pg_conn: Any, redis_client: Any) -> None:
    assert postgres_probe(pg_conn)().state is State.OK
    assert redis_probe(redis_client)().state is State.OK


# --- metrics (FR-611) ---


def test_fr611_everything_the_requirement_names_is_exported(rig: Rig) -> None:
    client = make_client(rig)
    _translate(client, "Apply online today")
    text = client.get("/v1/metrics", headers=OPS).text
    for name in (
        "dzweb_tag_integrity_ratio",
        "dzweb_entity_preservation_ratio",
        "dzweb_glossary_compliance_ratio",
        "dzweb_request_seconds_bucket",
        "dzweb_upstream_errors_total",
        "dzweb_fallback_total",
        "dzweb_pending_mt_ratio",
        "dzweb_queue_depth",
        "dzweb_queue_oldest_pending_seconds",
    ):
        assert f"# TYPE {name.removesuffix('_bucket')} " in text, name


def test_fr611_cache_hit_rate_counts_where_answers_came_from(rig: Rig) -> None:
    client = make_client(rig)
    _translate(client, "Apply online today")  # miss, translated live
    for _ in range(3):
        _translate(client, "Apply online today")  # served from the cache
    m = _metrics(client)
    assert m['dzweb_lookups_total{source="miss"}'] == 1
    assert m['dzweb_lookups_total{source="cache"}'] == 3
    assert m["dzweb_cache_hit_ratio"] == 0.75


def test_fr611_a_lookup_answered_by_the_database_is_not_a_cache_hit(rig: Rig) -> None:
    client = make_client(rig)
    _translate(client, "Apply online today")
    rig.cache.data.clear()  # Redis restarted
    _translate(client, "Apply online today")
    m = _metrics(client)
    assert m['dzweb_lookups_total{source="tm"}'] == 1
    assert m["dzweb_cache_hit_ratio"] == 0.0


def test_fr611_entity_failures_lower_entity_preservation_only() -> None:
    rig = make_rig(modes={Mode.INVENT_NUMBER: 1.0})
    client = make_client(rig)
    _translate(client, "Apply online today")
    m = _metrics(client)
    assert m['dzweb_model_outputs_total{result="entity_check_failed"}'] == 1
    assert m["dzweb_entity_preservation_ratio"] == 0.0
    assert m["dzweb_tag_integrity_ratio"] == 1.0


def test_fr611_tag_failures_lower_tag_integrity_only() -> None:
    rig = make_rig(modes={Mode.DROP: 1.0})
    client = make_client(rig)
    _translate(client, "Read the ⟦1⟧full notice⟦/1⟧ first")
    m = _metrics(client)
    assert m['dzweb_model_outputs_total{result="tag_fallback"}'] == 1
    assert m["dzweb_tag_integrity_ratio"] == 0.0
    assert m["dzweb_entity_preservation_ratio"] == 1.0


def test_fr611_rates_are_shares_of_what_the_model_returned(rig: Rig) -> None:
    client = make_client(rig)
    _translate(client, "Apply online", "Renew a passport", "Pay a bill")
    rig.translator._mock.modes = {Mode.INVENT_NUMBER: 1.0}
    _translate(client, "Book an appointment")
    m = _metrics(client)
    assert m['dzweb_model_outputs_total{result="ok"}'] == 3
    assert m["dzweb_entity_preservation_ratio"] == 0.75


def test_fr611_glossary_compliance_counts_terms(rig: Rig) -> None:
    client = make_client(rig)
    _translate(client, "Pay the fee at the Department of Immigration")  # two terms, restored
    assert _metrics(client)["dzweb_glossary_compliance_ratio"] == 1.0

    rig.translator._mock.modes = {Mode.DROP: 1.0}
    _translate(client, "The fee is due")  # one term, dropped by the model
    m = _metrics(client)
    assert m['dzweb_glossary_terms_total{result="restored"}'] == 2
    assert m['dzweb_glossary_terms_total{result="missing"}'] == 1
    assert m["dzweb_glossary_compliance_ratio"] == pytest.approx(2 / 3)


def test_fr611_rates_read_as_healthy_before_there_is_any_evidence(rig: Rig) -> None:
    """A fresh replica must not page anyone for a 0% it has not earned."""
    m = _metrics(make_client(rig))
    assert m["dzweb_tag_integrity_ratio"] == 1.0
    assert m["dzweb_entity_preservation_ratio"] == 1.0
    assert m["dzweb_glossary_compliance_ratio"] == 1.0
    assert m["dzweb_pending_mt_ratio"] == 0.0
    # No lookup yet, so no hit rate: 0% would page someone on every restart.
    assert "dzweb_cache_hit_ratio" not in m


def test_fr611_fallbacks_are_counted_by_cause() -> None:
    rig = make_rig(clients_to_persist=3)
    client = make_client(rig)
    _translate(client, "Apply online today")  # seen by one client only
    _translate(client, "Fee table", tier=1)  # Tier 1, nothing approved
    _translate(client, "Broken ⟦1⟧ markup")  # malformed
    m = _metrics(client)
    assert m['dzweb_fallback_total{cause="awaiting_distinct_clients"}'] == 1
    assert m['dzweb_fallback_total{cause="no_approved_translation"}'] == 1
    assert m['dzweb_segments_total{status="tier_blocked"}'] == 1
    assert m['dzweb_segments_total{status="tag_fallback"}'] == 1
    assert m["dzweb_pending_mt_ratio"] == pytest.approx(1 / 3)


def test_fr611_upstream_errors_are_counted_by_type() -> None:
    rig = make_rig(modes={Mode.UNAVAILABLE: 1.0})
    client = make_client(rig)
    _translate(client, "Apply online", "Renew a passport")
    m = _metrics(client)
    (errors,) = [v for k, v in m.items() if k.startswith("dzweb_upstream_errors_total")]
    assert errors == 2
    assert m['dzweb_segments_total{status="upstream_error"}'] == 2


def test_fr611_queue_depth_and_refusals_are_reported() -> None:
    rig = make_rig(rps=0.0001, queue_depth=1)
    client = make_client(rig)
    _translate(client, "Apply online", "Renew a passport")
    m = _metrics(client)
    assert m["dzweb_queue_depth"] == 1
    assert m["dzweb_queue_full_total"] == 1
    assert m["dzweb_queue_oldest_pending_seconds"] >= 0


def test_fr611_review_flags_and_pending_reviews_are_reported() -> None:
    rig = make_rig(review_cap=1)
    client = make_client(rig)
    _translate(client, "Apply online", "Renew a passport")
    m = _metrics(client)
    assert m['dzweb_review_flags_total{outcome="created"}'] == 1
    assert m['dzweb_review_flags_total{outcome="owed"}'] == 1
    assert m["dzweb_review_pending"] == 1
    assert m["dzweb_review_owed"] == 1


def test_fr611_latency_is_a_histogram_of_requests_that_reached_the_service(rig: Rig) -> None:
    client = make_client(rig)
    _translate(client, "Apply online today")
    _translate(client, "Apply online today")
    client.post("/v1/translate", json=body("x"), headers={"Origin": "https://evil.example"})  # 403
    m = _metrics(client)
    assert m["dzweb_request_seconds_count"] == 2
    assert m['dzweb_request_seconds_bucket{le="+Inf"}'] == 2
    assert 0 < m["dzweb_request_seconds_sum"] < 5


def test_fr611_histogram_buckets_are_cumulative() -> None:
    h = Histogram(bounds=(0.1, 0.5))
    for value in (0.05, 0.1, 0.3, 2.0, -1.0):
        h.observe(value)
    assert h.cumulative() == [("0.1", 3), ("0.5", 4), ("+Inf", 5)]
    assert h.total == pytest.approx(2.45)


def test_nfr410_metrics_are_readable_when_the_stores_are_down(rig: Rig) -> None:
    """This is exactly when someone will be reading them."""

    def explode(*_: object, **__: object) -> None:
        raise ConnectionError("down")

    rig.service.queue.depth = explode  # type: ignore[method-assign]
    rig.service.queue.oldest_pending_seconds = explode  # type: ignore[method-assign]
    rig.tm.pending_review = explode  # type: ignore[method-assign]
    m = _metrics(make_client(rig))
    assert "dzweb_queue_depth" not in m and "dzweb_review_pending" not in m
    assert "dzweb_pending_mt_ratio" in m  # everything held in memory is still there


def test_nfr303_metrics_carry_no_text_path_or_client(rig: Rig) -> None:
    client = make_client(rig, client_host="203.0.113.77")
    # Person-shaped but invented: distinctive tokens to look for (docs/CLAUDE.md).
    client.post(
        "/v1/translate",
        json=body("Testperson Examplename of Nowhereton", path="/application/11502001234"),
        headers=H,
    )
    exported = (
        client.get("/v1/metrics", headers=OPS).text + client.get("/v1/health", headers=OPS).text
    )
    for leaked in ("Testperson", "Nowhereton", "11502001234", "application", "203.0.113.77"):
        assert leaked not in exported


def test_fr611_metrics_are_not_readable_from_a_web_page(rig: Rig) -> None:
    response = make_client(rig).get("/v1/metrics", headers={**H, **OPS})
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")


def test_fr611_label_values_are_escaped() -> None:
    family = Family("dzweb_x_total", "counter", "x", (("", (("cause", 'a"b\\c\nd'),), 1.0),))
    assert 'dzweb_x_total{cause="a\\"b\\\\c\\nd"} 1' in render([family])


# --- operator routes are not public (FR-610, FR-611) -----------------------


@pytest.mark.parametrize("route", ["/v1/health", "/v1/metrics"])
def test_fr610_without_the_token_the_route_does_not_exist(rig: Rig, route: str) -> None:
    client = make_client(rig)
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "test-ops-token"}):
        response = client.get(route, headers=headers)
        assert response.status_code == 404
        assert response.json() == client.get("/v1/no-such-route").json()


@pytest.mark.parametrize("route", ["/v1/health", "/v1/metrics"])
def test_fr610_with_no_token_configured_nobody_gets_in(rig: Rig, route: str) -> None:
    """A deployment that forgets to set one exposes nothing."""
    client = make_client(rig, ops_token=None)
    assert client.get(route, headers=OPS).status_code == 404


def test_fr611_database_gauges_are_read_at_most_every_few_seconds(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.ops import metrics

    monkeypatch.setattr(metrics, "GAUGE_REUSE_SECONDS", 60.0)
    reads: list[int] = []
    real = rig.tm.pending_review

    def counted(site_id: str | None = None) -> int:
        reads.append(1)
        return real(site_id)

    rig.tm.pending_review = counted  # type: ignore[method-assign]
    client = make_client(rig)
    for _ in range(10):
        _metrics(client)
    assert len(reads) == 1


def test_fr611_glossary_compliance_ignores_output_whose_markers_could_not_be_read() -> None:
    """Regression: unreadable output counted every term as restored."""
    rig = make_rig(modes={Mode.TRUNCATE: 1.0})
    client = make_client(rig)
    _translate(client, "Pay the fee at the Department of Immigration")
    m = _metrics(client)
    assert m['dzweb_model_outputs_total{result="tag_fallback"}'] == 1
    assert m['dzweb_glossary_terms_total{result="restored"}'] == 0
    assert m['dzweb_glossary_terms_total{result="missing"}'] == 0


def test_fr611_one_missing_term_counts_once_not_for_every_term() -> None:
    rig = make_rig()
    client = make_client(rig)
    real = rig.translator.translate

    async def drop_one(model_text: str, reference: Any) -> str:
        out = await real(model_text, reference)
        return out.replace("⟦T:1⟧", "", 1)

    rig.translator.translate = drop_one  # type: ignore[method-assign]
    _translate(client, "Pay the fee at the Department of Immigration")  # two terms
    m = _metrics(client)
    assert m['dzweb_glossary_terms_total{result="restored"}'] == 1
    assert m['dzweb_glossary_terms_total{result="missing"}'] == 1
