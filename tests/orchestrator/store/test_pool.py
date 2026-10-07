"""The PostgreSQL pool, and store work off the event loop (S2.4). Requirements: NFR-100, NFR-410.

One connection per process serialised every request. The pool gives requests
in flight their own connections; a transaction, or a block that relies on
session state, keeps one connection throughout.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

from orchestrator.store.pg import PooledConnection, Rows, pinned
from tests.orchestrator.conftest import PG_DSN, requires_pg

pytestmark = [pytest.mark.integration, requires_pg]


@pytest.fixture
def pooled() -> Iterator[PooledConnection]:
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(PG_DSN, kwargs={"autocommit": True}, min_size=1, max_size=3, open=True)
    conn = PooledConnection(pool)
    yield conn
    conn.close()


def _pid(conn: Any) -> int:
    return int(conn.execute("SELECT pg_backend_pid()").fetchone()[0])


def test_s24_requests_in_flight_together_get_their_own_connections(
    pooled: PooledConnection,
) -> None:
    pids: list[int] = []
    both_in = threading.Barrier(2)

    def request() -> None:
        with pooled.transaction():  # holds its connection, as a busy request would
            both_in.wait(timeout=10)
            pids.append(_pid(pooled))

    threads = [threading.Thread(target=request) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(set(pids)) == 2, "two requests shared one connection"


def test_s24_a_transaction_keeps_one_connection(pooled: PooledConnection) -> None:
    with pooled.transaction():
        first = _pid(pooled)
        assert all(_pid(pooled) == first for _ in range(5))
        with pooled.transaction():  # nested: a savepoint, same connection
            assert _pid(pooled) == first


def test_nfr410_a_session_lock_is_released_where_it_was_taken(pooled: PooledConnection) -> None:
    """The bug the pool first had: migrate() locked on one connection, unlocked on another."""
    with pinned(pooled):
        pooled.execute("SELECT pg_advisory_lock(424242)")
        pooled.execute("SELECT pg_advisory_unlock(424242)")
    held = pooled.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 424242"
    ).fetchone()[0]
    assert held == 0


def test_s24_results_stay_readable_after_the_connection_went_back(pooled: PooledConnection) -> None:
    rows = pooled.execute("SELECT n FROM generate_series(1, 3) AS n")
    assert isinstance(rows, Rows)
    assert rows.fetchone() == (1,)
    assert rows.fetchall() == [(2,), (3,)]
    assert rows.fetchone() is None
    assert pooled.execute("SELECT 1 WHERE false").rowcount == 0
