"""A PostgreSQL connection that survives the server going away (NFR-410).

Every store in a process shares one psycopg connection (the pool is a separate
item in TODOS.md). psycopg never reopens a connection once it is closed, so a
database restart or failover used to leave the process answering every page in
English for good: each call raised, health said "degraded", and nothing ever
restarted it.

This wrapper opens a fresh connection when the current one is closed, on the
next call. A call that fails part way still fails -- the caller already treats
that as "storage unavailable" and answers with source text -- but the call
after it works again.

It is deliberately not a retry: a statement is never sent twice, because a
write that may already have committed must not be repeated.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, cast

log = logging.getLogger(__name__)


class ReconnectingConnection:
    """The parts of ``psycopg.Connection`` the stores use, reopening when closed."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect
        self._lock = threading.Lock()
        self._conn = connect()
        self.reconnects = 0

    def current(self) -> Any:
        """The live connection, reopened first if the last one was closed."""
        with self._lock:
            if self._conn.closed:
                log.warning("PostgreSQL connection was closed; reconnecting")
                self._conn = self._connect()  # raises if the server is still away
                self.reconnects += 1
            return self._conn

    def execute(self, query: Any, params: Any = None) -> Any:
        return self.current().execute(query, params)

    def transaction(self) -> AbstractContextManager[Any]:
        return cast(AbstractContextManager[Any], self.current().transaction())

    @property
    def closed(self) -> bool:
        return bool(self._conn.closed)

    @property
    def info(self) -> Any:
        return self.current().info

    def close(self) -> None:
        self._conn.close()


class Rows:
    """A statement's results, read in full before its connection went back to the pool.

    The stores use only ``fetchone``, ``fetchall`` and ``rowcount``.
    """

    def __init__(self, cursor: Any) -> None:
        self.rowcount: int = cursor.rowcount
        self._rows = list(cursor.fetchall()) if cursor.description is not None else []
        self._next = 0

    def fetchone(self) -> Any:
        if self._next >= len(self._rows):
            return None
        self._next += 1
        return self._rows[self._next - 1]

    def fetchall(self) -> list[Any]:
        rest, self._next = self._rows[self._next :], len(self._rows)
        return rest

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())


class PooledConnection:
    """The ``ReconnectingConnection`` interface over a pool (S2.4, TODOS: connection pool).

    One connection per process serialised every request. Here each statement
    borrows a connection for its own length, so requests that run at the same
    time (store calls run off the event loop, in threads) no longer queue on
    one socket. A transaction keeps its connection for its whole length: every
    statement inside it, in the same thread, goes to that connection.

    A connection that went away is checked and replaced by the pool before it
    is handed out, so a database restart still costs one failed call at most
    (NFR-410), as with ``ReconnectingConnection``.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._held = threading.local()

    def _in_transaction(self) -> Any:
        return getattr(self._held, "conn", None)

    def execute(self, query: Any, params: Any = None) -> Any:
        held = self._in_transaction()
        if held is not None:
            return held.execute(query, params)
        with self._pool.connection() as conn:
            return Rows(conn.execute(query, params))

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.session() as conn, conn.transaction() as tx:
            yield tx  # nested: a savepoint on the same connection

    @contextmanager
    def session(self) -> Iterator[Any]:
        """One connection for every statement in this thread until the block ends.

        For work that relies on session state: a session advisory lock taken
        on one pooled connection and released on another would never be
        released, and the next caller would wait for it forever.
        """
        held = self._in_transaction()
        if held is not None:
            yield held
            return
        with self._pool.connection() as conn:
            self._held.conn = conn
            try:
                yield conn
            finally:
                self._held.conn = None

    @property
    def closed(self) -> bool:
        return bool(self._pool.closed)

    @property
    def info(self) -> Any:
        with self._pool.connection() as conn:
            return conn.info

    def close(self) -> None:
        self._pool.close()


@contextmanager
def pinned(conn: Any) -> Iterator[None]:
    """Keep every statement in the block on one connection, pooled or not."""
    session = getattr(conn, "session", None)
    if session is None:
        yield  # a single connection is pinned already
        return
    with session():
        yield


@contextmanager
def advisory_lock(conn: Any, name: str) -> Iterator[None]:
    """Hold a PostgreSQL advisory lock for the length of one transaction.

    Processes that start together (API, worker, tools) queue here instead of
    racing each other through the same read-then-write.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (name,))
        yield
