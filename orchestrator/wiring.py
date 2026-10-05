"""Production wiring from environment variables: app, worker and pre-warm share it.

DZWEB_PG_DSN              PostgreSQL DSN (required)
DZWEB_REDIS_URL           Redis URL (required; must run maxmemory-policy volatile-lru)
DZWEB_TERMBASE            path to the published termbase JSON (required)
DZWEB_SITES               path to the enrolled-sites JSON (required)
DZWEB_TRANSLATOR          "mock" until the WSO2 client exists (S0.1)
DZWEB_ALLOW_MOCK_TRANSLATOR=1   required to run with the mock; it emits "DZ:" + English,
                                which must never reach citizens
DZWEB_MODEL_FORMAT        "wire" (default) or "xml" (decided by Sprint 0)
DZWEB_UPSTREAM_RPS        measured WSO2 limit (S0.2), default 5
DZWEB_WORKER_SHARE        share reserved for the worker (FR-156), default 0.5
DZWEB_AUDIT_ACTOR         who deployed this configuration (a person or a release id);
                          recorded as the actor of enrolment, tier-rule and termbase
                          changes (FR-620). Defaults to "config:<file name>"
DZWEB_OPS_TOKEN           bearer token for /v1/health and /v1/metrics; when unset they
                          answer 404 to everyone (FR-610, FR-611)

Connections: one PostgreSQL connection for the stores, reopened if the server
goes away (store/pg.py), and a second, short-timeout one for health checks and
metrics, so an operator's scrape never queues behind a citizen's request and a
hung database cannot hang the scrape. Redis calls time out too.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.governance.audit import (
    AuditLog,
    PostgresAuditLog,
    RecordedEvent,
    audit_site_changes,
    audit_termbase_load,
)
from orchestrator.governance.sites import SiteRegistry
from orchestrator.ops.health import Probe, postgres_probe, queue_probe, redis_probe
from orchestrator.ops.metrics import Gauges
from orchestrator.pipeline.glossary import Termbase
from orchestrator.pipeline.segment import MODEL_FORMATS, ModelFormat
from orchestrator.pipeline.version import pipeline_version
from orchestrator.queue.postgres_queue import PostgresJobQueue
from orchestrator.service.translate import TranslateService
from orchestrator.store.cache import (
    RedisCache,
    RedisSeenCounter,
    ResilientCache,
    ResilientSeenCounter,
)
from orchestrator.store.lookup import TranslationStore
from orchestrator.store.migrate import LOCK_NAME, migrate, pending
from orchestrator.store.pg import ReconnectingConnection, advisory_lock
from orchestrator.store.postgres_tm import PostgresTM
from orchestrator.store.reports import PostgresReportStore, ReportStore
from orchestrator.upstream.quota import QuotaManager
from orchestrator.upstream.translator import MockTranslator, Translator

#: Seconds. Short: they bound how long a dead dependency can hold a thread.
CONNECT_TIMEOUT = 5
OPS_STATEMENT_TIMEOUT_MS = 2000
REDIS_TIMEOUT = 2.0


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    pg_dsn: str
    redis_url: str
    termbase: Path
    sites: Path
    translator: str
    allow_mock: bool
    model_format: str
    upstream_rps: float
    worker_share: float
    audit_actor: str = ""
    ops_token: str = ""

    @staticmethod
    def from_env(env: Mapping[str, str] = os.environ) -> Settings:
        missing = [
            k
            for k in ("DZWEB_PG_DSN", "DZWEB_REDIS_URL", "DZWEB_TERMBASE", "DZWEB_SITES")
            if not env.get(k)
        ]
        if missing:
            raise ConfigError(f"missing required settings: {', '.join(missing)}")
        settings = Settings(
            pg_dsn=env["DZWEB_PG_DSN"],
            redis_url=env["DZWEB_REDIS_URL"],
            termbase=Path(env["DZWEB_TERMBASE"]),
            sites=Path(env["DZWEB_SITES"]),
            translator=env.get("DZWEB_TRANSLATOR", ""),
            allow_mock=env.get("DZWEB_ALLOW_MOCK_TRANSLATOR") == "1",
            model_format=env.get("DZWEB_MODEL_FORMAT", "wire"),
            upstream_rps=float(env.get("DZWEB_UPSTREAM_RPS", "5")),
            worker_share=float(env.get("DZWEB_WORKER_SHARE", "0.5")),
            audit_actor=env.get("DZWEB_AUDIT_ACTOR", ""),
            ops_token=env.get("DZWEB_OPS_TOKEN", ""),
        )
        if settings.model_format not in MODEL_FORMATS:
            raise ConfigError(f"DZWEB_MODEL_FORMAT must be one of {sorted(MODEL_FORMATS)}")
        return settings


def build_translator(settings: Settings, fmt: ModelFormat) -> Translator:
    if settings.translator == "mock":
        if not settings.allow_mock:
            raise ConfigError(
                "DZWEB_TRANSLATOR=mock emits fake output; set DZWEB_ALLOW_MOCK_TRANSLATOR=1 "
                "only for local development"
            )
        return MockTranslator(fmt)
    raise ConfigError(
        "no real translator configured: the WSO2 client arrives with Sprint 0 (S0.1); "
        "for local development use DZWEB_TRANSLATOR=mock with DZWEB_ALLOW_MOCK_TRANSLATOR=1"
    )


@dataclass
class Components:
    service: TranslateService
    store: TranslationStore
    reports: ReportStore
    queue: PostgresJobQueue
    translator: Translator
    fmt: ModelFormat
    quota: QuotaManager
    sites: SiteRegistry
    termbase: Termbase
    conn: Any
    audit: AuditLog
    health: dict[str, Probe]
    gauges: Gauges
    ops_conn: Any
    settings: Settings


def build(settings: Settings, *, apply_migrations: bool = True) -> Components:
    """Connect everything. ``apply_migrations=False`` is for read-only tools.

    Nothing here writes to the audit trail: that is ``record_configuration``,
    called by the API process only.
    """
    import psycopg
    import redis
    from psycopg.conninfo import conninfo_to_dict

    fmt = MODEL_FORMATS[settings.model_format]
    translator = build_translator(settings, fmt)  # fail fast before touching any service
    termbase = Termbase.load(settings.termbase)
    sites = SiteRegistry.load(settings.sites)
    conn = ReconnectingConnection(
        lambda: psycopg.connect(
            settings.pg_dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT
        )
    )
    if apply_migrations:
        migrate(conn)
    elif missing := pending(conn):
        raise ConfigError(f"database is not migrated ({', '.join(missing)} pending)")
    # Added to whatever options the DSN already carries, never in place of them.
    dsn_options = conninfo_to_dict(settings.pg_dsn).get("options") or ""
    ops_options = f"{dsn_options} -c statement_timeout={OPS_STATEMENT_TIMEOUT_MS}".strip()
    ops_conn = ReconnectingConnection(
        lambda: psycopg.connect(
            settings.pg_dsn,
            autocommit=True,
            connect_timeout=CONNECT_TIMEOUT,
            options=ops_options,
        )
    )
    audit = PostgresAuditLog(conn)
    client = redis.Redis.from_url(
        settings.redis_url, socket_timeout=REDIS_TIMEOUT, socket_connect_timeout=REDIS_TIMEOUT
    )
    store = TranslationStore(
        PostgresTM(conn),
        ResilientCache(RedisCache(client)),
        ResilientSeenCounter(RedisSeenCounter(client)),
        audit=audit,
        atomic=conn.transaction,  # a change and its audit record commit together
    )
    queue = PostgresJobQueue(conn)
    ops_queue = PostgresJobQueue(ops_conn)
    ops_tm = PostgresTM(ops_conn)
    quota = QuotaManager(settings.upstream_rps, settings.worker_share)
    service = TranslateService(
        store=store,
        termbase=termbase,
        translator=translator,
        model_format=fmt,
        queue=queue,
        quota=quota,
        pipeline_version=pipeline_version(),
    )
    return Components(
        service,
        store,
        PostgresReportStore(conn),
        queue,
        translator,
        fmt,
        quota,
        sites,
        termbase,
        conn,
        audit,
        {
            "postgres": postgres_probe(ops_conn),
            "redis": redis_probe(client),
            "queue": queue_probe(ops_queue),
        },
        {
            "dzweb_queue_depth": ("Jobs waiting for the worker.", ops_queue.depth),
            "dzweb_queue_oldest_pending_seconds": (
                "Age of the oldest job still waiting.",
                lambda: ops_queue.oldest_pending_seconds() or 0.0,
            ),
            "dzweb_review_pending": (
                "Machine translations waiting for a reviewer.",
                ops_tm.pending_review,
            ),
            "dzweb_review_owed": (
                "Review items past the daily cap, not yet released to reviewers.",
                ops_tm.owed,
            ),
        },
        ops_conn,
        settings,
    )


def record_configuration(c: Components) -> list[RecordedEvent]:
    """Write down what changed in the sites and termbase files (FR-620). API only.

    Held under the migration lock, so API replicas starting together record a
    change once. Tools and the worker never call this: a tool run from a laptop
    with a stale sites file must not write "removed" into the trail.
    """
    actor = c.settings.audit_actor
    termbase_file = c.settings.termbase
    with advisory_lock(c.conn, LOCK_NAME):
        recorded = audit_site_changes(
            c.audit, c.sites, actor or f"config:{c.settings.sites.name}"
        )
        loaded = audit_termbase_load(
            c.audit,
            c.termbase.version,
            hashlib.sha256(termbase_file.read_bytes()).hexdigest(),
            len(c.termbase.terms),
            actor or f"config:{termbase_file.name}",
        )
    return recorded + ([loaded] if loaded is not None else [])
