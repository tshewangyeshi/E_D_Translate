"""Pre-warm (S2.4) and production wiring. Requirements: FR-155, FR-510, NFR-304."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.ops.prewarm import prewarm
from orchestrator.pipeline.segment import MODEL_FORMATS
from orchestrator.queue.jobs import PRIORITY_PREWARM
from orchestrator.queue.worker import Worker
from orchestrator.testing.rig import ORIGIN, TERMBASE, body, make_rig
from orchestrator.upstream.quota import QuotaManager
from orchestrator.upstream.translator import MockTranslator
from orchestrator.wiring import ConfigError, Settings, build_translator

# "portal" is Tier 2 by default with a Tier 1 rule on /legal/*, so the page path
# each snapshot was served at decides what may be machine-translated (FR-512).
PAGE = "/services/renewal"
SEGMENTS = [
    {"text": "Passport renewal", "tier": None, "selector_tier": None, "path": PAGE},
    {"text": "Pay ⟦1⟧Nu. 500⟦/1⟧ online.", "tier": None, "selector_tier": None, "path": PAGE},
    {"text": "Fee table", "tier": None, "selector_tier": 1, "path": PAGE},
    {"text": "Passport renewal", "tier": None, "selector_tier": None, "path": PAGE},  # repeated
    {"text": "Broken ⟦1⟧ markup", "tier": None, "selector_tier": None, "path": PAGE},
]


def test_fr155_prewarm_queues_tier2_skips_duplicates_and_lists_tier1_for_review() -> None:
    rig = make_rig(clients_to_persist=3)  # NFR-304 threshold does not apply to reviewed snapshots
    site = rig.sites.get("portal")
    assert site is not None
    report = prewarm(rig.service, site, SEGMENTS)
    assert report.queued == 2 and report.invalid == 1
    assert len(report.tier1_needs_review) == 1  # FR-510: never machine-translated
    assert all(j.priority == PRIORITY_PREWARM for j in rig.queue.jobs.values())
    assert rig.translator.calls == 0  # pre-warm only queues; the worker translates


def test_fr155_prewarm_then_worker_then_page_is_served_without_waiting() -> None:
    rig = make_rig(rps=0.0001, clients_to_persist=3)
    rig.queue.translated = lambda key: key in rig.tm.lookup([], [key])[1]
    site = rig.sites.get("portal")
    assert site is not None
    prewarm(rig.service, site, SEGMENTS)
    worker = Worker(
        queue=rig.queue,
        store=rig.service.store,
        translator=MockTranslator(MODEL_FORMATS["wire"]),
        model_format=MODEL_FORMATS["wire"],
        quota=QuotaManager(requests_per_second=100.0),
        worker_id="w1",
    )
    assert asyncio.run(worker.run_once()).outcomes["stored"] == 2
    from orchestrator.testing.rig import make_client

    client = make_client(rig)
    resp = client.post(
        "/v1/translate", json=body("Pay ⟦1⟧Nu. 750⟦/1⟧ online."), headers={"Origin": ORIGIN}
    )
    (s,) = resp.json()["segments"]
    assert s["status"] == "translated" and "Nu. 750" in s["text"]
    assert prewarm(rig.service, site, SEGMENTS).already_translated == 2


# --- wiring guards ---

_ENV = {
    "DZWEB_PG_DSN": "postgresql://x@127.0.0.1:1/x",
    "DZWEB_REDIS_URL": "redis://127.0.0.1:1/0",
    "DZWEB_TERMBASE": str(TERMBASE),
    "DZWEB_SITES": "sites.json",
}


def test_fr155_settings_require_the_essentials() -> None:
    with pytest.raises(ConfigError, match="DZWEB_PG_DSN"):
        Settings.from_env({})
    with pytest.raises(ConfigError, match="DZWEB_MODEL_FORMAT"):
        Settings.from_env({**_ENV, "DZWEB_MODEL_FORMAT": "nope"})


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"DZWEB_TRANSLATOR": "mock"}, "ALLOW_MOCK"),
        ({}, "no real translator"),
    ],
)
def test_fr155_mock_translator_needs_explicit_opt_in(extra: dict[str, str], message: str) -> None:
    settings = Settings.from_env({**_ENV, **extra})
    with pytest.raises(ConfigError, match=message):
        build_translator(settings, MODEL_FORMATS["wire"])


def test_fr155_mock_translator_with_opt_in() -> None:
    settings = Settings.from_env(
        {**_ENV, "DZWEB_TRANSLATOR": "mock", "DZWEB_ALLOW_MOCK_TRANSLATOR": "1"}
    )
    assert isinstance(build_translator(settings, MODEL_FORMATS["wire"]), MockTranslator)


@pytest.mark.integration
def test_fr155_production_wiring_serves_a_request(
    pg_conn: Any, redis_client: Any, redis_url: str, tmp_path: Path
) -> None:
    from orchestrator.api.app import create_app
    from orchestrator.wiring import build

    schema = pg_conn.execute("SELECT current_schema()").fetchone()[0]
    sites = tmp_path / "sites.json"
    sites.write_text(
        json.dumps({"sites": [{"site_id": "portal", "origins": [ORIGIN], "default_tier": 2}]}),
        encoding="utf-8",
    )
    dsn = pg_conn.info.dsn + f" options=-csearch_path={schema}"
    c = build(
        Settings.from_env(
            {
                "DZWEB_PG_DSN": dsn,
                "DZWEB_REDIS_URL": redis_url,
                "DZWEB_TERMBASE": str(TERMBASE),
                "DZWEB_SITES": str(sites),
                "DZWEB_TRANSLATOR": "mock",
                "DZWEB_ALLOW_MOCK_TRANSLATOR": "1",
                "DZWEB_UPSTREAM_RPS": "1000",
            }
        )
    )
    try:
        app = create_app(service=c.service, sites=c.sites, termbase_version=c.termbase.version)
        client = TestClient(app)
        statuses = []
        for n in range(3):  # three distinct clients (NFR-304) via real Redis counter
            resp = TestClient(app, client=(f"203.0.113.{n + 1}", 50000)).post(
                "/v1/translate", json=body("Apply online today"), headers={"Origin": ORIGIN}
            )
            statuses.append(resp.json()["segments"][0]["status"])
        assert statuses == ["pending_mt", "pending_mt", "translated"]
        again = client.post(
            "/v1/translate", json=body("Apply online today"), headers={"Origin": ORIGIN}
        )
        assert again.json()["segments"][0]["status"] == "translated"
    finally:
        c.conn.close()


def test_fr510_prewarm_never_queues_a_tier1_path() -> None:
    """A snapshot of /legal/* is listed for human review, not sent to the model."""
    rig = make_rig(clients_to_persist=3)
    site = rig.sites.get("portal")
    assert site is not None
    legal = [{**s, "path": "/legal/notice"} for s in SEGMENTS]
    report = prewarm(rig.service, site, legal)
    assert report.queued == 0
    assert len(report.tier1_needs_review) == 3  # the 4 valid segments, deduplicated
    assert rig.queue.depth() == 0


def test_fr512_prewarm_without_a_path_does_not_machine_translate() -> None:
    """Missing path on a site with path rules is Tier 1, never a silent Tier 2."""
    rig = make_rig(clients_to_persist=3)
    site = rig.sites.get("portal")
    assert site is not None
    report = prewarm(rig.service, site, [{**s, "path": None} for s in SEGMENTS])
    assert report.queued == 0
