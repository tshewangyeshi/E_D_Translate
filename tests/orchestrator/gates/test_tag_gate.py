"""Tag-integrity GATE (NFR-200, FR-122, FR-123, FR-210). If this fails, nothing ships.

10,000 seeded runs: sentences with inline spans (some nested), void elements
and entities are masked, sent through the adversarial mock in every mode and
both token formats, decoded and restored. Every run must end in exactly one of:

  * refusal, so the source text is served (NFR-410);
  * for the widget, a segment whose tag markers equal the source's: same
    markers, same order, balanced, so every text node can be written back in
    place (FR-122, FR-210);
  * for markup callers (proxy, CMS, ``/v1/translate/html``), when the tags did
    not survive, formatting collapse (FR-123): balanced and properly nested,
    every source span exactly once, every void kept, entities intact.

Malformed markup is never emitted, under any mode.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from orchestrator.pipeline.protect import mask, restore
from orchestrator.pipeline.segment import (
    MODEL_FORMATS,
    Open,
    Segment,
    SegmentError,
    Text,
    Void,
    parse,
    validate,
)
from orchestrator.pipeline.tags import check_tags, collapse_formatting, tag_structure
from orchestrator.testing.mock_nmt import MockNMT, Mode, UpstreamError

RUNS = 10_000

ENTITIES = ["Nu. 500", "30 June 2026", "00012345678", "applications@portal.gov.example", "12.5%"]
WORDS = ["Pay", "the", "fee", "by", "at", "counter", "apply", "before", "online", "and", "office"]


def _sentence(rng: random.Random) -> str:
    parts: list[str] = []
    next_id = 1
    for _ in range(rng.randint(3, 9)):
        roll = rng.random()
        if roll < 0.25:
            parts.append(f"⟦{next_id}⟧{rng.choice(WORDS)} {rng.choice(WORDS)}⟦/{next_id}⟧")
            next_id += 1
        elif roll < 0.35:  # nested spans: a link around bold text
            outer, inner = next_id, next_id + 1
            parts.append(
                f"⟦{outer}⟧{rng.choice(WORDS)} ⟦{inner}⟧{rng.choice(WORDS)}⟦/{inner}⟧⟦/{outer}⟧"
            )
            next_id += 2
        elif roll < 0.45:
            parts.append(f"⟦v{next_id}/⟧")
            next_id += 1
        elif roll < 0.6:
            parts.append(rng.choice(ENTITIES))
        else:
            parts.append(rng.choice(WORDS))
    return " ".join(parts)


def _text(segment: Segment) -> str:
    return "".join(t.value for t in segment.tokens if isinstance(t, Text))


def _well_formed(segment: Segment) -> None:
    """What any renderer needs: balanced, nested, ids once, and it survives the wire."""
    validate(segment.tokens)
    assert parse(segment.to_wire()).structure() == segment.structure()


@pytest.mark.parametrize("fmt_name", sorted(MODEL_FORMATS))
def test_nfr200_malformed_markup_is_never_emitted_under_adversarial_model(fmt_name: str) -> None:
    fmt = MODEL_FORMATS[fmt_name]
    rng = random.Random(20261005)  # noqa: S311 - deterministic test data
    mock = MockNMT(fmt, seed=rng.randrange(2**32), modes={m: 1.0 for m in Mode})
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

        try:
            check_tags(restored, masked)
        except SegmentError:
            outcomes[f"tag_failure:{mock.last_mode}"] += 1
            collapsed = collapse_formatting(restored, masked)
            _well_formed(collapsed)
            spans = [m.id for m in tag_structure(masked) if isinstance(m, Open)]
            voids = [m for m in tag_structure(masked) if isinstance(m, Void)]
            assert [m.id for m in tag_structure(collapsed) if isinstance(m, Open)] == spans
            assert [m for m in tag_structure(collapsed) if isinstance(m, Void)] == voids
            assert _text(collapsed) == _text(restored)  # no text lost or added
            continue

        outcomes[f"widget:{mock.last_mode}"] += 1
        _well_formed(restored)
        assert tag_structure(restored) == tag_structure(masked), (mock.last_mode, raw)

    assert outcomes["refused"] > 0
    assert outcomes[f"widget:{Mode.WELL_BEHAVED}"] > 0
    assert any(k.startswith("tag_failure:") for k in outcomes), "collapse never exercised"


def test_fr123_collapse_applies_the_formatting_to_the_whole() -> None:
    source = parse("Click ⟦1⟧here⟦/1⟧ to ⟦2⟧apply⟦/2⟧ now.⟦v3/⟧")
    model = parse("DZ apply here click now")  # the model lost every tag
    collapsed = collapse_formatting(model, source)
    assert collapsed.to_wire() == "⟦1⟧⟦2⟧DZ apply here click now⟦/2⟧⟦/1⟧⟦v3/⟧"


def test_fr123_collapse_keeps_entities_and_drops_the_models_own_tags() -> None:
    source, _ = mask(parse("Pay ⟦1⟧Nu. 500⟦/1⟧ today."))
    assert source.to_wire() == "Pay ⟦1⟧⟦CUR:2⟧⟦/1⟧ today."
    model = parse("⟦1⟧DZ⟦/1⟧ ⟦CUR:2⟧ DZ", allow_entities=True)  # entity moved out of its span
    collapsed = collapse_formatting(model, source)
    assert collapsed.to_wire() == "⟦1⟧DZ ⟦CUR:2⟧ DZ⟦/1⟧"


def test_fr123_a_segment_without_formatting_collapses_to_its_text() -> None:
    assert collapse_formatting(parse("DZ text"), parse("Plain text")).to_wire() == "DZ text"
