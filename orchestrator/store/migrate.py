"""Apply SQL migrations in order, once each. ``python -m orchestrator.store.migrate <dsn>``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

MIGRATIONS = Path(__file__).with_name("migrations")


def migrate(conn: Any) -> list[str]:
    """Apply pending migrations on a psycopg connection. Returns names applied."""
    applied: list[str] = []
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migration").fetchall()}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    return applied


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
