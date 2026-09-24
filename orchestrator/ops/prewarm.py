"""Offline pre-warm of enrolled pilot pages (S2.4, ER-O7). Requirements: FR-155, FR-156, FR-510.

    adapters/widget: npm run build && node scripts/export-segments.mjs pages/*.html > segments.json
    python -m orchestrator.ops.prewarm --site portal segments.json
    python -m orchestrator.queue.run_worker        # drains the queue within its reserved quota

Segments come from PUBLIC snapshots reviewed by the team, so the
N-distinct-clients rule (NFR-304) does not apply here. Tier 1 segments are never
machine-translated (FR-510): they are counted and listed for the offline human
review (S7.0) instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.governance.sites import Site
from orchestrator.pipeline.segment import SegmentError
from orchestrator.queue.jobs import PRIORITY_PREWARM
from orchestrator.service.translate import SegmentIn, TranslateService
from orchestrator.store.lookup import LookupItem


@dataclass
class PrewarmReport:
    queued: int = 0
    already_translated: int = 0
    queue_full: int = 0
    invalid: int = 0
    tier1_needs_review: list[str] = field(default_factory=list)  # segment keys, for S7.0

    def summary(self) -> str:
        return (
            f"queued {self.queued}, already translated {self.already_translated}, "
            f"Tier 1 needing human review {len(self.tier1_needs_review)}, "
            f"invalid {self.invalid}, queue full {self.queue_full}"
        )


def prewarm(service: TranslateService, site: Site, segments: list[dict[str, Any]]) -> PrewarmReport:
    """Queue what may be machine-translated. Each segment carries the page ``path``.

    The path decides the tier (FR-512), so a segment without one is Tier 1 on a
    site that has path rules: it lands in ``tier1_needs_review`` rather than being
    translated. That is the safe direction, but it means an export without paths
    pre-warms nothing -- which the report makes visible.
    """
    report = PrewarmReport()
    prepared = []
    for n, raw in enumerate(segments):
        seg_in = SegmentIn(f"p{n}", str(raw["text"]), raw.get("tier"), raw.get("selector_tier"))
        try:
            prepared.append(service.prepare(site, seg_in, raw.get("path")))
        except SegmentError:
            report.invalid += 1
    unique = {p.keys.machine_key: p for p in prepared}.values()  # a page repeats headers/footers
    hits = service.store.lookup([LookupItem(p.keys, p.tier) for p in unique])
    for p, hit in zip(unique, hits, strict=True):
        if p.tier == 1:
            if hit is None:
                report.tier1_needs_review.append(p.keys.segment_key)
            else:
                report.already_translated += 1
        elif hit is not None:
            report.already_translated += 1
        elif service.queue.enqueue(service.job_for(site, p, PRIORITY_PREWARM)):
            report.queued += 1
        else:
            report.queue_full += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("segments", type=Path, help="output of scripts/export-segments.mjs")
    parser.add_argument("--site", required=True, help="enrolled site id")
    args = parser.parse_args(argv)

    from orchestrator.wiring import Settings, build

    components = build(Settings.from_env())
    site = components.sites.get(args.site)
    if site is None:
        print(f"site {args.site!r} is not enrolled or is disabled", file=sys.stderr)
        return 2
    data = json.loads(args.segments.read_text(encoding="utf-8"))
    segments = [
        {**s, "path": page.get("path")} for page in data["pages"] for s in page["segments"]
    ]
    if site.path_rules and any(s["path"] is None for s in segments):
        print(
            f"site {site.site_id!r} has path rules, but the export has pages without a path: "
            "re-export with 'page.html=/the/site/path' so each page is tiered correctly",
            file=sys.stderr,
        )
        return 2
    report = prewarm(components.service, site, segments)
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
