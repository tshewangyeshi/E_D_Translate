"""chrF++ (tools/chrf.py) agrees with sacrebleu (S10.1, NFR-202).

Values computed with sacrebleu 2.6.0, CHRF(word_order=2), on 2026-10-06, and
reproduced exactly by tools/chrf.py. Pinned here so the scorer cannot drift
from the published metric without this failing.
"""

from __future__ import annotations

import pytest

from tools.chrf import corpus_chrf

DZ_HYP = "འབྲུག་གི་ཡིག་ཆ།"
DZ_REF = "འབྲུག་ཡིག་ཆ།"

SACREBLEU = [
    (["Apply online for a passport."], ["Apply online for a passport."], 100.0),
    (["Apply online for the passport."], ["Apply online for a passport."], 79.036070),
    (["(Fee) Nu. 500, payable today!"], ["Fee: Nu. 500 - payable today."], 59.098363),
    (["the the the fee"], ["the fee is the fee"], 40.229790),
    (["xyz"], ["Apply online."], 2.450980),
    ([DZ_HYP], [DZ_REF], 63.662902),
    (
        ["Apply online.", "Pay the fee at the counter.", "Offices close early."],
        ["Apply online today.", "Pay the fee at the office counter.", "The offices close early."],
        72.206489,
    ),
]


@pytest.mark.parametrize(("hyps", "refs", "expected"), SACREBLEU)
def test_nfr202_chrf_plus_plus_matches_sacrebleu(
    hyps: list[str], refs: list[str], expected: float
) -> None:
    assert corpus_chrf(hyps, refs) == pytest.approx(expected, abs=1e-5)


def test_nfr202_hypotheses_and_references_must_align() -> None:
    with pytest.raises(ValueError):
        corpus_chrf(["one"], ["one", "two"])
