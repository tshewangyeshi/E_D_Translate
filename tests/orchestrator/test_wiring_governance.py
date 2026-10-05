"""Production wiring of the Sprint 4 governance pieces, on real PostgreSQL and Redis.

Requirements: FR-511, FR-610, FR-611, FR-620, NFR-410.

Each piece has unit tests against in-memory stores. These prove the pieces are
actually connected in the process that will run: a log nobody writes to, a
probe nobody registered and a flag nobody sets would all pass their own tests.
They also cover what only a real deployment has: several processes starting
at once, and a database that goes away and comes back.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.governance.audit import Action, PostgresAuditLog
from orchestrator.queue.worker import Worker
from orchestrator.service.translate import SegmentIn
from orchestrator.store.migrate import MIGRATIONS
from orchestrator.store.models import RaisedBy, ReviewState
from orchestrator.testing.rig import OPS, OPS_TOKEN, ORIGIN, TERMBASE, body
from orchestrator.wiring import Components, ConfigError, Settings, build, record_configuration

pytestmark = pytest.mark.integration

PORTAL = {"site_id": "portal", "origins": [ORIGIN], "default_tier": 2}


class Deployment:
    def __init__(self, pg_conn: Any, redis_url: str, tmp_path: Path) -> None:
        self.schema = pg_conn.execute("SELECT current_schema()").fetchone()[0]
        self.dsn = pg_conn.info.dsn + f" options=-csearch_path={self.schema}"
        self.sites = tmp_path / "sites.json"
        self.env = {
            "DZWEB_PG_DSN": self.dsn,
            "DZWEB_REDIS_URL": redis_url,
            "DZWEB_TERMBASE": str(TERMBASE),
            "DZWEB_SITES": str(self.sites),
            "DZWEB_TRANSLATOR": "mock",
            "DZWEB_ALLOW_MOCK_TRANSLATOR": "1",
            "DZWEB_UPSTREAM_RPS": "1000",
            "DZWEB_OPS_TOKEN": OPS_TOKEN,
        }
        self.started: list[Components] = []

    def write_sites(self, *sites: dict[str, Any]) -> None:
        self.sites.write_text(json.dumps({"sites": list(sites)}), encoding="utf-8")

    def start(self, *sites: dict[str, Any], api: bool = True, **env: str) -> Components:
        """One process start, the way main.create() does it for the API."""
        if sites:
            self.write_sites(*sites)
        components = build(Settings.from_env({**self.env, **env}))
        self.started.append(components)
        if api:
            record_configuration(components)
        return components

    def close(self) -> None:
        for components in self.started:
            for conn in (components.conn, components.ops_conn):
                if not conn.closed:
                    conn.close()


@pytest.fixture
def deployment(
    pg_conn: Any, redis_client: Any, redis_url: str, tmp_path: Path
) -> Iterator[Deployment]:
    d = Deployment(pg_conn, redis_url, tmp_path)
    yield d
    d.close()


def _client(c: Components, host: str = "203.0.113.1") -> TestClient:
    app = create_app(
        service=c.service,
        sites=c.sites,
        termbase_version=c.termbase.version,
        reports=c.reports,
        health=c.health,
        gauges=c.gauges,
        ops_token=c.settings.ops_token,
    )
    return TestClient(app, client=(host, 50000))


def _translate_as_three_clients(c: Components, text: str) -> dict[str, Any]:
    # One app, so one salt: three addresses are three clients (NFR-304),
    # counted by the real Redis counter.
    app = create_app(service=c.service, sites=c.sites, termbase_version=c.termbase.version)
    for n in (1, 2, 3):
        response = TestClient(app, client=(f"203.0.113.{n}", 50000)).post(
            "/v1/translate", json=body(text), headers={"Origin": ORIGIN}
        )
    (segment,) = response.json()["segments"]
    return segment


# --- FR-620 ---


def test_fr620_the_running_service_audits_to_postgresql(deployment: Deployment) -> None:
    c = deployment.start(PORTAL)
    assert isinstance(c.audit, PostgresAuditLog)
    assert c.store.audit is c.audit


def test_fr620_starting_the_api_records_the_sites_and_termbase_it_found(
    deployment: Deployment,
) -> None:
    c = deployment.start(PORTAL, DZWEB_AUDIT_ACTOR="release-2026.10.1")
    events = c.audit.events()
    assert sorted(e.action.value for e in events) == [
        "site.enrolment_change",
        "termbase.publish",
        "tier_rule.change",
    ]
    assert {e.actor for e in events} == {"release-2026.10.1"}
    (termbase,) = c.audit.events(action=Action.TERMBASE_PUBLISH)
    assert termbase.subject == f"termbase:{c.termbase.version}"
    assert termbase.detail["change"] == "loaded"
    assert termbase.detail["term_count"] == len(c.termbase.terms)


def test_fr620_the_actor_defaults_to_the_file_the_change_came_from(
    deployment: Deployment,
) -> None:
    c = deployment.start(PORTAL)
    assert {e.actor for e in c.audit.events(action=Action.SITE_ENROLMENT_CHANGE)} == {
        "config:sites.json"
    }
    assert {e.actor for e in c.audit.events(action=Action.TERMBASE_PUBLISH)} == {
        f"config:{TERMBASE.name}"
    }


def test_fr620_restarting_without_changes_records_nothing_new(deployment: Deployment) -> None:
    deployment.start(PORTAL)
    c = deployment.start(PORTAL)
    assert len(c.audit.events(limit=None)) == 3


def test_fr620_the_worker_and_tools_never_write_the_trail(deployment: Deployment) -> None:
    """A tool run with a stale sites file must not record changes that never happened."""
    api = deployment.start(PORTAL)
    deployment.start({**PORTAL, "default_tier": 3}, api=False)  # a worker, a laptop
    deployment.start(api=False)
    assert len(api.audit.events(limit=None)) == 3


def test_fr620_a_loosened_tier_rule_is_on_record_after_the_next_start(
    deployment: Deployment,
) -> None:
    strict = {**PORTAL, "path_rules": [{"path": "/legal/*", "tier": 1}]}
    deployment.start(strict, DZWEB_AUDIT_ACTOR="release-1")
    c = deployment.start(PORTAL, DZWEB_AUDIT_ACTOR="release-2")  # the rule is gone

    latest = c.audit.events(action=Action.TIER_RULE_CHANGE)[0]
    assert latest.actor == "release-2"
    assert latest.detail["change"] == "changed" and latest.detail["path_rules"] == 0


def test_fr620_api_replicas_starting_together_record_a_change_once(
    deployment: Deployment,
) -> None:
    deployment.write_sites(PORTAL)
    replicas = [deployment.start(api=False) for _ in range(3)]
    barrier = threading.Barrier(len(replicas))
    errors: list[BaseException] = []

    def start(c: Components) -> None:
        barrier.wait()
        try:
            record_configuration(c)
        except BaseException as err:  # noqa: BLE001 - reported below
            errors.append(err)

    threads = [threading.Thread(target=start, args=(c,)) for c in replicas]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(replicas[0].audit.events(limit=None)) == 3


def test_fr620_an_approval_through_the_wired_store_is_audited(deployment: Deployment) -> None:
    c = deployment.start(PORTAL)
    site = c.sites.get("portal")
    assert site is not None
    p = c.service.prepare(site, SegmentIn("b0", "Apply online today"), "/services")
    c.store.approve(
        keys=p.keys,
        masked_source=p.with_terms.to_wire(),
        masked_target="approved",
        author="reviewer-9",
        term_ids=(),
        site_id="portal",
    )
    (event,) = c.audit.events(action=Action.REVIEW_APPROVE)
    assert event.actor == "reviewer-9"
    assert event.subject == f"segment:{p.keys.segment_key}"


# --- migrations: several processes, one database ---


def test_nfr410_processes_starting_together_all_start(
    pg_conn: Any, redis_client: Any, redis_url: str, tmp_path: Path
) -> None:
    """The first deploy of a change: every process migrates at once, none may fail."""
    import psycopg

    schema = f"race_{pg_conn.info.backend_pid}"
    pg_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    pg_conn.execute(f"CREATE SCHEMA {schema}")
    d = Deployment(pg_conn, redis_url, tmp_path)
    d.env["DZWEB_PG_DSN"] = pg_conn.info.dsn + f" options=-csearch_path={schema}"
    d.write_sites(PORTAL)
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def start() -> None:
        barrier.wait()
        try:
            d.start(api=False)
        except BaseException as err:  # noqa: BLE001 - reported below
            errors.append(err)

    threads = [threading.Thread(target=start) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        with psycopg.connect(pg_conn.info.dsn, autocommit=True) as check:
            (applied,) = check.execute(
                f"SELECT count(*) FROM {schema}.schema_migration"  # noqa: S608 - our own name
            ).fetchone()
        assert applied == len(list(MIGRATIONS.glob("*.sql")))
    finally:
        d.close()
        pg_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_nfr305_a_read_only_tool_refuses_an_unmigrated_database(
    pg_conn: Any, redis_client: Any, redis_url: str, tmp_path: Path
) -> None:
    """A dry run answers a question; it does not migrate a database to do it."""
    schema = f"fresh_{pg_conn.info.backend_pid}"
    pg_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    pg_conn.execute(f"CREATE SCHEMA {schema}")
    d = Deployment(pg_conn, redis_url, tmp_path)
    d.env["DZWEB_PG_DSN"] = pg_conn.info.dsn + f" options=-csearch_path={schema}"
    d.write_sites(PORTAL)
    try:
        with pytest.raises(ConfigError, match="not migrated"):
            build(Settings.from_env(d.env), apply_migrations=False)
        tables = pg_conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s", (schema,)
        ).fetchone()[0]
        assert tables == 0
    finally:
        pg_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


# --- FR-511 ---


def test_fr511_a_live_translation_is_flagged_in_postgresql(deployment: Deployment) -> None:
    c = deployment.start(PORTAL)
    segment = _translate_as_three_clients(c, "Apply online today")
    assert segment["status"] == "translated"

    item = c.store.tm.review_item(segment["segment_key"])
    assert item is not None and item.state is ReviewState.PENDING_REVIEW
    assert item.site_id == "portal" and item.raised_by is RaisedBy.REQUEST
    assert c.store.tm.pending_review("portal") == 1


def test_fr511_the_wired_worker_flags_what_it_stores(deployment: Deployment) -> None:
    c = deployment.start(PORTAL, DZWEB_UPSTREAM_RPS="0.0001", DZWEB_WORKER_SHARE="0.5")
    segment = _translate_as_three_clients(c, "Renew your passport")
    assert segment["status"] == "pending_mt" and c.queue.depth() == 1

    w = deployment.start(api=False, DZWEB_UPSTREAM_RPS="100")  # the worker process
    worker = Worker(
        queue=w.queue,
        store=w.store,
        translator=w.translator,
        model_format=w.fmt,
        quota=w.quota,
        worker_id="w1",
    )
    assert asyncio.run(worker.run_once()).outcomes["stored"] == 1
    item = c.store.tm.review_item(segment["segment_key"])
    assert item is not None and item.state is ReviewState.PENDING_REVIEW
    assert item.raised_by is RaisedBy.REQUEST


def test_fr511_a_tier2_serve_flags_output_stored_for_tier3_first(
    deployment: Deployment,
) -> None:
    open_site = {"site_id": "open", "origins": ["https://open.gov.example"], "default_tier": 3}
    c = deployment.start(PORTAL, open_site)
    app = create_app(service=c.service, sites=c.sites, termbase_version=c.termbase.version)
    for n in (1, 2, 3):
        TestClient(app, client=(f"203.0.113.{n}", 50000)).post(
            "/v1/translate",
            json=body("Apply online today", site="open"),
            headers={"Origin": "https://open.gov.example"},
        )
    key = _translate_as_three_clients(c, "Apply online today")["segment_key"]
    item = c.store.tm.review_item(key)
    assert item is not None and item.state is ReviewState.PENDING_REVIEW
    assert item.site_id == "portal"


# --- FR-610, FR-611, NFR-410 ---


def test_fr610_the_running_service_reports_all_five_upstreams(deployment: Deployment) -> None:
    c = deployment.start(PORTAL)
    report = _client(c).get("/v1/health", headers=OPS).json()
    assert report["status"] == "ok"
    states = {name: u["state"] for name, u in report["upstreams"].items()}
    assert states == {
        "nmt": "unknown",
        "postgres": "ok",
        "redis": "ok",
        "queue": "ok",
        "quota": "ok",
    }


def test_fr610_without_the_token_the_operator_routes_do_not_exist(
    deployment: Deployment,
) -> None:
    c = deployment.start(PORTAL)
    client = _client(c)
    for route in ("/v1/health", "/v1/metrics"):
        assert client.get(route).status_code == 404
        assert client.get(route, headers={"Authorization": "Bearer wrong"}).status_code == 404


def test_nfr410_a_lost_database_connection_is_reopened(
    deployment: Deployment, pg_conn: Any
) -> None:
    """A database restart must not leave the service in English until someone notices."""
    c = deployment.start(PORTAL)
    client = _client(c)
    assert _translate_as_three_clients(c, "Apply online today")["status"] == "translated"
    c.store.cache.inner.client.flushdb()  # make the next request go to PostgreSQL

    # The server ends every connection the service holds, as a restart or
    # failover would: the whole pool, not one socket.
    killed = pg_conn.execute(
        "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
        " WHERE datname = current_database() AND pid <> pg_backend_pid()"
    ).fetchone()[0]
    assert killed >= 1

    def ask() -> Any:
        return client.post(
            "/v1/translate", json=body("Apply online today"), headers={"Origin": ORIGIN}
        )

    assert ask().status_code == 200  # English at worst, never an error (NFR-410)
    assert ask().json()["segments"][0]["status"] == "translated"


def test_fr611_the_running_service_exports_queue_and_review_gauges(
    deployment: Deployment,
) -> None:
    c = deployment.start(PORTAL)
    _translate_as_three_clients(c, "Apply online today")
    text = _client(c).get("/v1/metrics", headers=OPS).text
    assert "dzweb_queue_depth 0" in text
    assert "dzweb_review_pending 1" in text
    assert "dzweb_review_owed 0" in text
    assert 'dzweb_review_flags_total{outcome="created"} 1' in text
