"""Wiring for S2.1 tests: the real service and app over in-memory stores and the mock translator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.ratelimit import RateLimiter
from orchestrator.governance.sites import Site, SiteRegistry
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

TERMBASE = Path(__file__).resolve().parents[2] / "fixtures" / "glossary" / "termbase-sample.json"
ORIGIN = "https://portal.gov.example"
LEGAL_ORIGIN = "https://legal.gov.example"


@dataclass
class Rig:
    service: TranslateService
    tm: InMemoryTM
    cache: InMemoryCache
    queue: InMemoryQueue
    translator: MockTranslator
    sites: SiteRegistry


def make_rig(
    *,
    modes: dict[Mode, float] | None = None,
    delay: float = 0.0,
    budget: float = 1.5,
    clients_to_persist: int = 1,
    rps: float = 1000.0,
    queue_depth: int = 10_000,
) -> Rig:
    tm, cache = InMemoryTM(), InMemoryCache()
    store = TranslationStore(
        tm,
        ResilientCache(cache),
        InMemorySeenCounter(),
        StoreSettings(distinct_clients_to_persist=clients_to_persist),
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
            Site("portal", frozenset({ORIGIN}), default_tier=2, tier1_selectors=(".fees",)),
            Site("legal", frozenset({LEGAL_ORIGIN}), default_tier=1),
        ]
    )
    return Rig(service, tm, cache, queue, translator, sites)


def make_client(rig: Rig, *, client_host: str = "203.0.113.10", **limits: Any) -> TestClient:
    app = create_app(
        service=rig.service,
        sites=rig.sites,
        termbase_version="sample-2026.09.1",
        origin_limiter=limits.get("origin_limiter", RateLimiter(per_minute=60_000, burst=10_000)),
        client_limiter=limits.get("client_limiter", RateLimiter(per_minute=60_000, burst=10_000)),
    )
    return TestClient(app, client=(client_host, 50000))


@pytest.fixture
def rig() -> Rig:
    return make_rig()


@pytest.fixture
def client(rig: Rig) -> TestClient:
    return make_client(rig)


def body(*texts: str, site: str = "portal", **extra: Any) -> dict[str, Any]:
    return {
        "site": site,
        "path": "/services/renewal",
        "segments": [{"id": f"b{n}", "text": t} for n, t in enumerate(texts)],
        **extra,
    }
