"""S1.6 — glossary substitution. Requirements: FR-400, FR-401, FR-402, FR-153."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

import pytest

from orchestrator.pipeline.glossary import (
    ComplianceStats,
    GlossaryCheckError,
    Termbase,
    TermbaseError,
    find_terms,
    fingerprint,
    restore_terms,
    substitute,
)
from orchestrator.pipeline.protect import mask, restore
from orchestrator.pipeline.segment import MODEL_FORMATS, Segment, SegmentError, parse, tokenize
from orchestrator.testing.mock_nmt import MockNMT, Mode, UpstreamError

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "glossary" / "termbase-sample.json"
RAW: dict[str, Any] = json.loads(SAMPLE.read_text(encoding="utf-8"))
TB = Termbase.load(SAMPLE)


def _targets(ids: list[str]) -> list[str]:
    by_id = {t.term_id: t.target for t in TB.terms}
    return [by_id[i] for i in ids]


def _sub(text: str) -> tuple[Segment, dict[int, Any]]:
    masked, _ = mask(parse(text))
    return substitute(masked, TB)


# --- FR-400: termbase loading ---


def test_fr400_sample_termbase_loads_and_skips_retired_terms() -> None:
    assert TB.version == "sample-2026.09.1"
    assert {t.term_id for t in TB.terms} == {f"T-000{i}" for i in range(1, 7)}


def _broken(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(RAW)
    data["terms"][0].update(changes)
    return data


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"terms": []}, "termbase_version"),
        ({"termbase_version": "v1", "terms": "nope"}, "list"),
        (_broken(id="T-0002"), "duplicate term id"),
        (_broken(source="department"), "duplicates the source"),
        (_broken(source="Form 5"), "numeral"),
        (_broken(source=" padded "), "outer whitespace"),
        (_broken(target="ཀ\u200bཁ"), "zero-width"),
        (_broken(target="bad ⟦1⟧"), "delimiters"),
        (_broken(version=0), "positive integer"),
        (_broken(version=True), "positive integer"),
        (_broken(case_sensitive="yes"), "true or false"),
    ],
)
def test_fr400_invalid_termbase_is_rejected(data: dict[str, Any], message: str) -> None:
    with pytest.raises(TermbaseError, match=message):
        Termbase.from_dict(data)


def test_fr400_case_sensitive_entries_may_differ_only_in_case() -> None:
    data = copy.deepcopy(RAW)
    data["terms"].append(
        {"id": "T-0099", "version": 1, "source": "Un", "target": "ཀ", "case_sensitive": True}
    )
    assert "T-0099" in {t.term_id for t in Termbase.from_dict(data).terms}


# --- FR-400: matching ---


def test_fr400_longest_match_wins() -> None:
    found = find_terms("Visit the Department of Immigration today", TB)
    assert [t.term_id for _, _, t in found] == ["T-0001"]


def test_fr400_shorter_term_still_matches_on_its_own() -> None:
    found = find_terms("Ask the Department.", TB)
    assert [t.term_id for _, _, t in found] == ["T-0002"]


def test_fr400_case_sensitivity_follows_the_entry_flag() -> None:
    assert [t.term_id for _, _, t in find_terms("the UN office", TB)] == ["T-0004"]
    assert find_terms("the un office", TB) == []
    assert [t.term_id for _, _, t in find_terms("department OF immigration", TB)] == ["T-0001"]


def test_fr400_matches_whole_words_only() -> None:
    assert find_terms("coffee and feedback", TB) == []
    assert [t.term_id for _, _, t in find_terms("the fee, paid", TB)] == ["T-0005"]


def test_fr400_term_split_by_markup_is_not_matched() -> None:
    seg, terms = _sub("the Department of ⟦1⟧Immigration⟦/1⟧")
    # Only "Department" sits whole inside one text run.
    assert [t.term_id for t in terms.values()] == ["T-0002"]


# --- FR-401: the model never sees the English term; the target always appears ---


def test_fr401_english_term_never_reaches_the_model() -> None:
    seg, _ = _sub("Pay the fee at the Department of Immigration by 30 June 2026")
    for fmt in MODEL_FORMATS.values():
        sent = fmt.encode(seg).lower()
        assert "department" not in sent
        assert " fee " not in f" {sent} "


def test_fr401_approved_target_appears_in_the_output() -> None:
    source, terms = _sub("Pay the fee at the Department of Immigration")
    out = restore_terms(source, source, terms)
    text = out.plain_text()
    for target in _targets(["T-0005", "T-0001"]):
        assert target in text


def test_fr401_term_ids_continue_after_entity_ids() -> None:
    seg, terms = _sub("Pay Nu. 500 fee")
    assert seg.to_wire() == "Pay ⟦CUR:1⟧ ⟦T:2⟧"
    assert list(terms) == [2]


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ("Pay ⟦CUR:1⟧", "missing"),
        ("Pay ⟦CUR:1⟧ ⟦T:2⟧ ⟦T:2⟧", "duplicate"),
        ("Pay ⟦CUR:1⟧ ⟦T:2⟧ ⟦T:7⟧", "unknown"),
    ],
)
def test_fr401_bad_term_output_falls_back(output: str, reason: str) -> None:
    source, terms = _sub("Pay Nu. 500 fee")
    with pytest.raises(GlossaryCheckError) as err:
        restore_terms(Segment(tokenize(output, allow_entities=True)), source, terms)
    assert err.value.reason == reason
    assert err.value.cause == "glossary_term_missing"


def test_fr401_target_with_tibetan_digit_is_not_a_numeral_leak() -> None:
    """The leak scan checks model text, not approved glossary targets (FR-142)."""
    masked, entities = mask(parse("Submit Form Five by 2026-06-30"))
    with_terms, terms = substitute(masked, TB)
    after_entities = restore(with_terms, with_terms, entities)
    served = restore_terms(after_entities, with_terms, terms).plain_text()
    assert _targets(["T-0006"])[0] in served
    assert "2026-06-30" in served


# --- FR-153: glossary fingerprint ---


def test_fr153_fingerprint_depends_only_on_matched_term_versions() -> None:
    _, a = _sub("Pay the fee to the Department")
    _, b = _sub("the Department takes the fee, the fee")
    assert fingerprint(a.values()) == fingerprint(b.values())
    _, c = _sub("the UN fee")
    assert fingerprint(c.values()) != fingerprint(a.values())


def test_fr153_bumping_a_term_version_changes_only_segments_using_it() -> None:
    data = copy.deepcopy(RAW)
    data["terms"][4]["version"] = 2  # T-0005 "fee"
    bumped = Termbase.from_dict(data)
    fee_seg, _ = mask(parse("Pay the fee"))
    other_seg, _ = mask(parse("Visit the UN"))
    for seg, changes in ((fee_seg, True), (other_seg, False)):
        before = fingerprint(substitute(seg, TB)[1].values())
        after = fingerprint(substitute(seg, bumped)[1].values())
        assert (before != after) is changes


def test_fr153_no_terms_has_a_stable_fingerprint() -> None:
    assert fingerprint([]) == fingerprint(())


# --- FR-402: compliance ---


def test_fr402_compliance_rate_per_batch() -> None:
    stats = ComplianceStats()
    stats.record(3, restored=True)
    stats.record(1, restored=False)
    stats.record(0, restored=False)
    assert (stats.found, stats.restored) == (4, 3)
    assert stats.rate == 0.75
    assert ComplianceStats().rate == 1.0


# --- end to end with the adversarial mock ---


@pytest.mark.parametrize("fmt_name", sorted(MODEL_FORMATS))
def test_fr401_under_adversarial_model_targets_are_exact_or_block_falls_back(fmt_name: str) -> None:
    fmt = MODEL_FORMATS[fmt_name]
    rng = random.Random(606)  # noqa: S311 - deterministic test data
    mock = MockNMT(fmt, seed=606, modes={m: 1.0 for m in Mode})
    phrases = ["the fee", "Department of Immigration", "UN", "citizenship certificate",
               "Nu. 500", "30 June 2026", "Form Five", "apply", "online"]
    served = 0
    for _ in range(1000):
        text = " ".join(rng.choice(phrases) for _ in range(rng.randint(2, 6)))
        masked, entities = mask(parse(text))
        with_terms, terms = substitute(masked, TB)
        try:
            decoded = fmt.decode(mock.translate(with_terms), with_terms)
            out = restore_terms(restore(decoded, with_terms, entities), with_terms, terms)
        except (SegmentError, UpstreamError):
            continue
        served += 1
        text_out = out.plain_text()
        for term in terms.values():
            assert text_out.count(term.target) >= 1
        assert find_terms(text_out, TB) == []  # no English term survives, as a whole word
    assert served > 0
