"""Production wiring from environment variables: app, worker and pre-warm share it.

DZWEB_PG_DSN              PostgreSQL DSN (required)
DZWEB_REDIS_URL           Redis URL (required; must run maxmemory-policy volatile-lru)
DZWEB_TERMBASE            path to the published termbase JSON (required)
DZWEB_SITES               path to the enrolled-sites JSON (required)
DZWEB_TRANSLATOR          "wso2" (the GovTech API) or "mock"
DZWEB_ALLOW_MOCK_TRANSLATOR=1   required to run with the mock; it emits "DZ:" + English,
                                which must never reach citizens
DZWEB_WSO2_URL            translate endpoint          } required with
DZWEB_WSO2_TOKEN_URL      OAuth2 token endpoint       } DZWEB_TRANSLATOR=wso2;
DZWEB_WSO2_CLIENT_ID      client credentials          } the secret only ever in
DZWEB_WSO2_CLIENT_SECRET                              } the environment or .env
DZWEB_MODEL_VERSION       the model behind the API, part of every cache key (FR-150).
                          The API does not report it: change this when GovTech
                          changes the model. Default "dsai-translationapi-1.0.0"
DZWEB_WSO2_CONCURRENCY    parallel calls to the API, default 4
DZWEB_MODEL_FORMAT        "wire" (default) or "xml" (decided by Sprint 0)
DZWEB_UPSTREAM_RPS        measured WSO2 limit (S0.2), default 5
DZWEB_WORKER_SHARE        share reserved for the worker (FR-156), default 0.5
DZWEB_AUDIT_ACTOR         who deployed this configuration (a person or a release id);
                          recorded as the actor of enrolment, tier-rule and termbase
                          changes (FR-620). Defaults to "config:<file name>"
DZWEB_OPS_TOKEN           bearer token for /v1/health and /v1/metrics; when unset they
                          answer 404 to everyone (FR-610, FR-611)

For local work these can live in a .env file in the working directory
(git-ignored). A variable set in the real environment always wins over .env.

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
from orchestrator.upstream.wso2 import Wso2Config, Wso2Translator

DEFAULT_MODEL_VERSION = "dsai-translationapi-1.0.0"
WSO2_SETTINGS = (
    "DZWEB_WSO2_URL",
    "DZWEB_WSO2_TOKEN_URL",
    "DZWEB_WSO2_CLIENT_ID",
    "DZWEB_WSO2_CLIENT_SECRET",
)


def environment(dotenv: Path | None = None) -> dict[str, str]:
    """The process environment, filled in from ``.env`` where it says nothing."""
    env = dict(os.environ)
    path = dotenv or Path.cwd() / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env.setdefault(key.strip(), value.strip())
    return env

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
    wso2: Wso2Config | None = None

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> Settings:
        """Settings from ``env``, or from the environment and ``.env`` when not given."""
        env = environment() if env is None else env
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
            wso2=_wso2(env),
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
    if settings.translator == "wso2":
        if settings.wso2 is None:
            raise ConfigError(
                f"DZWEB_TRANSLATOR=wso2 needs {', '.join(WSO2_SETTINGS)} in the environment or .env"
            )
        return Wso2Translator(settings.wso2)
    raise ConfigError(
        "DZWEB_TRANSLATOR must be 'wso2' (the GovTech API) or 'mock'; "
        "for local development use DZWEB_TRANSLATOR=mock with DZWEB_ALLOW_MOCK_TRANSLATOR=1"
    )


def _wso2(env: Mapping[str, str]) -> Wso2Config | None:
    """The API settings, or None when any is missing (reported only if wso2 is chosen)."""
    if not all(env.get(k) for k in WSO2_SETTINGS):
        return None
    try:
        concurrency = int(env.get("DZWEB_WSO2_CONCURRENCY", "4"))
    except ValueError as err:
        raise ConfigError("DZWEB_WSO2_CONCURRENCY must be a whole number") from err
    return Wso2Config(
        url=env["DZWEB_WSO2_URL"],
        token_url=env["DZWEB_WSO2_TOKEN_URL"],
        client_id=env["DZWEB_WSO2_CLIENT_ID"],
        client_secret=env["DZWEB_WSO2_CLIENT_SECRET"],
        model_version=env.get("DZWEB_MODEL_VERSION") or DEFAULT_MODEL_VERSION,
        concurrency=concurrency,
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
