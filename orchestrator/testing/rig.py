"""Test wiring: the real service and app over in-memory stores and the mock translator.

Shared by the API, queue and worker tests. Test support only; never used in production wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from orchestrator.api.app import ClientHasher, create_app
from orchestrator.api.ratelimit import RateLimiter
from orchestrator.governance.audit import InMemoryAuditLog
from orchestrator.governance.sites import PathRule, Site, SiteRegistry
from orchestrator.pipeline.glossary import Termbase
from orchestrator.pipeline.segment import MODEL_FORMATS
from orchestrator.queue.jobs import InMemoryQueue
from orchestrator.service.translate import ServiceSettings, TranslateService
from orchestrator.store.cache import InMemoryCache, InMemorySeenCounter, ResilientCache
from orchestrator.store.lookup import StoreSettings, TranslationStore
from orchestrator.store.tm import InMemoryTM
from orchestrator.testing.mock_nmt import Mode
from orchestrator.upstream.quota import QuotaManager
from orchestrator.upstream.translator import MockTranslator

TERMBASE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "glossary" / "termbase-sample.json"
)
ORIGIN = "https://portal.gov.example"
LEGAL_ORIGIN = "https://legal.gov.example"
OPEN_ORIGIN = "https://open.gov.example"
#: Operator token for /v1/health and /v1/metrics in tests (never a real secret).
OPS_TOKEN = "test-ops-token"  # noqa: S105 - a fixed test value, never deployed
OPS = {"Authorization": f"Bearer {OPS_TOKEN}"}


@dataclass
class Rig:
    service: TranslateService
    tm: InMemoryTM
    cache: InMemoryCache
    queue: InMemoryQueue
    translator: MockTranslator
    sites: SiteRegistry
    audit: InMemoryAuditLog
    seen: InMemorySeenCounter


def make_rig(
    *,
    modes: dict[Mode, float] | None = None,
    delay: float = 0.0,
    budget: float = 1.5,
    clients_to_persist: int = 1,
    rps: float = 1000.0,
    queue_depth: int = 10_000,
    review_cap: int = 500,
    today: Any = None,
) -> Rig:
    tm, cache = InMemoryTM(), InMemoryCache()
    audit = InMemoryAuditLog()
    seen = InMemorySeenCounter(today) if today is not None else InMemorySeenCounter()
    store = TranslationStore(
        tm,
        ResilientCache(cache),
        seen,
        StoreSettings(
            distinct_clients_to_persist=clients_to_persist,
            review_items_per_site_per_day=review_cap,
        ),
        audit=audit,
    )
    translator = MockTranslator(MODEL_FORMATS["wire"], modes=modes, delay_seconds=delay)
    queue = InMemoryQueue(max_depth=queue_depth)
    service = TranslateService(
        store=store,
        termbase=Termbase.load(TERMBASE),
        translator=translator,
        model_format=MODEL_FORMATS["wire"],
        queue=queue,
        quota=QuotaManager(requests_per_second=rps, worker_share=0.5),
        pipeline_version="p-test",
        settings=ServiceSettings(live_budget_seconds=budget),
    )
    sites = SiteRegistry(
        [
            Site(
                "portal",
                frozenset({ORIGIN}),
                default_tier=2,
                tier1_selectors=(".fees",),
                path_rules=(PathRule("/legal/*", 1),),
            ),
            Site("legal", frozenset({LEGAL_ORIGIN}), default_tier=1),
            Site("open", frozenset({OPEN_ORIGIN}), default_tier=3),
        ]
    )
    return Rig(service, tm, cache, queue, translator, sites, audit, seen)


def make_client(rig: Rig, *, client_host: str = "203.0.113.10", **limits: Any) -> TestClient:
    app = create_app(
        service=rig.service,
        sites=rig.sites,
        termbase_version="sample-2026.09.1",
        origin_limiter=limits.get("origin_limiter", RateLimiter(per_minute=60_000, burst=10_000)),
        client_limiter=limits.get("client_limiter", RateLimiter(per_minute=60_000, burst=10_000)),
        hasher=limits.get("hasher") or ClientHasher(),
        health=limits.get("health"),
        ops_token=limits.get("ops_token", OPS_TOKEN),
    )
    return TestClient(app, client=(client_host, 50000))


def body(*texts: str, site: str = "portal", **extra: Any) -> dict[str, Any]:
    return {
        "site": site,
        "path": "/services/renewal",
        "segments": [{"id": f"b{n}", "text": t} for n, t in enumerate(texts)],
        **extra,
    }


class FakeClock:
    """Deterministic time for queue tests (leases, backoff)."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
