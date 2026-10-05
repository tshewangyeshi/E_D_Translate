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


@contextmanager
def advisory_lock(conn: Any, name: str) -> Iterator[None]:
    """Hold a PostgreSQL advisory lock for the length of one transaction.

    Processes that start together (API, worker, tools) queue here instead of
    racing each other through the same read-then-write.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (name,))
        yield
