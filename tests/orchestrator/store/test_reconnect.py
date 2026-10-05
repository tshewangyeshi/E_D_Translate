"""A lost PostgreSQL connection is reopened (NFR-410).

Found by the 2026-09-29 red-team review: psycopg never reopens a closed
connection, so a database restart left every process answering in English
until someone restarted it. The integration case, with a real server ending
the connection, is in tests/orchestrator/test_wiring_governance.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.store.pg import ReconnectingConnection


class _Conn:
    def __init__(self, n: int) -> None:
        self.n = n
        self.closed = False
        self.statements: list[str] = []

    def execute(self, query: str, params: Any = None) -> _Conn:
        if self.closed:
            raise ConnectionError("the connection is closed")
        self.statements.append(query)
        return self


def test_nfr410_a_closed_connection_is_replaced_on_the_next_call() -> None:
    opened: list[_Conn] = []

    def connect() -> _Conn:
        opened.append(_Conn(len(opened)))
        return opened[-1]

    conn = ReconnectingConnection(connect)
    conn.execute("SELECT 1")
    opened[0].closed = True  # the server went away

    conn.execute("SELECT 2")
    assert [c.statements for c in opened] == [["SELECT 1"], ["SELECT 2"]]
    assert conn.reconnects == 1


def test_nfr410_a_statement_is_never_sent_twice() -> None:
    """A write that may already have committed must not be repeated."""
    first = _Conn(0)

    def fail_mid_statement(query: str, params: Any = None) -> _Conn:
        first.closed = True
        raise ConnectionError("server closed the connection unexpectedly")

    first.execute = fail_mid_statement  # type: ignore[method-assign]
    second = _Conn(1)
    conns = iter([first, second])
    conn = ReconnectingConnection(lambda: next(conns))

    with pytest.raises(ConnectionError):
        conn.execute("INSERT INTO x VALUES (1)")
    assert second.statements == []  # not retried
    conn.execute("SELECT 1")
    assert second.statements == ["SELECT 1"]


def test_nfr410_while_the_server_is_away_each_call_fails_cleanly() -> None:
    attempts: list[int] = []
    live = _Conn(0)

    def connect() -> _Conn:
        attempts.append(1)
        if len(attempts) == 1:
            return live
        raise ConnectionError("connection refused")

    conn = ReconnectingConnection(connect)
    live.closed = True
    for _ in range(3):
        with pytest.raises(ConnectionError):
            conn.execute("SELECT 1")
    assert len(attempts) == 4  # one try per call, no loop
