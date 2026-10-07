"""Offline Tier 1 seed (S7.0). Requirements: FR-413, FR-410, FR-411, FR-510, FR-620.

The reviewer in these tests is a helper that does what a careful one would:
every target filled with Dzongkha, every locked placeholder kept where it was,
the unit marked approved. Each rejection test then breaks one row the way a
real one gets broken -- a placeholder dropped, two swapped, a number typed in.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

import pytest

from orchestrator.governance.audit import Action
from orchestrator.ops.seed import (
    XLIFF_NS,
    coverage,
    export_xliff,
    import_xliff,
    render_coverage,
    tier1_units,
)
from orchestrator.store.models import Origin, ReviewState
from orchestrator.testing.rig import ORIGIN, Rig, body, make_client, make_rig

DZ = "\u0f60\u0f56\u0fb2\u0f74\u0f42\u0f0b\u0f42\u0f72\u0f0b"  # Dzongkha words, as filler
NS = {"x": XLIFF_NS}
REVIEWER = "dcdd:reviewer-1"

SEGMENTS = [
    {"text": "Fees are non-refundable after 30 June 2026.", "path": "/legal/terms"},
    {"text": "Fees are non-refundable after 30 June 2026.", "path": "/legal/privacy"},
    {"text": "Read the ⟦1⟧renewal guide⟦/1⟧ first.", "path": "/legal/terms"},
    {
        "text": "The processing fee is listed below.",
        "path": "/services/renewal",
        "selector_tier": 1,
    },
    {"text": "Apply online for a passport.", "path": "/services/renewal"},  # Tier 2
]


def _rig() -> tuple[Rig, Any]:
    rig = make_rig()
    site = rig.sites.get("portal")
    assert site is not None
    return rig, site


def _exported(rig: Rig, site: Any) -> str:
    units, _ = tier1_units(rig.service, site, SEGMENTS)
    return export_xliff(units, site.site_id)


def _reviewed(xliff: str, breaks: Callable[[str, ET.Element], None] | None = None) -> bytes:
    """Fill every target with Dzongkha around the same placeholders; approve each unit."""
    ET.register_namespace("", XLIFF_NS)
    root = ET.fromstring(xliff)  # noqa: S314 - our own export, not outside input
    for unit in root.iter(f"{{{XLIFF_NS}}}trans-unit"):
        source = unit.find("x:source", NS)
        target = unit.find("x:target", NS)
        assert source is not None and target is not None
        target.clear()
        target.text = DZ if (source.text or "").strip() else source.text
        for child in source:
            clone = copy.deepcopy(child)
            for node in clone.iter():
                if (node.text or "").strip():
                    node.text = DZ
            if (child.tail or "").strip():
                clone.tail = DZ
            target.append(clone)
        target.set("state", "translated")
        unit.set("approved", "yes")
        if breaks is not None:
            breaks(unit.get("id", ""), target)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# --- export ---


def test_fr413_export_holds_only_tier1_once_each_with_its_pages() -> None:
    rig, site = _rig()
    units, already = tier1_units(rig.service, site, SEGMENTS)
    assert already == 0
    assert len(units) == 3  # the repeated notice once, the link, the selector-tiered fee
    repeated = next(u for u in units if u.source.startswith("Fees are"))
    assert repeated.paths == ["/legal/terms", "/legal/privacy"]
    assert not any("Apply online" in u.source for u in units)  # Tier 2 is not for reviewers


def test_fr413_placeholders_are_locked_codes_showing_what_they_stand_for() -> None:
    rig, site = _rig()
    xliff = _exported(rig, site)
    root = ET.fromstring(xliff)  # noqa: S314 - our own export, not outside input
    codes = root.findall(".//x:source/x:x", NS)
    shown = {c.get("ctype"): c.get("equiv-text") for c in codes}
    assert shown["x-dzweb-date"] == "30 June 2026"  # the value, for the reviewer to see
    assert any(k == "x-dzweb-term" for k in shown)  # a glossary term, with its Dzongkha
    assert root.findall(".//x:source/x:g", NS), "the link is a paired tag"
    assert "30 June 2026" not in "".join(root.find(".//x:source", NS).itertext())  # type: ignore[union-attr]


# --- import ---


def test_fr413_an_approved_row_becomes_a_human_translation_with_an_audit_event() -> None:
    rig, site = _rig()
    report = import_xliff(rig.service, site, _reviewed(_exported(rig, site)), REVIEWER)
    assert report.rejected == [] and len(report.approved) == 3

    key = report.approved[0]
    (version,) = rig.tm.history(key)
    assert version.origin is Origin.HUMAN and version.author == REVIEWER
    assert rig.tm.review_item(key).state is ReviewState.APPROVED  # type: ignore[union-attr]
    events = rig.audit.events(action=Action.SEED_IMPORT)
    assert len(events) == 3
    assert str(events[0].detail["batch"]).startswith("seed-")


def test_fr510_a_seeded_translation_is_what_a_tier1_page_now_shows() -> None:
    rig, site = _rig()
    api = make_client(rig)
    text = "Fees are non-refundable after 30 June 2026."
    before = api.post(
        "/v1/translate", json=body(text, path="/legal/terms"), headers={"Origin": ORIGIN}
    )
    assert before.json()["segments"][0]["status"] == "tier_blocked"

    import_xliff(rig.service, site, _reviewed(_exported(rig, site)), REVIEWER)
    after = api.post(
        "/v1/translate", json=body(text, path="/legal/terms"), headers={"Origin": ORIGIN}
    )
    (segment,) = after.json()["segments"]
    assert segment["status"] == "translated" and segment["origin"] == "human"
    assert "30 June 2026" in segment["text"] and DZ in segment["text"]  # the date restored exactly


def _drop_x(target: ET.Element) -> None:
    for parent in target.iter():
        for child in list(parent):
            if child.tag.endswith("}x"):
                parent.remove(child)
                return


def _duplicate_x(target: ET.Element) -> None:
    x = next(c for c in target.iter() if c.tag.endswith("}x"))
    target.append(copy.deepcopy(x))


def _type_a_number(target: ET.Element) -> None:
    target.text = (target.text or "") + " 31 "


def _no_dzongkha(target: ET.Element) -> None:
    for node in target.iter():
        if node.text:
            node.text = "TODO"
        if node.tail:
            node.tail = " "


def _unknown(target: ET.Element) -> None:
    ET.SubElement(target, f"{{{XLIFF_NS}}}x", {"id": "e99"})


@pytest.mark.parametrize(
    ("breaks", "reason"),
    [
        (_drop_x, "placeholders"),
        (_duplicate_x, "placeholders"),
        (_type_a_number, "placeholders"),
        (_no_dzongkha, "not translated"),
        (_unknown, "unknown placeholder"),
    ],
)
def test_fr413_a_broken_row_is_rejected_with_a_reason(
    breaks: Callable[[ET.Element], None], reason: str
) -> None:
    rig, site = _rig()
    xliff = _exported(rig, site)
    victim = ET.fromstring(xliff).findall(".//x:trans-unit", NS)[0].get("id")  # noqa: S314 - our own export, not outside input

    def one(uid: str, target: ET.Element) -> None:
        if uid == victim:
            breaks(target)

    report = import_xliff(rig.service, site, _reviewed(xliff, one), REVIEWER)
    assert [uid for uid, _ in report.rejected] == [victim]
    assert reason in report.rejected[0][1]
    assert rig.tm.history(victim) == [], "a rejected row was stored"
    assert len(report.approved) == 2  # the good rows still go in


def test_fr413_reordered_tags_are_rejected() -> None:
    rig, site = _rig()
    tagged = [{"text": "⟦1⟧Apply⟦/1⟧ and ⟦2⟧pay⟦/2⟧ here.", "path": "/legal/x"}]
    units, _ = tier1_units(rig.service, site, tagged)

    def swap(uid: str, target: ET.Element) -> None:
        gs = [c for c in target if c.tag.endswith("}g")]
        gs[0].set("id", "t2"), gs[1].set("id", "t1")

    report = import_xliff(
        rig.service, site, _reviewed(export_xliff(units, "portal"), swap), REVIEWER
    )
    assert len(report.rejected) == 1 and "placeholders" in report.rejected[0][1]


def test_fr413_a_row_whose_english_changed_since_export_is_rejected() -> None:
    rig, site = _rig()

    def tamper(uid: str, target: ET.Element) -> None:
        pass

    raw = _reviewed(_exported(rig, site), tamper).replace(b"non-refundable", b"refundable")
    report = import_xliff(rig.service, site, raw, REVIEWER)
    assert any("source changed since export" in r for _, r in report.rejected)


def test_fr413_unapproved_rows_wait_and_nothing_is_written_on_a_dry_run() -> None:
    rig, site = _rig()
    xliff = _exported(rig, site)
    assert import_xliff(rig.service, site, xliff.encode(), REVIEWER).not_approved == 3

    report = import_xliff(rig.service, site, _reviewed(xliff), REVIEWER, dry_run=True)
    assert len(report.approved) == 3
    assert all(rig.tm.history(uid) == [] for uid in report.approved)
    assert rig.audit.events(action=Action.SEED_IMPORT) == []


def test_fr160_zero_width_characters_a_reviewer_pasted_are_not_stored() -> None:
    rig, site = _rig()

    def paste(uid: str, target: ET.Element) -> None:
        target.text = (target.text or "") + "\u200b"

    report = import_xliff(rig.service, site, _reviewed(_exported(rig, site), paste), REVIEWER)
    assert not report.rejected
    for uid in report.approved:
        assert "\u200b" not in rig.tm.history(uid)[0].masked_target


def test_fr413_a_file_with_a_dtd_is_refused() -> None:
    rig, site = _rig()
    hostile = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><xliff/>'
    with pytest.raises(ValueError, match="DTD"):
        import_xliff(rig.service, site, hostile, REVIEWER)


def test_fr413_export_leaves_out_what_is_already_approved() -> None:
    rig, site = _rig()
    import_xliff(rig.service, site, _reviewed(_exported(rig, site)), REVIEWER)
    units, already = tier1_units(rig.service, site, SEGMENTS)
    assert units == [] and already == 3


# --- coverage ---


def test_s70_coverage_per_page_before_and_after_the_seed() -> None:
    rig, site = _rig()
    before = {c.path: (c.tier1, c.approved) for c in coverage(rig.service, site, SEGMENTS)}
    assert before == {"/legal/privacy": (1, 0), "/legal/terms": (2, 0), "/services/renewal": (1, 0)}

    import_xliff(rig.service, site, _reviewed(_exported(rig, site)), REVIEWER)
    pages = coverage(rig.service, site, SEGMENTS)
    assert all(c.approved == c.tier1 and c.english == [] for c in pages)
    assert "TOTAL" in render_coverage(pages)


def test_s70_reads_the_extractors_own_output(tmp_path: Any) -> None:
    """scripts/export-segments.mjs writes pages, each with its path (found end to end)."""
    import json

    from orchestrator.ops.seed import load_segments

    exported = {
        "pages": [
            {
                "file": "a.html",
                "path": "/legal/terms",
                "segments": [{"text": "Fees apply.", "tier": None, "selector_tier": None}],
            },
            {
                "file": "b.html",
                "path": "/services/x",
                "segments": [{"text": "Apply online.", "tier": None, "selector_tier": 1}],
            },
        ]
    }
    path = tmp_path / "segments.json"
    path.write_text(json.dumps(exported), encoding="utf-8")
    assert load_segments(path) == [
        {"text": "Fees apply.", "tier": None, "selector_tier": None, "path": "/legal/terms"},
        {"text": "Apply online.", "tier": None, "selector_tier": 1, "path": "/services/x"},
    ]
