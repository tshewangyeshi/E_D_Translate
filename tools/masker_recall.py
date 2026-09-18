"""Masker recall over a hand-labelled set (S1.3; reused for the Sprint 0 gate, S0.1).

A labelled entity counts as protected when one masked span fully covers it,
because what matters for FR-140 is that none of its characters reach the model.
Kind mismatches are reported but do not reduce recall.

    python tools/masker_recall.py tests/fixtures/masking/labelled-synthetic.json --split heldout
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from orchestrator.pipeline.protect import find_entities


@dataclass(frozen=True)
class Report:
    total: int
    protected: int
    misses: list[str]
    kind_mismatches: list[str]

    @property
    def recall(self) -> float:
        return 1.0 if self.total == 0 else self.protected / self.total


def measure(items: list[dict[str, object]], split: str | None = None) -> Report:
    total = protected = 0
    misses: list[str] = []
    kinds: list[str] = []
    for item in items:
        if split and item.get("split") != split:
            continue
        text = str(item["text"])
        spans = find_entities(text)
        cursor = 0
        for ent in item["entities"]:  # type: ignore[attr-defined]
            value, kind = str(ent["value"]), str(ent["kind"])
            start = text.find(value, cursor)
            if start == -1:
                raise ValueError(f"label {value!r} not found in {text!r}")
            end = start + len(value)
            cursor = end
            total += 1
            cover = next(((a, b, k) for a, b, k in spans if a <= start and end <= b), None)
            if cover is None:
                misses.append(f"{value!r} ({kind}) in {text!r}")
                continue
            protected += 1
            if cover[2] != kind:
                kinds.append(f"{value!r}: labelled {kind}, masked as {cover[2]}")
    return Report(total, protected, misses, kinds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labelled", type=Path)
    parser.add_argument("--split", choices=["tune", "heldout"], default=None)
    args = parser.parse_args(argv)
    items = json.loads(args.labelled.read_text(encoding="utf-8"))["items"]
    report = measure(items, args.split)
    for miss in report.misses:
        print(f"MISS  {miss}")
    for note in report.kind_mismatches:
        print(f"KIND  {note}")
    print(f"recall {report.protected}/{report.total} = {report.recall:.2%}")
    return 0 if report.recall == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
