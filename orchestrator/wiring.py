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
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.governance.sites import SiteRegistry
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
from orchestrator.store.migrate import migrate
from orchestrator.store.postgres_tm import PostgresTM
from orchestrator.upstream.quota import QuotaManager
from orchestrator.upstream.translator import MockTranslator, Translator


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
    queue: PostgresJobQueue
    translator: Translator
    fmt: ModelFormat
    quota: QuotaManager
    sites: SiteRegistry
    termbase: Termbase
    conn: Any


def build(settings: Settings) -> Components:
    import psycopg
    import redis

    fmt = MODEL_FORMATS[settings.model_format]
    translator = build_translator(settings, fmt)  # fail fast before touching any service
    termbase = Termbase.load(settings.termbase)
    sites = SiteRegistry.load(settings.sites)
    conn = psycopg.connect(settings.pg_dsn, autocommit=True)
    migrate(conn)
    client = redis.Redis.from_url(settings.redis_url)
    store = TranslationStore(
        PostgresTM(conn),
        ResilientCache(RedisCache(client)),
        ResilientSeenCounter(RedisSeenCounter(client)),
    )
    queue = PostgresJobQueue(conn)
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
    return Components(service, store, queue, translator, fmt, quota, sites, termbase, conn)
