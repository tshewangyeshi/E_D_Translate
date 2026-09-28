"""Entity-preservation GATE (NFR-201, FR-140, FR-141, FR-142). If this fails, nothing ships.

10,000 seeded runs: random sentences full of entities and inline markup are
masked, sent through the adversarial mock in every mode and both candidate
token formats, decoded and restored. Every run must end in exactly one of:

  * refusal (SegmentError / EntityCheckError / upstream error), so the source
    text is served (NFR-410), or
  * a restored segment where every source entity appears with its exact
    bytes, and the digit runs are exactly the source's digit runs: no number
    merged with a neighbour (1500 + 90000001 -> 150090000001), split,
    converted or invented. (Letters touching a number are fine: Dzongkha has
    no spaces between words.)

Nothing in between: no altered entity, no partial restore.
"""

from __future__ import annotations

import random
import re
from collections import Counter

import pytest

from orchestrator.pipeline.protect import mask, restore
from orchestrator.pipeline.segment import MODEL_FORMATS, Segment, SegmentError, Text, Void, parse
from orchestrator.testing.mock_nmt import MockNMT, Mode, UpstreamError

RUNS = 10_000

ENTITIES = [
    "Nu. 500", "Nu. 1,500", "BTN 2500.50", "Ngultrum 300", "30 June 2026", "June 30, 2026",
    "2026-06-30", "30/06/2026", "00012345678", "MoXX/DEMO/2026/123", "90000001", "12.5%",
    "applications@portal.gov.example", "https://portal.gov.example/services/renewal", "1500", "7",
]
WORDS = ["Pay", "the", "fee", "by", "at", "counter", "apply", "before", "online", "and", "office"]


def _sentence(rng: random.Random) -> str:
    parts: list[str] = []
    next_id = 1
    for _ in range(rng.randint(3, 10)):
        roll = rng.random()
        if roll < 0.4:
            parts.append(rng.choice(ENTITIES))
        elif roll < 0.55:
            parts.append(f"⟦{next_id}⟧{rng.choice(WORDS)} {rng.choice(ENTITIES)}⟦/{next_id}⟧")
            next_id += 1
        elif roll < 0.6:
            parts.append(f"⟦v{next_id}/⟧")
            next_id += 1
        else:
            parts.append(rng.choice(WORDS))
    return " ".join(parts)


DIGIT_RUN = re.compile(r"\d+")


def _entity_values(segment: Segment) -> list[str]:
    _, entities = mask(segment)
    return [e.value for e in entities.values()]


def _rendered(segment: Segment) -> str:
    """Text as a reader sees it: void elements (<br>, <img>) separate, inline tags do not."""
    out = []
    for t in segment.tokens:
        if isinstance(t, Text):
            out.append(t.value)
        elif isinstance(t, Void):
            out.append("\n")
    return "".join(out)


def _intact(served: str, source: Segment) -> bool:
    values = Counter(_entity_values(source))
    if any(served.count(v) < n for v, n in values.items()):
        return False
    return Counter(DIGIT_RUN.findall(served)) == Counter(DIGIT_RUN.findall(_rendered(source)))


@pytest.mark.parametrize("fmt_name", sorted(MODEL_FORMATS))
def test_nfr201_entities_are_never_altered_under_adversarial_model(fmt_name: str) -> None:
    fmt = MODEL_FORMATS[fmt_name]
    rng = random.Random(20260918)  # noqa: S311 - deterministic test data
    modes = {m: 1.0 for m in Mode}
    mock = MockNMT(fmt, seed=rng.randrange(2**32), modes=modes)
    outcomes: Counter[str] = Counter()
    for _ in range(RUNS // len(MODEL_FORMATS)):
        source = parse(_sentence(rng))
        masked, entities = mask(source)
        try:
            raw = mock.translate(masked)
            decoded = fmt.decode(raw, masked)
            restored = restore(decoded, masked, entities)
        except (SegmentError, UpstreamError):
            outcomes["refused"] += 1
            continue
        outcomes[f"served:{mock.last_mode}"] += 1
        # Independent check of what would be served: every entity intact, digits unchanged.
        assert _intact(_rendered(restored), source), (mock.last_mode, raw)
    assert outcomes["refused"] > 0
    assert outcomes[f"served:{Mode.WELL_BEHAVED}"] > 0
