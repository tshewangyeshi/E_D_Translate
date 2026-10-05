"""Release owed review items into the reviewers' queue (S3.1, FR-511).

    python -m orchestrator.ops.review_owed --site portal
    python -m orchestrator.ops.review_owed --site portal --limit 200

A Tier 2 page view past its site's daily cap still opens a review item, as
"owed": the citizen is served the translation and the item is on record, but
it is kept out of the reviewers' queue so that forged traffic cannot bury the
reviewers. This command moves the oldest owed items into the queue when the
reviewers have room. Without ``--limit`` it only reports how many are owed.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", required=True, help="enrolled site id")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="release at most this many, oldest first (default: release none, just count)",
    )
    args = parser.parse_args(argv)
    if args.limit < 0:
        print("--limit must not be negative", file=sys.stderr)
        return 2

    from orchestrator.wiring import Settings, build

    components = build(Settings.from_env(), apply_migrations=False)
    if components.sites.get(args.site) is None:
        print(f"site {args.site!r} is not enrolled or is disabled", file=sys.stderr)
        return 2
    tm = components.store.tm
    if args.limit:
        released = components.store.release_owed(args.site, args.limit)
        print(f"released {released} owed review items for {args.site}")
    print(f"{tm.owed(args.site)} owed, {tm.pending_review(args.site)} pending for {args.site}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
