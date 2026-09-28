"""S1.2 — server segment grammar. Requirements: FR-120, FR-121, FR-122, FR-124."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from orchestrator.locale.dz import SPLIT_TERMINATORS
from orchestrator.pipeline.segment import (
    MODEL_FORMATS,
    Close,
    Entity,
    Open,
    Segment,
    SegmentError,
    Text,
    Token,
    Void,
    escape,
    parse,
    split_long,
    validate,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "extraction" / "cases.json"
CASES = json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]
FIXTURE_SEGMENTS = [
    pytest.param(seg, id=f"{case['name']}#{i}")
    for case in CASES
    for i, seg in enumerate(case["expected"]["segments"])
]


# --- shared fixtures (ER-O6): widget output must parse identically on the server ---


@pytest.mark.parametrize("seg", FIXTURE_SEGMENTS)
def test_fr121_shared_fixture_segments_parse_and_round_trip(seg: dict[str, object]) -> None:
    parsed = parse(str(seg["text"]))
    assert parsed.to_wire() == seg["text"]
    assert parsed.slots() == seg["slots"]
    assert parsed.plain_text() == "".join(seg["slots"])  # type: ignore[arg-type]


# --- escaping ---

_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=60)


@given(_text)
def test_fr121_any_text_escapes_and_round_trips(value: str) -> None:
    wire = escape(value)
    seg = parse(wire)
    assert seg.to_wire() == wire
    assert seg.plain_text() == value


def test_fr121_literal_delimiters_are_text_not_markers() -> None:
    seg = parse("Use ⟦⟦1⟧⟧ literally")
    assert seg.structure() == ()
    assert seg.plain_text() == "Use ⟦1⟧ literally"


# --- structural round trip over generated well-formed segments ---


@st.composite
def _segments(draw: st.DrawFn) -> Segment:
    tokens: list[Token] = []
    next_id = 1
    for _ in range(draw(st.integers(0, 6))):
        kind = draw(st.sampled_from(["text", "pair", "void"]))
        if kind == "text":
            tokens.append(Text(draw(_text.filter(bool))))
        elif kind == "void":
            tokens.append(Void(next_id))
            next_id += 1
        else:
            tokens += [Open(next_id), Text(draw(_text.filter(bool))), Close(next_id)]
            next_id += 1
    merged: list[Token] = []
    for t in tokens:  # adjacent Text tokens are one token after parsing
        if merged and isinstance(t, Text) and isinstance(merged[-1], Text):
            merged[-1] = Text(merged[-1].value + t.value)
        else:
            merged.append(t)
    return Segment(tuple(merged))


@given(_segments())
def test_fr122_parse_of_serialised_segment_is_identity(seg: Segment) -> None:
    assert parse(seg.to_wire()) == seg
    assert len(seg.slots()) == len(seg.structure()) + 1


# --- rejection: malformed or hostile input never reaches the model ---


@pytest.mark.parametrize(
    ("wire", "cause"),
    [
        ("Stray ⟧ close", "unescaped_delimiter"),
        ("Open ⟦1 never closed", "unterminated_marker"),
        ("Nested ⟦⟦1⟧ bad", "unescaped_delimiter"),
        ("Bad ⟦x⟧ marker", "malformed_marker"),
        ("Space ⟦ 1⟧ marker", "malformed_marker"),
        ("⟦1⟧unclosed", "unbalanced"),
        ("close only⟦/1⟧", "unbalanced"),
        ("⟦1⟧a⟦2⟧b⟦/1⟧c⟦/2⟧", "unbalanced"),
        ("⟦v1/⟧ then ⟦1⟧x⟦/1⟧", "duplicate_id"),
        ("Pay ⟦CUR:1⟧ now", "entity_not_allowed"),
    ],
)
def test_fr122_invalid_wire_is_rejected_with_a_cause(wire: str, cause: str) -> None:
    with pytest.raises(SegmentError) as err:
        parse(wire)
    assert err.value.cause == cause


def test_fr122_client_cannot_forge_entity_tokens_but_pipeline_can_parse_them() -> None:
    with pytest.raises(SegmentError):
        parse("Fee ⟦CUR:1⟧ due")
    seg = parse("Fee ⟦CUR:1⟧ due", allow_entities=True)
    assert seg.structure() == (Entity("CUR", 1),)


def test_fr122_nested_pairs_are_valid_for_proxy_preserve_nested_mode() -> None:
    validate(parse("⟦1⟧a ⟦2⟧b⟦/2⟧ c⟦/1⟧").tokens)


# --- FR-124: splitting over-long segments ---


def test_fr124_split_happens_after_terminators_and_rejoins_exactly() -> None:
    wire = "First sentence here. Second ⟦1⟧link text⟦/1⟧ sentence! Third one?"
    pieces = split_long(parse(wire), max_len=50, terminators=SPLIT_TERMINATORS)
    assert "".join(p.to_wire() for p in pieces) == wire
    assert all(len(p.to_wire()) <= 50 for p in pieces)
    assert len(pieces) >= 2


def test_fr124_never_splits_inside_a_placeholder_pair() -> None:
    wire = "Intro. ⟦1⟧Inside. Still inside. More inside.⟦/1⟧ Outro."
    pieces = split_long(parse(wire), max_len=50, terminators=SPLIT_TERMINATORS)
    for p in pieces:
        validate(p.tokens)  # every piece is balanced on its own
    assert "".join(p.to_wire() for p in pieces) == wire


def test_fr124_splits_dzongkha_at_shad() -> None:
    wire = "ཁྱོད་ཀྱི་ཞུ་ཡིག་ འཛིན་ཡོདཔ། ཡིག་ཆ་ ཚང་དགོ།"
    pieces = split_long(parse(wire), max_len=30, terminators=SPLIT_TERMINATORS)
    assert "".join(p.to_wire() for p in pieces) == wire
    assert pieces[0].to_wire().rstrip().endswith("།")


def test_fr124_unsplittable_segment_raises_too_long() -> None:
    with pytest.raises(SegmentError) as err:
        split_long(parse("x" * 80), max_len=20, terminators=SPLIT_TERMINATORS)
    assert err.value.cause == "too_long"


@given(_segments())
def test_fr124_split_pieces_always_rejoin_to_the_original(seg: Segment) -> None:
    try:
        pieces = split_long(seg, max_len=25, terminators=SPLIT_TERMINATORS)
    except SegmentError as err:
        assert err.cause == "too_long"
        return
    assert "".join(p.to_wire() for p in pieces) == seg.to_wire()
    for p in pieces:
        validate(p.tokens)


# --- model token formats (S0.1 candidates) ---

_ENTITY_SEGMENT = parse(
    "Pay ⟦1⟧⟦CUR:2⟧⟦/1⟧ by ⟦DATE:3⟧ at <counter> & desk⟦v4/⟧.", allow_entities=True
)


@pytest.mark.parametrize("name", sorted(MODEL_FORMATS))
@pytest.mark.parametrize("seg", [*FIXTURE_SEGMENTS, pytest.param(None, id="entities")])
def test_fr122_model_formats_round_trip(name: str, seg: dict[str, object] | None) -> None:
    fmt = MODEL_FORMATS[name]
    segment = _ENTITY_SEGMENT if seg is None else parse(str(seg["text"]))
    assert fmt.decode(fmt.encode(segment), segment) == segment


def test_fr122_xml_format_rejects_unknown_entities_and_stray_tags() -> None:
    fmt = MODEL_FORMATS["xml"]
    with pytest.raises(SegmentError):
        fmt.decode("Pay <e9/> now", _ENTITY_SEGMENT)
    with pytest.raises(SegmentError):
        fmt.decode("Pay <x1 broken", _ENTITY_SEGMENT)
