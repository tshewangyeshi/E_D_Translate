"""Fault injection on a real deployment (S10.4). Requirements: NFR-410, NFR-411, NFR-412, FR-510.

Each case breaks one dependency on purpose and holds the service to the same
three promises: the page gets HTTP 200, English wherever a translation could
not be given, and an operator can see what happened in a metric.

Not here yet: the fetcher cases (a redirect to 169.254.169.254, a DNS answer
that changes between resolve and connect). The fetcher is S8.0, with the proxy.
"""

from __future__ import annotations

import asyncio
import statistics
import time

import pytest

from orchestrator.queue.worker import Worker
from tests.fault.conftest import Deployment, ask, client, metric

pytestmark = pytest.mark.integration

TEXT = "Apply online for a passport."


def _source_text(segments: list[dict[str, str]], texts: tuple[str, ...]) -> bool:
    return [s["text"] for s in segments] == list(texts)


# --- Redis down ---


def test_nfr410_redis_down_serves_from_postgres_within_800ms(deployment: Deployment) -> None:
    texts = tuple(f"Service {n}: apply online for a passport." for n in range(64))
    warm = deployment.start()
    status, segments = ask(client(warm), *texts)
    assert status == 200 and all(s["status"] == "translated" for s in segments)

    # Another process starts with Redis gone: the cached batch comes from PostgreSQL.
    cold = deployment.start(DZWEB_REDIS_URL="redis://127.0.0.1:1/0")
    api = client(cold)
    times = []
    for _ in range(20):
        started = time.perf_counter()
        status, segments = ask(api, *texts)
        times.append(time.perf_counter() - started)
        assert status == 200
        assert all(s["status"] == "translated" for s in segments)
    p95 = statistics.quantiles(times, n=20)[-1]
    assert p95 < 0.8, (
        f"p95 {p95 * 1000:.0f} ms on a 64-segment cached batch with Redis down [ER-21]"
    )
    assert metric(api, "dzweb_cache_failures_total") > 0


# --- PostgreSQL down ---


def test_nfr410_postgres_down_answers_english_and_tier1_fails_closed(
    deployment: Deployment,
) -> None:
    c = deployment.start()
    api = client(c)
    assert ask(api, TEXT)[1][0]["status"] == "translated"
    c.conn.close()  # the database is gone

    # Already in the cache: still served. Losing the database costs nothing cached.
    assert ask(api, TEXT)[1][0]["status"] == "translated"

    # Not in the cache: English, Tier 2 and Tier 1 alike (FR-510 fails closed).
    other = "Renew your driving licence online."
    for path in ("/services/renewal", "/legal/terms"):
        status, segments = ask(api, other, path=path)
        assert status == 200
        assert _source_text(segments, (other,)), f"{path} served something other than English"
        assert segments[0]["status"] == "upstream_error"
    assert metric(api, "dzweb_fallback_total", lambda line: "store_unavailable" in line) >= 2


# --- the model: slow, failing, talking nonsense ---


@pytest.mark.parametrize(
    ("mode", "status", "kind"),
    [
        ("slow", "pending_mt", "timeout"),
        ("503", "upstream_error", "UpstreamUnavailable"),
        ("garbage", "upstream_error", "UpstreamBadResponse"),
    ],
)
def test_nfr410_a_failing_model_leaves_the_page_english(
    deployment: Deployment, mode: str, status: str, kind: str
) -> None:
    c = deployment.start()
    deployment.gateway.mode = mode
    api = client(c)
    started = time.perf_counter()
    code, segments = ask(api, TEXT)
    elapsed = time.perf_counter() - started
    assert code == 200
    assert segments[0]["status"] == status and _source_text(segments, (TEXT,))
    assert elapsed < 3, f"the page waited {elapsed:.1f}s for a failing model (FR-155)"
    assert metric(api, "dzweb_upstream_errors_total", lambda line: kind in line) >= 1
    assert c.queue.depth() == 1, "the segment was not queued for a retry"


# --- the worker ---


def test_nfr410_a_job_held_by_a_dead_worker_is_reclaimed(deployment: Deployment) -> None:
    c = deployment.start()
    deployment.gateway.mode = "slow"
    api = client(c)
    assert ask(api, TEXT)[1][0]["status"] == "pending_mt"
    deployment.gateway.mode = "ok"

    (job,) = c.queue.claim("worker-that-dies", 10, lease_seconds=0.5)  # ... and never finishes
    assert c.queue.claim("worker-2", 10, lease_seconds=60) == [], "a held job was handed out twice"
    time.sleep(1.0)
    assert c.queue.sweep() == 1  # the lease ran out: the job is available again

    worker = Worker(
        queue=c.queue,
        store=c.store,
        translator=c.service.translator,
        model_format=c.fmt,
        quota=c.quota,
        worker_id="worker-2",
    )
    asyncio.run(worker.run_once())
    assert c.queue.depth() == 0
    assert ask(api, TEXT)[1][0]["status"] == "translated"
    assert metric(api, "dzweb_queue_depth") == 0


def test_nfr305_retention_also_drops_the_cached_copies(deployment: Deployment) -> None:
    c = deployment.start()
    api = client(c)
    assert ask(api, TEXT)[1][0]["status"] == "translated"
    calls = deployment.gateway.calls
    from datetime import UTC, datetime, timedelta

    assert c.store.expire_machine(datetime.now(UTC) + timedelta(seconds=1)) >= 1
    status, segments = ask(api, TEXT)
    assert status == 200 and segments[0]["status"] == "translated"
    assert deployment.gateway.calls > calls, "an expired translation was served from the cache"


# --- overload ---


def test_nfr412_a_stampede_is_shed_to_a_bounded_queue(deployment: Deployment) -> None:
    c = deployment.start(DZWEB_UPSTREAM_RPS="2")  # one live call a second for requests
    c.queue.max_depth = 5
    api = client(c)
    texts = tuple(f"Notice {n}: offices close early today." for n in range(40))
    started = time.perf_counter()
    status, segments = ask(api, *texts)
    elapsed = time.perf_counter() - started
    assert status == 200 and elapsed < 3
    assert all(s["status"] in ("translated", "pending_mt") for s in segments)
    assert sum(s["status"] == "pending_mt" for s in segments) >= 30, "work was not shed"
    assert c.queue.depth() == 5, "the queue grew past its bound"
    assert metric(api, "dzweb_queue_full_total") >= 1
    for s, text in zip(segments, texts, strict=True):
        if s["status"] == "pending_mt":
            assert s["text"] == text


def test_nfr412_with_live_quota_gone_the_worker_still_proceeds(deployment: Deployment) -> None:
    c = deployment.start(DZWEB_UPSTREAM_RPS="2")
    while c.quota.try_live():  # the request path's share is spent
        pass
    api = client(c)
    status, segments = ask(api, TEXT)
    assert status == 200 and segments[0]["status"] == "pending_mt"
    assert metric(api, "dzweb_fallback_total", lambda line: "no_live_quota" in line) >= 1

    worker = Worker(
        queue=c.queue,
        store=c.store,
        translator=c.service.translator,
        model_format=c.fmt,
        quota=c.quota,
        worker_id="worker",
    )
    asyncio.run(worker.run_once())  # its own share of the quota (FR-156)
    assert ask(api, TEXT)[1][0]["status"] == "translated"
