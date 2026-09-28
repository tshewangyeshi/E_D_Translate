"""Derived pipeline version (FR-154, ER-13). Guarded directory (docs/CLAUDE.md).

Never bumped by hand. The version is a hash of:

  * the masking pattern data (kinds and regex sources),
  * the segment grammar and split terminators,
  * the masked and split output of a golden corpus.

So any change that alters masking or segmentation output changes the version,
and a refactor that changes nothing observable leaves it alone.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path

from orchestrator.locale.dz import SPLIT_TERMINATORS
from orchestrator.pipeline.protect import PATTERNS, mask
from orchestrator.pipeline.segment import CLOSE, OPEN, parse, split_long

GOLDEN = Path(__file__).with_name("golden_corpus.json")
SPLIT_PROBE_LEN = 40  # small enough that the corpus exercises splitting


def fingerprint_inputs(corpus_path: Path = GOLDEN) -> list[str]:
    parts = [f"pattern:{kind}:{rx.pattern}" for kind, rx in PATTERNS]
    parts.append(f"delimiters:{OPEN}{CLOSE}")
    parts.append("terminators:" + "".join(sorted(SPLIT_TERMINATORS)))
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))["segments"]
    for wire in corpus:
        masked, entities = mask(parse(wire))
        parts.append("masked:" + masked.to_wire())
        parts.append("values:" + "|".join(f"{k}={e.kind}:{e.value}" for k, e in entities.items()))
        try:
            pieces = split_long(masked, SPLIT_PROBE_LEN, SPLIT_TERMINATORS)
            parts.append("split:" + "||".join(p.to_wire() for p in pieces))
        except ValueError as err:
            parts.append(f"split-error:{err}")
    return parts


def compute_pipeline_version(corpus_path: Path = GOLDEN) -> str:
    digest = hashlib.sha256("\n".join(fingerprint_inputs(corpus_path)).encode("utf-8")).hexdigest()
    return f"p-{digest[:16]}"


@cache
def pipeline_version() -> str:
    """Computed once per process at startup."""
    return compute_pipeline_version()
