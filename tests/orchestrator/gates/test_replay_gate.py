"""Replay GATE on real model output (NFR-200, NFR-201, S10.2). If this fails, nothing ships.

The adversarial mock proves the checks; it cannot prove the real model. These
are GovTech staging's own answers (tests/fixtures/mt-replay, recorded once by
tools/s0_placeholder_survival.py), replayed through today's decoder and
validation with no network call:

  * NFR-201: everything that would be served has every entity and glossary
    placeholder exactly once, and no number, email or address of the model's
    own -- checked independently of the validator that decided to serve it;
  * NFR-200: tag integrity of at least 99% on the adopted pipeline;
  * no regression: today's code serves at least as much as was usable when
    the answers were recorded.

The nightly run against the live endpoint on the frozen evaluation set is
tools/nightly_gate.py (S10.2, S10.1).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from orchestrator.pipeline.glossary import GlossaryCheckError
from orchestrator.pipeline.protect import EntityCheckError
from orchestrator.pipeline.segment import MODEL_FORMATS, Entity, Segment, SegmentError, Text, parse
from orchestrator.pipeline.tags import tag_structure
from orchestrator.pipeline.validate import validate_output

REPLAY = Path(__file__).resolve().parents[2] / "fixtures" / "mt-replay"
ADOPTED = "live"  # xml, lenient, pieces, placeholder-only text not sent (S0.1)

_LEAK = re.compile(r"[\d༠-༳]|@|https?://|www\.", re.IGNORECASE)


def _recordings() -> list[dict[str, Any]]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(REPLAY.glob("*.json"))]


def _replay(rec: dict[str, Any]) -> tuple[str, Segment | None]:
    """'served', 'entity', 'term' or 'tag', and what would be served."""
    source = parse(rec["source_wire"], allow_entities=True)
    try:
        decoded = MODEL_FORMATS["xml"].decode(rec["raw_output"], source)
        validate_output(decoded, source)
    except EntityCheckError:
        return "entity", None
    except GlossaryCheckError:
        return "term", None
    except SegmentError:
        return "tag", None
    return "served", decoded


def _intact(served: Segment, source: Segment) -> bool:
    """Independent of the validator: placeholders as in the source, nothing invented."""

    def placeholders(seg: Segment) -> Counter[tuple[str, int]]:
        return Counter((t.kind, t.id) for t in seg.tokens if isinstance(t, Entity))

    own_text = "".join(t.value for t in served.tokens if isinstance(t, Text))
    source_text = "".join(t.value for t in source.tokens if isinstance(t, Text))
    invented = {m.group(0) for m in _LEAK.finditer(own_text)} - {
        m.group(0) for m in _LEAK.finditer(source_text)
    }
    return (
        placeholders(served) == placeholders(source)
        and tag_structure(served) == tag_structure(source)
        and not invented
    )


@pytest.fixture(scope="module")
def adopted() -> list[dict[str, Any]]:
    recs = [r for r in _recordings() if r["format"] == ADOPTED]
    assert len(recs) >= 20, "too few recordings of the adopted pipeline to gate on"
    return recs


def test_nfr201_real_model_output_that_is_served_has_every_entity_intact(
    adopted: list[dict[str, Any]],
) -> None:
    served = 0
    for rec in adopted:
        outcome, output = _replay(rec)
        if outcome == "served":
            served += 1
            assert output is not None
            assert _intact(output, parse(rec["source_wire"], allow_entities=True)), rec[
                "source_wire"
            ]
    assert served > 0


def test_nfr200_real_model_output_keeps_its_tags(adopted: list[dict[str, Any]]) -> None:
    tag_failures = sum(1 for rec in adopted if _replay(rec)[0] == "tag")
    integrity = 1 - tag_failures / len(adopted)
    assert integrity >= 0.99, f"tag integrity {integrity:.1%} on {len(adopted)} real answers"


def test_s102_todays_code_serves_at_least_what_was_usable_when_recorded(
    adopted: list[dict[str, Any]],
) -> None:
    usable_then = sum(1 for rec in adopted if rec["ok"])
    served_now = sum(1 for rec in adopted if _replay(rec)[0] == "served")
    assert served_now >= usable_then, f"served {served_now}, recorded usable {usable_then}"
