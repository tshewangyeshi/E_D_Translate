"""S1.3 — entity masking and restoration. Requirements: FR-140, FR-141, FR-142, FR-143."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from orchestrator.locale.dz import to_tibetan_digits
from orchestrator.pipeline.protect import (
    EntityCheckError,
    MaskedEntity,
    leak_reason,
    mask,
    restore,
)
from orchestrator.pipeline.segment import Entity, Segment, SegmentError, Text, parse, tokenize
from tools.masker_recall import measure

LABELLED = Path(__file__).resolve().parents[1] / "fixtures" / "masking" / "labelled-synthetic.json"


def _masked(wire: str) -> tuple[Segment, dict[int, MaskedEntity]]:
    return mask(parse(wire))


# --- FR-140: masking ---


@pytest.mark.parametrize(
    ("text", "masked", "values"),
    [
        ("fee of 1500 per year", "fee of ⟦NUM:1⟧ per year", ["1500"]),
        ("Apply by 30 June 2026.", "Apply by ⟦DATE:1⟧.", ["30 June 2026"]),
        ("call 90000001", "call ⟦NUM:1⟧", ["90000001"]),
        ("Pay Nu. 1,500.", "Pay ⟦CUR:1⟧.", ["Nu. 1,500"]),
        ("BTN 500, payable", "⟦CUR:1⟧, payable", ["BTN 500"]),
        ("CID 00012345678 required", "CID ⟦CID:1⟧ required", ["00012345678"]),
        ("Ref MoXX/DEMO/2026/123", "Ref ⟦REF:1⟧", ["MoXX/DEMO/2026/123"]),
        ("see https://www.gov.example/apply.", "see ⟦URL:1⟧.", ["https://www.gov.example/apply"]),
        ("mail help@portal.gov.example.", "mail ⟦EMAIL:1⟧.", ["help@portal.gov.example"]),
        ("up 12.5% in 2025", "up ⟦PCT:1⟧ in ⟦NUM:2⟧", ["12.5%", "2025"]),
    ],
)
def test_fr140_masks_every_entity_kind(text: str, masked: str, values: list[str]) -> None:
    seg, entities = _masked(text)
    assert seg.to_wire() == masked
    assert [e.value for e in entities.values()] == values


@pytest.mark.parametrize("regression", ["1500", "2026", "90000001"])
def test_fr140_regression_four_plus_digit_numbers_are_masked(regression: str) -> None:
    seg, entities = _masked(f"value {regression} here")
    assert [e.value for e in entities.values()] == [regression]
    assert not any(ch.isdigit() for ch in seg.plain_text())


def test_fr140_currency_is_masked_before_bare_numbers() -> None:
    _, entities = _masked("Pay Nu. 1,500 now")
    assert [(e.kind, e.value) for e in entities.values()] == [("CUR", "Nu. 1,500")]


def test_fr140_entity_ids_continue_after_tag_ids() -> None:
    seg, entities = _masked("Pay ⟦1⟧Nu. 500⟦/1⟧ by ⟦v2/⟧ 2026-06-30")
    assert seg.to_wire() == "Pay ⟦1⟧⟦CUR:3⟧⟦/1⟧ by ⟦v2/⟧ ⟦DATE:4⟧"
    assert set(entities) == {3, 4}


def test_fr140_value_split_by_markup_is_still_protected() -> None:
    seg, _ = _masked("Pay Nu. ⟦1⟧500⟦/1⟧ today")
    assert not any(ch.isdigit() for ch in seg.plain_text())


def test_fr140_already_masked_segment_is_rejected() -> None:
    with pytest.raises(SegmentError):
        mask(parse("x ⟦NUM:1⟧", allow_entities=True))


# --- FR-140/141: restoration ---


def test_fr140_restore_is_byte_identical_and_uses_this_requests_map() -> None:
    seg_a, map_a = _masked("Pay Nu. 500 today")
    seg_b, map_b = _masked("Pay Nu. 600 today")
    assert seg_a == seg_b  # one cache entry serves both requests (FR-143)
    model_out = parse("Today pay ⟦CUR:1⟧", allow_entities=True)
    assert restore(model_out, seg_a, map_a).plain_text() == "Today pay Nu. 500"
    assert restore(model_out, seg_b, map_b).plain_text() == "Today pay Nu. 600"


def test_fr140_restore_keeps_whitespace_variants_byte_identical() -> None:
    seg, entities = _masked("Pay Nu.  500 today")  # double space inside the amount
    out = restore(seg, seg, entities)
    assert out.plain_text() == "Pay Nu.  500 today"


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ("Pay ⟦CUR:1⟧ ⟦CUR:1⟧ by ⟦DATE:2⟧", "duplicate"),
        ("Pay ⟦CUR:1⟧ today", "missing"),
        ("Pay ⟦CUR:1⟧ by ⟦DATE:2⟧ ⟦NUM:9⟧", "unknown"),
        ("Pay ⟦DATE:1⟧ by ⟦CUR:2⟧", "kind_mismatch"),
        ("Pay ⟦CUR:1⟧ by ⟦DATE:2⟧ 42", "leak"),
    ],
)
def test_fr141_bad_entity_output_is_refused(output: str, reason: str) -> None:
    seg, entities = _masked("Pay Nu. 500 by 2026-06-30")
    decoded = Segment(tokenize(output, allow_entities=True))  # as a decoder would: unvalidated
    with pytest.raises(EntityCheckError) as err:
        restore(decoded, seg, entities)
    assert err.value.reason == reason
    assert err.value.cause == "entity_check_failed"


@pytest.mark.parametrize(
    "output",
    ["Pay ⟦NUM:1⟧⟦NUM:2⟧ now", "Pay ⟦3⟧⟦NUM:1⟧⟦/3⟧⟦NUM:2⟧ now"],
)
def test_fr140_regression_entities_newly_touching_are_refused(output: str) -> None:
    # 1500 and 90000001 rendered with nothing between them read as 150090000001.
    seg, entities = _masked("Pay 1500 and 90000001 now")
    with pytest.raises(EntityCheckError) as err:
        restore(Segment(tokenize(output, allow_entities=True)), seg, entities)
    assert err.value.reason == "adjacent"


def test_fr140_entities_separated_by_a_void_element_are_allowed() -> None:
    seg, entities = _masked("Pay 1500 and 90000001 now")
    decoded = Segment(tokenize("Pay ⟦NUM:1⟧⟦v3/⟧⟦NUM:2⟧ now", allow_entities=True))
    out = restore(decoded, seg, entities)
    # plain_text() joins runs; on screen the <br> keeps the two numbers apart
    assert out.plain_text() == "Pay 150090000001 now"


# --- FR-142: leak scan ---


@pytest.mark.parametrize(
    "text",
    [
        "pay 42",
        to_tibetan_digits("pay 42"),
        "pay ½",
        "pay ٤٢",
        "mail a@b.example",
        "go to www.x.bt",
        "http://x",
    ],
)
def test_fr142_leak_scan_rejects_numerals_emails_and_urls(text: str) -> None:
    assert leak_reason(text) is not None


def test_fr142_leak_scan_accepts_plain_words() -> None:
    assert leak_reason("Pay the fee at the counter.") is None


def test_fr142_model_converting_an_entity_to_tibetan_digits_is_refused() -> None:
    seg, entities = _masked("Pay Nu. 500 today")
    out = Segment((Text("Pay " + to_tibetan_digits("500") + " today"),))
    with pytest.raises(EntityCheckError):
        restore(out, seg, entities)


# --- properties ---

_pool = st.sampled_from(
    ["Nu. 500", "Nu. 1,500", "BTN 2500.50", "30 June 2026", "2026-06-30", "00012345678",
     "MoXX/DEMO/2026/123", "90000001", "12.5%", "help@portal.gov.example", "1500", "7"]
)
_words = st.sampled_from(
    ["Pay", "the", "fee", "by", "at", "counter", "apply", "before", "online", "and"]
)


@given(st.lists(st.one_of(_pool, _words), min_size=1, max_size=12))
def test_fr140_mask_then_identity_restore_round_trips_exactly(parts: list[str]) -> None:
    text = " ".join(parts)
    seg, entities = mask(Segment((Text(text),)))
    assert not any(ch.isdigit() for ch in seg.plain_text())
    assert restore(seg, seg, entities).plain_text() == text


@given(st.text(alphabet=st.characters(whitelist_categories=("Nd",)), min_size=1, max_size=20))
def test_fr140_any_digit_run_in_any_script_is_masked(digits: str) -> None:
    seg, _ = mask(Segment((Text(f"code {digits} here"),)))
    assert not any(ch.isdigit() for ch in seg.plain_text())


# --- recall (FR-140, S1.3) ---


@pytest.mark.parametrize("split", ["tune", "heldout"])
def test_fr140_masker_recall_is_100_percent_on_labelled_set(split: str) -> None:
    items = json.loads(LABELLED.read_text(encoding="utf-8"))["items"]
    report = measure(items, split)
    assert report.total > 0
    assert report.misses == []
    assert report.recall == 1.0


def test_fr143_entity_map_is_per_request_and_not_in_masked_text() -> None:
    seg, entities = _masked("CID 00012345678 and Nu. 500")
    wire = seg.to_wire()
    for ent in entities.values():
        assert ent.value not in wire
    assert all(isinstance(m, Entity) for m in seg.structure())
