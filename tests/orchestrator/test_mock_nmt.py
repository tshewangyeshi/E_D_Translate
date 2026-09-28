"""S1.4 — adversarial NMT mock. Supports FR-122, FR-141, FR-142."""

from __future__ import annotations

import re

import pytest

from orchestrator.pipeline.segment import MODEL_FORMATS, Segment, SegmentError, parse, validate
from orchestrator.testing.mock_nmt import (
    CORRUPTING_MODES,
    MockNMT,
    Mode,
    UpstreamTimeout,
    UpstreamUnavailable,
)

SEGMENT = parse("Pay ⟦1⟧the fee⟦/1⟧ of ⟦CUR:2⟧ by ⟦DATE:3⟧⟦v4/⟧ online.", allow_entities=True)
DIGITS = re.compile(r"[0-9༠-༩]")


def _detectable(fmt_name: str, output: str, reference: Segment) -> bool:
    """A corrupted output must be caught by decode, structure or leak checks."""
    fmt = MODEL_FORMATS[fmt_name]
    try:
        decoded = fmt.decode(output, reference)
        validate(decoded.tokens)
    except SegmentError:
        return True
    if decoded.structure() != reference.structure():
        return True
    return DIGITS.search(decoded.plain_text()) is not None


@pytest.mark.parametrize("fmt_name", sorted(MODEL_FORMATS))
def test_fr122_well_behaved_mock_preserves_structure(fmt_name: str) -> None:
    mock = MockNMT(MODEL_FORMATS[fmt_name])
    out = mock.translate(SEGMENT, Mode.WELL_BEHAVED)
    assert MODEL_FORMATS[fmt_name].decode(out, SEGMENT) == SEGMENT


@pytest.mark.parametrize("fmt_name", sorted(MODEL_FORMATS))
@pytest.mark.parametrize("mode", [m for m in CORRUPTING_MODES if m is not Mode.EMPTY])
def test_fr141_every_corrupting_mode_is_detectable(fmt_name: str, mode: Mode) -> None:
    for seed in range(50):
        out = MockNMT(MODEL_FORMATS[fmt_name], seed=seed).translate(SEGMENT, mode)
        assert _detectable(fmt_name, out, SEGMENT), (mode, seed, out)


def test_fr141_empty_mode_returns_nothing() -> None:
    assert MockNMT(MODEL_FORMATS["wire"]).translate(SEGMENT, Mode.EMPTY) == ""


def test_fr141_upstream_failures_raise() -> None:
    mock = MockNMT(MODEL_FORMATS["wire"])
    with pytest.raises(UpstreamTimeout):
        mock.translate(SEGMENT, Mode.TIMEOUT)
    with pytest.raises(UpstreamUnavailable):
        mock.translate(SEGMENT, Mode.UNAVAILABLE)


def test_fr141_mock_is_deterministic_under_a_seed() -> None:
    mix = {m: 1.0 for m in CORRUPTING_MODES} | {Mode.WELL_BEHAVED: 1.0}
    runs = []
    for _ in range(2):
        mock = MockNMT(MODEL_FORMATS["wire"], seed=1234, modes=mix)
        runs.append([mock.translate(SEGMENT) for _ in range(200)])
    assert runs[0] == runs[1]
    assert len(set(runs[0])) > 5  # the mix actually varies


def test_fr142_tibetan_digit_mode_emits_tibetan_digits() -> None:
    out = MockNMT(MODEL_FORMATS["wire"], seed=7).translate(SEGMENT, Mode.TIBETAN_DIGITS)
    assert re.search("[༠-༩]", out)
