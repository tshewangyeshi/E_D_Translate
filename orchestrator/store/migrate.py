"""Apply SQL migrations in order, once each. ``python -m orchestrator.store.migrate <dsn>``.

The API, the worker and the operator tools all migrate at start, and in a
deploy they start together. Without a lock two of them read the same list of
applied files, both apply the next one, and the loser fails to start. So the
whole run holds one advisory lock: the second process waits, then finds
nothing left to do.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

MIGRATIONS = Path(__file__).with_name("migrations")

#: Also taken by anything else that must not interleave with a migration.
LOCK_NAME = "dzweb.schema_migration"


def migrate(conn: Any) -> list[str]:
    """Apply pending migrations on a psycopg connection. Returns names applied."""
    from orchestrator.store.pg import pinned

    with pinned(conn):  # the session lock below must be released where it was taken
        return _migrate(conn)


def _migrate(conn: Any) -> list[str]:
    applied: list[str] = []
    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (LOCK_NAME,))
    try:
        with conn.transaction():
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migration ("
                " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        # Read only once the lock is held: another process may just have applied some.
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migration").fetchall()}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))
    return applied


def pending(conn: Any) -> list[str]:
    """Migrations not yet applied, without applying any. For read-only tools."""
    exists = conn.execute("SELECT to_regclass('schema_migration')").fetchone()[0]
    done: set[str] = set()
    if exists is not None:
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migration").fetchall()}
    return [p.name for p in sorted(MIGRATIONS.glob("*.sql")) if p.name not in done]


def main(argv: list[str]) -> int:
    import psycopg

    if len(argv) != 2:
        print("usage: python -m orchestrator.store.migrate <postgres-dsn>")
        return 2
    with psycopg.connect(argv[1], autocommit=True) as conn:
        for name in migrate(conn):
            print(f"applied {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
