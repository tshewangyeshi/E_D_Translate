"""Restoration and tag validation in the service (S1.5). Requirements: FR-122, FR-123, FR-210.

The pipeline-level gate is tests/orchestrator/gates/test_tag_gate.py. Here:
the widget gets English on any tag failure; a markup caller gets the
translation with its formatting collapsed, which is served but never stored
(the widget shares the keys); and each batch reports its tag integrity.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from orchestrator.api.app import batch_tag_integrity
from orchestrator.pipeline.segment import Segment, parse
from orchestrator.service.translate import SegmentIn, SegmentOut, Status
from orchestrator.testing.rig import ORIGIN, body, make_client, make_rig

SOURCE = "Pay ⟦1⟧Nu. 500⟦/1⟧ at the ⟦2⟧counter⟦/2⟧ today."


class Answers:
    """A translator that answers every call with fixed model text."""

    model_version = "test"

    def __init__(self, text: str) -> None:
        self.text = text

    async def translate(self, model_text: str, reference: Segment) -> str:
        return self.text


def _accept(markup: bool) -> tuple[SegmentOut, Any]:
    rig = make_rig()
    site = rig.sites.get("portal")
    assert site is not None
    p = rig.service.prepare(site, SegmentIn("s0", SOURCE), "/services/renewal")
    entity = next(t for t in p.with_terms.structure() if getattr(t, "kind", None) == "CUR")
    # The model kept the amount but swapped the two spans round.
    decoded = parse(f"DZ ⟦2⟧DZ⟦/2⟧ ⟦1⟧⟦CUR:{entity.id}⟧⟦/1⟧ DZ", allow_entities=True)  # type: ignore[union-attr]
    return rig.service._accept_model_output(site, p, decoded, markup), rig


def test_fr122_the_widget_gets_english_when_tags_do_not_survive() -> None:
    out, rig = _accept(markup=False)
    assert out.status is Status.TAG_FALLBACK
    assert out.text == SOURCE
    assert not rig.tm._versions


def test_fr123_a_markup_caller_gets_the_translation_with_formatting_collapsed() -> None:
    out, _ = _accept(markup=True)
    assert out.status is Status.TAG_FALLBACK and out.reason == "formatting_collapsed"
    assert out.text == "⟦1⟧⟦2⟧DZ DZ Nu. 500 DZ⟦/2⟧⟦/1⟧"  # the amount restored, byte-identical


def test_fr210_a_collapsed_translation_is_never_stored() -> None:
    _, rig = _accept(markup=True)
    assert not rig.tm._versions and not rig.cache.data and not rig.queue.rows


def test_fr123_collapse_still_refuses_a_changed_amount() -> None:
    rig = make_rig()
    site = rig.sites.get("portal")
    assert site is not None
    p = rig.service.prepare(site, SegmentIn("s0", SOURCE), "/services/renewal")
    decoded = parse("DZ ⟦2⟧DZ⟦/2⟧ DZ", allow_entities=True)  # the amount is gone
    out = rig.service._accept_model_output(site, p, decoded, True)
    assert out.status is Status.ENTITY_CHECK_FAILED and out.text == SOURCE


def test_fr122_unreadable_model_tags_are_a_checked_tag_failure() -> None:
    rig = make_rig()
    rig.service.translator = Answers("DZ ⟦9⟧x⟦/9⟧")  # type: ignore[assignment]
    site = rig.sites.get("portal")
    assert site is not None
    segments = [SegmentIn("s0", "Apply online now.")]
    (out,) = asyncio.run(rig.service.translate(site, "c", segments, "/services/renewal"))
    assert out.status is Status.TAG_FALLBACK and out.model_checked
    assert out.text == "Apply online now."


def _out(status: Status, checked: bool) -> SegmentOut:
    return SegmentOut("s", "t", status, model_checked=checked)


def test_nfr200_tag_integrity_is_computed_per_batch() -> None:
    batch = [
        _out(Status.TRANSLATED, True),
        _out(Status.TRANSLATED, True),
        _out(Status.TAG_FALLBACK, True),
        _out(Status.ENTITY_CHECK_FAILED, True),  # its tags were not the problem
        _out(Status.TRANSLATED, False),  # a cache hit: the model was not asked
    ]
    assert batch_tag_integrity(batch) == " tag_integrity=0.75"
    assert batch_tag_integrity([_out(Status.TRANSLATED, False)]) == ""


@pytest.mark.parametrize(("answer", "expected"), [(None, "1.00"), ("DZ ⟦9⟧x⟦/9⟧", "0.00")])
def test_nfr200_each_request_logs_its_tag_integrity(
    answer: str | None, expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig()
    if answer is not None:
        rig.service.translator = Answers(answer)  # type: ignore[assignment]
    with caplog.at_level(logging.INFO, logger="orchestrator.api.app"):
        make_client(rig).post(
            "/v1/translate", json=body("Apply online now."), headers={"Origin": ORIGIN}
        )
    assert f"tag_integrity={expected}" in caplog.text
