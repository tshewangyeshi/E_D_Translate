"""Store fixtures: in-memory always; PostgreSQL/Redis when reachable (docker compose up -d).

Integration parameters are marked ``integration`` and skipped WITH A REASON when
the services are down; tools/check.py reports that explicitly as NOT RUN.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from orchestrator.store.tm import InMemoryTM

# Throwaway local container from docker-compose.yml (trust auth, 127.0.0.1 only): no credential.
PG_DSN = os.environ.get("DZWEB_TEST_PG_DSN", "postgresql://dzweb@127.0.0.1:55432/dzweb_test")
REDIS_URL = os.environ.get("DZWEB_TEST_REDIS_URL", "redis://127.0.0.1:56379/15")


def postgres_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(PG_DSN, connect_timeout=2):
            return True
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return False


def redis_available() -> bool:
    try:
        import redis

        return bool(redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2).ping())
    except Exception:  # noqa: BLE001
        return False


_PG = postgres_available()
_REDIS = redis_available()
_SKIP_PG = pytest.mark.skipif(not _PG, reason="PostgreSQL not reachable: docker compose up -d")
_SKIP_REDIS = pytest.mark.skipif(not _REDIS, reason="Redis not reachable: docker compose up -d")


@pytest.fixture
def pg_conn() -> Iterator[Any]:
    """A fresh schema per test, migrated, dropped afterwards."""
    if not _PG:
        pytest.skip("PostgreSQL not reachable: docker compose up -d")
    import psycopg

    from orchestrator.store.migrate import migrate

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        try:
            migrate(conn)
            yield conn
        finally:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(
    params=[
        "memory",
        pytest.param("postgres", marks=[pytest.mark.integration, _SKIP_PG]),
    ]
)
def tm(request: pytest.FixtureRequest) -> Any:
    if request.param == "memory":
        return InMemoryTM()
    from orchestrator.store.postgres_tm import PostgresTM

    return PostgresTM(request.getfixturevalue("pg_conn"))


@pytest.fixture
def redis_client() -> Iterator[Any]:
    if not _REDIS:
        pytest.skip("Redis not reachable: docker compose up -d")
    import redis

    client = redis.Redis.from_url(REDIS_URL)
    client.flushdb()
    yield client
    client.flushdb()


requires_pg = _SKIP_PG
requires_redis = _SKIP_REDIS
