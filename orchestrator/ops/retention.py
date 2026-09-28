"""Expire unapproved machine translations (S3.4, NFR-305).

    python -m orchestrator.ops.retention --days 90
    python -m orchestrator.ops.retention --days 90 --dry-run

Machine output that nobody approved is working data, not a record. Keeping it
indefinitely means a growing store of text scraped from government pages with
no one accountable for it, so it is invalidated once it is older than the
retention period. Approved translations are never touched: a human signed them
off and they are the service's memory.

Invalidation rather than deletion, deliberately. `translation_version` rows are
immutable by database trigger (S1.7) precisely so that what was served can be
explained afterwards; marking a row invalid stops it being served while keeping
the audit trail intact.

Residual: a row expired here can still be served from the Redis hot cache until
its own TTL runs out, which is 7 days. The rows hold masked text only (FR-143),
so this is a delay in forgetting rather than an exposure, but the window is
real and worth knowing when reporting a retention period to anyone.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

DEFAULT_DAYS = 90


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"expire unapproved machine translations older than this (default {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many rows would be expired, and change nothing",
    )
    args = parser.parse_args(argv)

    if args.days < 1:
        print("--days must be at least 1", file=sys.stderr)
        return 2

    from orchestrator.wiring import Settings, build

    components = build(Settings.from_env())
    cutoff = datetime.now(UTC) - timedelta(days=args.days)

    if args.dry_run:
        # Counting without changing anything: the operator sees the blast
        # radius before a retention run they cannot undo.
        count = components.store.count_machine_before(cutoff)
        print(
            f"would expire {count} unapproved machine translations created before {cutoff:%Y-%m-%d}"
        )
        return 0

    expired = components.store.expire_machine(cutoff)
    print(f"expired {expired} unapproved machine translations created before {cutoff:%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
