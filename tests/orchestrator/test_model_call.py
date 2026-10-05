"""How a segment is sent to the model (S0.1 outcome). Requirements: FR-122, FR-140, FR-155,
FR-156, FR-210.

Three rules, decided 2026-10-05 from the staging measurements
(docs/designs/s0-placeholder-survival.md):

* text with no words outside its placeholders is never sent;
* text with inline tags is sent piece by piece, and the tags are put back by
  us, so the model can never lose one;
* the xml format is the default, and decoding it forgives damaged tag syntax
  but never a missing, extra or unknown placeholder.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestrator.pipeline.protect import EntityCheckError, mask, restore
from orchestrator.pipeline.segment import (
    DEFAULT_MODEL_FORMAT,
    MODEL_FORMATS,
    Close,
    Entity,
    Open,
    Segment,
    SegmentError,
    Text,
    Void,
    has_words,
    parse,
    split_at_tags,
)
from orchestrator.pipeline.tags import check_tags
from orchestrator.service.model_call import calls_needed, translate_segment
from orchestrator.testing.rig import ORIGIN, body, make_client, make_rig

XML = MODEL_FORMATS["xml"]


class Recorder:
    """A translator that records what it was sent and answers with a marked copy."""

    model_version = "test"

    def __init__(self, answer: Any = None) -> None:
        self.sent: list[str] = []
        self.answer = answer or (lambda text: f"DZ[{text}]")

    async def translate(self, model_text: str, reference: Segment) -> str:
        self.sent.append(model_text)
        return str(self.answer(model_text))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- has_words / split_at_tags ---


@pytest.mark.parametrize(
    ("wire", "words"),
    [
        ("Apply online", True),
        ("⟦CUR:1⟧", False),
        ("⟦CUR:1⟧ .", False),
        ("⟦T:1⟧", False),
        ("12 / 30", False),
        ("Fee ⟦CUR:1⟧", True),
    ],
)
def test_fr155_only_text_with_words_is_worth_sending(wire: str, words: bool) -> None:
    assert has_words(parse(wire, allow_entities=True)) is words


def test_fr210_splitting_at_tags_and_joining_gives_the_segment_back() -> None:
    seg = parse("Read the ⟦1⟧rules for ⟦CUR:2⟧⟦/1⟧ now.⟦v3/⟧Done", allow_entities=True)
    pieces = split_at_tags(seg)
    assert [type(p).__name__ for p in pieces] == [
        "Segment",
        "Open",
        "Segment",
        "Close",
        "Segment",
        "Void",
        "Segment",
    ]
    joined: list[Any] = []
    for p in pieces:
        joined.extend(p.tokens if isinstance(p, Segment) else (p,))
    assert Segment(tuple(joined)) == seg


# --- translate_segment ---


def test_fr155_a_placeholder_only_segment_is_never_sent() -> None:
    model = Recorder()
    seg = parse("⟦CUR:1⟧", allow_entities=True)
    assert run(translate_segment(model, XML, seg)) == seg
    assert model.sent == [] and calls_needed(seg) == 0


def test_fr122_a_segment_without_tags_is_one_call() -> None:
    model = Recorder()
    seg = parse("Pay ⟦CUR:1⟧ by ⟦DATE:2⟧.", allow_entities=True)
    run(translate_segment(model, XML, seg))
    assert model.sent == ["Pay <e1/> by <e2/>."] and calls_needed(seg) == 1


def test_fr210_tagged_text_is_sent_piece_by_piece_and_no_tag_is_ever_sent() -> None:
    model = Recorder()
    seg = parse("Read the ⟦1⟧eligibility rules⟦/1⟧ before you apply.")
    out = run(translate_segment(model, XML, seg))
    assert sorted(model.sent) == ["Read the", "before you apply.", "eligibility rules"]
    assert not any("<x" in s for s in model.sent)
    assert calls_needed(seg) == 3
    assert out.structure() == seg.structure()  # our tags, in the source's order
    check_tags(out, seg)


def test_fr210_the_spaces_around_a_link_are_the_pages_own() -> None:
    model = Recorder(lambda text: f"<{text}>")
    seg = parse("Read the ⟦1⟧rules⟦/1⟧ before you apply.")
    out = run(translate_segment(model, XML, seg))
    assert out.tokens == (
        Text("<Read the> "),
        Open(1),
        Text("<rules>"),
        Close(1),
        Text(" <before you apply.>"),
    )


def test_fr210_a_piece_with_no_words_is_not_sent() -> None:
    model = Recorder()
    seg = parse("⟦1⟧Form Five⟦/1⟧ ⟦v2/⟧⟦CUR:3⟧", allow_entities=True)
    out = run(translate_segment(model, XML, seg))
    assert model.sent == ["Form Five"]
    assert [t for t in out.tokens if isinstance(t, Entity | Void)] == [Void(2), Entity("CUR", 3)]


def test_fr140_entities_inside_a_piece_travel_with_it() -> None:
    model = Recorder(lambda text: text.replace("Pay", "DZpay"))
    masked, entities = mask(parse("Pay Nu. 500 ⟦1⟧online⟦/1⟧ today."))
    out = run(translate_segment(model, XML, masked))
    assert model.sent[0] == "Pay <e2/>"  # the link is tag 1, the fee entity 2
    restored = restore(out, masked, entities)
    assert "Nu. 500" in restored.to_wire()


def test_fr140_a_lost_entity_in_a_piece_still_fails() -> None:
    """Pieces protect tags, not entities: those are still checked after."""
    model = Recorder(lambda text: "DZ")
    masked, entities = mask(parse("Pay Nu. 500 ⟦1⟧online⟦/1⟧ today."))
    out = run(translate_segment(model, XML, masked))
    with pytest.raises(EntityCheckError):
        restore(out, masked, entities)


# --- the lenient xml decoder ---


@pytest.mark.parametrize(
    "answer",
    [
        "DZ <e1/> DZ <e2/>.",
        "DZ <e1> DZ <e2>.",  # missing slash: measured
        "DZ <E1/> DZ < e2 / >.",  # case and spaces: measured
    ],
)
def test_fr122_damaged_tag_syntax_is_forgiven(answer: str) -> None:
    seg = parse("Pay ⟦CUR:1⟧ by ⟦DATE:2⟧.", allow_entities=True)
    decoded = XML.decode(answer, seg)
    assert decoded.structure() == seg.structure()


@pytest.mark.parametrize(
    "answer",
    [
        "DZ <e9/> DZ <e2/>.",  # unknown id
        "DZ </e1> DZ <e2/>.",  # an entity cannot close
        "DZ <x5/> DZ <e2/>.",  # self-closing tag that is not a void in the source
        "DZ <e1/ DZ <e2/>.",  # a broken fragment left in the text
    ],
)
def test_fr122_damaged_tag_identity_is_not(answer: str) -> None:
    seg = parse("Pay ⟦CUR:1⟧ by ⟦DATE:2⟧.", allow_entities=True)
    with pytest.raises(SegmentError):
        XML.decode(answer, seg)


def test_fr140_a_transliterated_placeholder_is_lost_not_guessed() -> None:
    """Measured: <e1/> came back as <ཨི་༡>. It is not mapped back; the entity check refuses it."""
    masked, entities = mask(parse("Fee Nu. 500"))
    decoded = XML.decode("DZ <ཨི་༡/>", masked)
    with pytest.raises(EntityCheckError):
        restore(decoded, masked, entities)


def test_fr122_a_broken_tag_fragment_is_refused() -> None:
    seg = parse("Fee ⟦CUR:1⟧", allow_entities=True)
    with pytest.raises(SegmentError):
        XML.decode("DZ <e1", seg)  # no closing bracket: a fragment, not a tag


def test_fr122_xml_is_the_default_format() -> None:
    assert DEFAULT_MODEL_FORMAT == "xml"


# --- through the API ---


def test_fr210_a_link_paragraph_translates_where_it_used_to_fall_back() -> None:
    rig = make_rig()
    client = make_client(rig)
    response = client.post(
        "/v1/translate",
        json=body("Read the ⟦1⟧eligibility rules⟦/1⟧ before you apply."),
        headers={"Origin": ORIGIN},
    )
    (segment,) = response.json()["segments"]
    assert segment["status"] == "translated"
    assert "⟦1⟧" in segment["text"] and "⟦/1⟧" in segment["text"]
    assert rig.translator.calls == 3


def test_fr156_a_piecewise_segment_takes_one_quota_token_per_call() -> None:
    rig = make_rig(rps=4.0)  # live share: 2 tokens
    client = make_client(rig)
    response = client.post(
        "/v1/translate",
        json=body("Read the ⟦1⟧eligibility rules⟦/1⟧ before you apply."),
        headers={"Origin": ORIGIN},
    )
    (segment,) = response.json()["segments"]
    assert segment["status"] == "pending_mt"  # three calls, two tokens: queued instead
    assert rig.translator.calls == 0


# --- values after a label (seen on the pilot portal, 2026-10-05) ---


def _masked_wire(text: str) -> Segment:
    seg, _ = mask(parse(text))
    return seg


@pytest.mark.parametrize(
    ("text", "sent"),
    [
        ("Email ID: help@portal.gov.example", "Email ID:"),
        ("⟦1⟧Phone number: +975-17000001⟦/1⟧", "Phone number:"),
        ("Contact - 17000001 / 77000002", "Contact -"),
    ],
)
def test_fr140_only_the_label_is_sent_and_its_values_come_back_unchanged(
    text: str, sent: str
) -> None:
    seg = _masked_wire(text)
    model = Recorder()
    out = run(translate_segment(model, XML, seg))
    assert model.sent == [sent]
    assert [t for t in out.tokens if isinstance(t, Entity)] == [
        t for t in seg.tokens if isinstance(t, Entity)
    ]
    assert out.to_wire().startswith(seg.to_wire()[: seg.to_wire().index(sent)] + f"DZ[{sent}]")


@pytest.mark.parametrize(
    "text",
    [
        "Write to help@portal.gov.example for help",  # inside a sentence: word order
        "Write to help@portal.gov.example",  # no label: the model places it
    ],
)
def test_fr140_a_value_inside_a_sentence_is_still_sent(text: str) -> None:
    seg = _masked_wire(text)
    model = Recorder(lambda text: f"DZ {text}")
    run(translate_segment(model, XML, seg))
    assert "<e1/>" in model.sent[0]


def test_fr140_values_in_brackets_at_the_end_are_not_sent() -> None:
    """Seen on the pilot portal: "(www...)" at the end came back in Tibetan letters."""
    seg = _masked_wire("Submit it online through the system (www.portal.gov.example).")
    model = Recorder()
    out = run(translate_segment(model, XML, seg))
    assert model.sent == ["Submit it online through the system"]
    assert out.to_wire() == "DZ[Submit it online through the system] (⟦URL:1⟧)."


def test_fr140_brackets_with_words_in_them_are_still_sent() -> None:
    seg = _masked_wire("Call the office (or write to help@portal.gov.example).")
    model = Recorder(lambda text: f"DZ {text}")
    run(translate_segment(model, XML, seg))
    assert "<e1/>" in model.sent[0]


# --- sentence by sentence (seen on the pilot portal, 2026-10-05) ---


def _sentences_of(text: str) -> list[str]:
    from orchestrator.service.model_call import split_sentences

    return [s.to_wire() for s, _ in split_sentences(parse(text))]


@pytest.mark.parametrize(
    ("text", "sentences"),
    [
        (
            "Apply online. Pay the fee! Is it done?",
            ["Apply online.", "Pay the fee!", "Is it done?"],
        ),
        ("Pay Nu. 500 now. Dr. Wangmo signs it.", ["Pay Nu. 500 now.", "Dr. Wangmo signs it."]),
        ("Bring it, e.g. Form A. Then wait.", ["Bring it, e.g. Form A. Then wait."]),
        ("1. Submit the form. 2. Pay the fee.", ["1. Submit the form.", "2. Pay the fee."]),
        (
            "It doubled (TANG, 2009).The colleges grew.",
            ["It doubled (TANG, 2009).", "The colleges grew."],
        ),
        ('He said "Apply now." Then he left.', ['He said "Apply now."', "Then he left."]),
        ("Version 2.5 of the form. Use it.", ["Version 2.5 of the form.", "Use it."]),
        ("One sentence only", ["One sentence only"]),
        (
            "Built on . Net Three-layer design. It works.",
            ["Built on . Net Three-layer design.", "It works."],
        ),
    ],
)
def test_fr122_a_piece_is_sent_sentence_by_sentence(text: str, sentences: list[str]) -> None:
    assert _sentences_of(text) == sentences


def test_fr122_sentences_go_separately_and_come_back_with_the_source_spacing() -> None:
    seg = parse("Apply online.  Pay the fee.")
    model = Recorder()
    out = run(translate_segment(model, XML, seg))
    assert sorted(model.sent) == ["Apply online.", "Pay the fee."]
    assert out.to_wire() == "DZ[Apply online.]  DZ[Pay the fee.]"
    assert calls_needed(seg) == 2


def test_fr140_a_placeholder_stays_in_its_sentence() -> None:
    seg = _masked_wire("Write to help@portal.gov.example. Then wait for a reply.")
    model = Recorder()
    out = run(translate_segment(model, XML, seg))
    assert sorted(model.sent) == ["Then wait for a reply.", "Write to <e1/>."]
    assert [t for t in out.tokens if isinstance(t, Entity)] == [
        t for t in seg.tokens if isinstance(t, Entity)
    ]


def test_fr210_sentences_inside_a_link_piece_keep_the_tags_around_them() -> None:
    seg = parse("⟦1⟧Read this. Then apply.⟦/1⟧ Thank you.")
    model = Recorder()
    out = run(translate_segment(model, XML, seg))
    assert out.to_wire() == "⟦1⟧DZ[Read this.] DZ[Then apply.]⟦/1⟧ DZ[Thank you.]"
    assert calls_needed(seg) == 3
