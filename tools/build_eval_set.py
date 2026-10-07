"""Build the frozen evaluation set from the pilot snapshot (S10.1, NFR-202).

    python tools/g2c_demo/scrape.py            # the snapshot, if not already there
    python tools/build_eval_set.py             # -> eval/frozen/segments.jsonl

Real government text, so it never enters this public repository: eval/frozen/
is git-ignored, and belongs in a private repository or protected path once
the pilot has one. Built once and frozen: rebuilding replaces it, which is a
decision to record (eval/README.md), not something a tuning run may do.

Each segment carries a *proposed* tier for a person to confirm:

  1  fees, penalties, eligibility, legal and deadline statements -- text a
     citizen acts on, which is never machine translated in production (FR-510)
     but is still scored, because Tier 1 seeding is measured against it;
  2  service descriptions, procedures, documents required;
  3  navigation, headings and short labels.
"""

from __future__ import annotations

import hashlib
import html
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tools" / "g2c_demo" / "data"
OUT = ROOT / "eval" / "frozen" / "segments.jsonl"
SIZE = 800
SEED = 20261006

_BLOCK_END = re.compile(r"<\s*/?\s*(p|li|td|th|tr|div|h[1-6]|br|ul|ol|table)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_TIER1 = re.compile(
    r"\b(fee|fees|nu\.|penalt|fine|eligib|must|shall|deadline|within \d+|days?\b|"
    r"liable|law|act\b|rule|regulation|refund|charge)",
    re.I,
)


def segments_of(document: str) -> list[str]:
    text = html.unescape(_TAG.sub("", _BLOCK_END.sub("\n", document or "")))
    out = []
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if 3 <= len(line) <= 400 and re.search(r"[A-Za-z]{3}", line):
            out.append(line)
    return out


def proposed_tier(text: str) -> int:
    if _TIER1.search(text):
        return 1
    if len(text.split()) <= 4:
        return 3
    return 2


def build() -> list[dict[str, object]]:
    services = json.loads((SNAPSHOT / "services.json").read_text(encoding="utf-8"))
    categories = json.loads((SNAPSHOT / "categories.json").read_text(encoding="utf-8"))
    pool: dict[str, dict[str, object]] = {}
    for c in categories:
        for text in (c["categoryName"], c.get("categoryDescription") or ""):
            if text.strip():
                pool.setdefault(text.strip(), {"source": f"category:{c['id']}"})
    for s in services:
        for text in (s["serviceName"], s.get("serviceDescription") or ""):
            if text.strip():
                pool.setdefault(text.strip(), {"source": f"service:{s['id']}"})
        for text in segments_of(s.get("serviceDocument") or ""):
            pool.setdefault(text, {"source": f"service:{s['id']}"})

    texts = sorted(pool)
    by_tier: dict[int, list[str]] = {1: [], 2: [], 3: []}
    for text in texts:
        by_tier[proposed_tier(text)].append(text)
    rng = random.Random(SEED)  # noqa: S311 - a fixed sample, not security
    # All three tiers, in proportion, but never fewer than 50 of a tier that has them.
    chosen: list[str] = []
    for group in by_tier.values():
        share = max(min(50, len(group)), round(SIZE * len(group) / max(1, len(texts))))
        chosen += rng.sample(group, min(share, len(group)))
    chosen = sorted(set(chosen))[: max(SIZE, 500)]
    return [
        {
            "id": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "text": text,
            "proposed_tier": proposed_tier(text),
            "source": pool[text]["source"],
            "reference": None,  # a human Dzongkha translation, for chrF++ (NFR-202)
        }
        for text in chosen
    ]


def main() -> int:
    if not (SNAPSHOT / "services.json").exists():
        print("no snapshot: run python tools/g2c_demo/scrape.py first", file=sys.stderr)
        return 2
    items = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as out:
        for item in items:
            out.write(json.dumps(item, ensure_ascii=False) + "\n")
    tiers = {t: sum(1 for i in items if i["proposed_tier"] == t) for t in (1, 2, 3)}
    print(f"{len(items)} segments -> {OUT.relative_to(ROOT)}; proposed tiers {tiers}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
