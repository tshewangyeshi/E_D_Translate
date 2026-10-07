"""Offline Tier 1 seed (S7.0). Requirements: FR-413, FR-410, FR-411, FR-510, FR-620.

Tier 1 text -- fees, eligibility, legal notices -- is never machine translated
(FR-510): until a person approves a translation it stays English. Before the
reviewer interface exists, reviewers work offline:

    # segments from the pilot snapshots, with the real widget extractor
    (cd adapters/widget && npm run build && node scripts/export-segments.mjs \\
        --tier1 ".fees" page.html=/services/renewal ... > ../../segments.json)

    python -m orchestrator.ops.seed export   --site portal segments.json -o tier1.xlf
    #   ... reviewers translate tier1.xlf in a CAT tool and mark units approved ...
    python -m orchestrator.ops.seed import   --site portal --reviewer dcdd:reviewer-1 tier1.xlf
    python -m orchestrator.ops.seed coverage --site portal segments.json

The file is XLIFF 1.2. Inline tags are ``<g>``, line breaks and protected
values ``<x/>``: translation tools show them as locked codes, and each value's
``equiv-text`` shows the reviewer what it stands for ("Nu. 500", or a glossary
term with its approved Dzongkha). Only units the reviewer marked approved --
``approved="yes"`` on the unit, or a ``final`` / ``signed-off`` target -- are
imported. Each is checked before anything is written, and rejected with a
reason when:

  * its English changed since export (the pipeline re-derives it: a masker
    change shifts placeholders, so the row is refused rather than guessed at);
  * a placeholder is missing, repeated, unknown or out of order (FR-413);
  * the text invents a number, an email or an address the source lacks;
  * it carries no Dzongkha at all.

A valid row becomes an approved human translation (``origin = human``,
``review_item = approved``) with a ``seed.import`` audit event (FR-620),
through the same ``TranslationStore.approve`` the reviewer interface will use.

Snapshots must be public pages with synthetic data (docs/CLAUDE.md); the
export carries their text to reviewers outside this system.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from orchestrator.governance.audit import Action
from orchestrator.governance.sites import Site
from orchestrator.locale.dz import has_dzongkha, strip_render_artefacts
from orchestrator.pipeline.glossary import TERM_KIND
from orchestrator.pipeline.segment import (
    Close,
    Entity,
    Open,
    Segment,
    SegmentError,
    Text,
    Token,
    Void,
)
from orchestrator.pipeline.validate import validate_output
from orchestrator.service.translate import SegmentIn, TranslateService
from orchestrator.store.lookup import LookupItem

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
APPROVED_STATES = frozenset({"final", "signed-off"})


@dataclass
class Unit:
    """One Tier 1 segment to translate: the same text on several pages is one unit."""

    key: str  # segment_key
    source: str  # the English, as extracted (wire format)
    prepared: Any
    paths: list[str] = field(default_factory=list)


def tier1_units(
    service: TranslateService, site: Site, segments: list[dict[str, Any]]
) -> tuple[list[Unit], int]:
    """Tier 1 segments not yet approved, de-duplicated; and how many were approved already."""
    units: dict[str, Unit] = {}
    for n, raw in enumerate(segments):
        seg_in = SegmentIn(f"s{n}", str(raw["text"]), raw.get("tier"), raw.get("selector_tier"))
        try:
            p = service.prepare(site, seg_in, raw.get("path"))
        except SegmentError:
            continue
        if p.tier != 1:
            continue
        unit = units.setdefault(p.keys.segment_key, Unit(p.keys.segment_key, seg_in.text, p))
        if raw.get("path") and raw["path"] not in unit.paths:
            unit.paths.append(str(raw["path"]))
    todo = list(units.values())
    hits = service.store.lookup([LookupItem(u.prepared.keys, 1) for u in todo])
    pending = [u for u, hit in zip(todo, hits, strict=True) if hit is None]
    return pending, len(todo) - len(pending)


# --- export ---------------------------------------------------------------------


def _inline(p: Any) -> str:
    """The masked segment as XLIFF inline markup, placeholders locked."""
    out = []
    for token in p.with_terms.tokens:
        if isinstance(token, Text):
            out.append(escape(token.value))
        elif isinstance(token, Open):
            out.append(f'<g id="t{token.id}">')
        elif isinstance(token, Close):
            out.append("</g>")
        elif isinstance(token, Void):
            out.append(f'<x id="v{token.id}" ctype="lb" equiv-text="[line break or image]"/>')
        elif token.kind == TERM_KIND:
            term = p.terms[token.id]
            shown = quoteattr(f"{term.source} = {term.target}")
            out.append(f'<x id="T{token.id}" ctype="x-dzweb-term" equiv-text={shown}/>')
        else:
            value = quoteattr(p.entities[token.id].value)
            kind = token.kind.lower()
            out.append(f'<x id="e{token.id}" ctype="x-dzweb-{kind}" equiv-text={value}/>')
    return "".join(out)


def export_xliff(units: list[Unit], site_id: str) -> str:
    rows = []
    for u in units:
        pages = ", ".join(u.paths) or "(no path)"
        rows.append(
            f'      <trans-unit id="{u.key}" approved="no">\n'
            f"        <source>{_inline(u.prepared)}</source>\n"
            f'        <target state="needs-translation"></target>\n'
            f'        <note from="dzweb-pages">{escape(pages)}</note>\n'
            f'        <note from="dzweb-source">{escape(u.source)}</note>\n'
            f"      </trans-unit>"
        )
    body = "\n".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<xliff version="1.2" xmlns="{XLIFF_NS}">\n'
        f'  <file original="dzweb:{site_id}:tier1" source-language="en" '
        f'target-language="dz" datatype="html">\n'
        "    <body>\n"
        f"{body}\n"
        "    </body>\n"
        "  </file>\n"
        "</xliff>\n"
    )


# --- import ---------------------------------------------------------------------


@dataclass
class ImportReport:
    approved: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (unit id, reason)
    not_approved: int = 0

    def summary(self) -> str:
        return (
            f"approved {len(self.approved)}, rejected {len(self.rejected)}, "
            f"not yet approved by the reviewer {self.not_approved}"
        )


class _RowError(ValueError):
    pass


def _q(tag: str) -> str:
    return f"{{{XLIFF_NS}}}{tag}"


def _tokens(element: ET.Element, source: Segment) -> list[Token]:
    """Inline XLIFF back to segment tokens, by the ids the source used."""
    by_id: dict[str, Token] = {}
    for token in source.tokens:
        if isinstance(token, Open):
            by_id[f"t{token.id}"] = token
        elif isinstance(token, Void):
            by_id[f"v{token.id}"] = token
        elif isinstance(token, Entity):
            by_id[f"{'T' if token.kind == TERM_KIND else 'e'}{token.id}"] = token
    out: list[Token] = []

    def walk(node: ET.Element) -> None:
        if node.text:
            out.append(Text(node.text))
        for child in node:
            name = child.tag.split("}")[-1]
            pid = child.get("id", "")
            if name == "g":
                opened = by_id.get(pid)
                if not isinstance(opened, Open):
                    raise _RowError(f"unknown tag {pid!r}")
                out.append(opened)
                walk(child)
                out.append(Close(opened.id))
            elif name == "x":
                token = by_id.get(pid)
                if token is None or isinstance(token, Open):
                    raise _RowError(f"unknown placeholder {pid!r}")
                out.append(token)
            elif name == "mrk":  # a CAT tool's own markup: keep its text
                walk(child)
            else:
                raise _RowError(f"unexpected element <{name}>")
            if child.tail:
                out.append(Text(child.tail))

    walk(element)
    merged: list[Token] = []
    for token in out:
        if isinstance(token, Text):
            value = strip_render_artefacts(token.value)  # FR-160: never stored
            if merged and isinstance(merged[-1], Text):
                merged[-1] = Text(merged[-1].value + value)
            elif value:
                merged.append(Text(value))
        else:
            merged.append(token)
    return merged


def parse_xliff(raw: bytes) -> list[ET.Element]:
    """The trans-units of an XLIFF file. Refuses a DTD: no entity expansion from outside."""
    head = raw[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in raw.upper():
        raise ValueError("XLIFF with a DTD or entity declarations is refused")
    root = ET.fromstring(raw)  # noqa: S314 - DTDs refused above; no external entities in ET
    return list(root.iter(_q("trans-unit")))


def _approved(unit: ET.Element, target: ET.Element | None) -> bool:
    if unit.get("approved") == "yes":
        return True
    return target is not None and target.get("state") in APPROVED_STATES


def import_xliff(
    service: TranslateService,
    site: Site,
    raw: bytes,
    reviewer: str,
    *,
    dry_run: bool = False,
) -> ImportReport:
    report = ImportReport()
    batch = "seed-" + hashlib.sha256(raw).hexdigest()[:12]
    for unit in parse_xliff(raw):
        uid = unit.get("id", "")
        target = unit.find(_q("target"))
        if not _approved(unit, target):
            report.not_approved += 1
            continue
        try:
            source_note = next(
                (n.text or "" for n in unit.findall(_q("note")) if n.get("from") == "dzweb-source"),
                None,
            )
            if source_note is None:
                raise _RowError("no dzweb-source note: not a file this tool exported")
            p = service.prepare(site, SegmentIn(uid, source_note))
            if p.keys.segment_key != uid:
                raise _RowError("source changed since export: export again")
            if target is None:
                raise _RowError("no target")
            translated = Segment(tuple(_tokens(target, p.with_terms)))
            text = "".join(t.value for t in translated.tokens if isinstance(t, Text))
            if not has_dzongkha(text):
                raise _RowError("not translated: no Dzongkha in the target")
            validate_output(translated, p.with_terms)
        except _RowError as err:
            report.rejected.append((uid, str(err)))
            continue
        except SegmentError as err:
            reason = getattr(err, "reason", None) or err.cause
            report.rejected.append((uid, f"placeholders: {reason}"))
            continue
        if not dry_run:
            service.store.approve(
                keys=p.keys,
                masked_source=p.with_terms.to_wire(),
                masked_target=translated.to_wire(),
                author=reviewer,
                term_ids=sorted({t.term_id for t in p.terms.values()}),
                site_id=site.site_id,
                action=Action.SEED_IMPORT,
                detail={"batch": batch},
            )
        report.approved.append(uid)
    return report


# --- coverage -------------------------------------------------------------------


@dataclass
class PageCoverage:
    path: str
    tier1: int = 0
    approved: int = 0
    english: list[str] = field(default_factory=list)  # the Tier 1 text still English


def coverage(
    service: TranslateService, site: Site, segments: list[dict[str, Any]]
) -> list[PageCoverage]:
    """Per page: Tier 1 segments, how many are approved, and the ones still English."""
    pages: dict[str, PageCoverage] = {}
    found: list[tuple[PageCoverage, str, Any]] = []
    for n, raw in enumerate(segments):
        path = str(raw.get("path") or "(no path)")
        seg_in = SegmentIn(f"s{n}", str(raw["text"]), raw.get("tier"), raw.get("selector_tier"))
        try:
            p = service.prepare(site, seg_in, raw.get("path"))
        except SegmentError:
            continue
        if p.tier != 1:
            continue
        page = pages.setdefault(path, PageCoverage(path))
        page.tier1 += 1
        found.append((page, seg_in.text, p))
    hits = service.store.lookup([LookupItem(p.keys, 1) for _, _, p in found])
    for (page, text, _), hit in zip(found, hits, strict=True):
        if hit is not None:
            page.approved += 1
        else:
            page.english.append(text)
    return sorted(pages.values(), key=lambda c: c.path)


def render_coverage(pages: list[PageCoverage]) -> str:
    lines = [f"{'page':40} {'Tier 1':>7} {'approved':>9} {'English':>8}"]
    for c in pages:
        lines.append(f"{c.path[:40]:40} {c.tier1:7} {c.approved:9} {len(c.english):8}")
    total = sum(c.tier1 for c in pages)
    done = sum(c.approved for c in pages)
    lines.append(f"{'TOTAL':40} {total:7} {done:9} {total - done:8}")
    return "\n".join(lines)


# --- command line ---------------------------------------------------------------


def load_segments(path: Path) -> list[dict[str, Any]]:
    """scripts/export-segments.mjs output (pages, each with its path), or a flat list."""
    data = json.loads(path.read_text("utf-8"))
    if isinstance(data, dict):
        return [{**s, "path": page.get("path")} for page in data["pages"] for s in page["segments"]]
    return list(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.ops.seed",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="Tier 1 segments to XLIFF for reviewers")
    exp.add_argument("segments", type=Path)
    exp.add_argument("-o", "--out", type=Path, required=True)
    imp = sub.add_parser("import", help="approved XLIFF units into the translation memory")
    imp.add_argument("xliff", type=Path)
    imp.add_argument("--reviewer", required=True, help="an identifier, e.g. dcdd:reviewer-1")
    imp.add_argument("--dry-run", action="store_true", help="check every row, write nothing")
    cov = sub.add_parser("coverage", help="Tier 1 approved / still English, per page")
    cov.add_argument("segments", type=Path)
    for p in (exp, imp, cov):
        p.add_argument("--site", required=True)
    args = parser.parse_args(argv)

    from orchestrator.wiring import Settings, build

    c = build(Settings.from_env())
    site = c.sites.get(args.site)
    if site is None:
        print(f"unknown site {args.site!r}", file=sys.stderr)
        return 2
    if args.command == "export":
        units, done = tier1_units(c.service, site, load_segments(args.segments))
        args.out.write_text(export_xliff(units, site.site_id), encoding="utf-8")
        print(f"{len(units)} Tier 1 segments to translate -> {args.out}; {done} already approved")
        return 0
    if args.command == "import":
        report = import_xliff(
            c.service, site, args.xliff.read_bytes(), args.reviewer, dry_run=args.dry_run
        )
        for uid, reason in report.rejected:
            print(f"REJECTED {uid[:16]}: {reason}")
        print(("[dry run] " if args.dry_run else "") + report.summary())
        return 1 if report.rejected else 0
    pages = coverage(c.service, site, load_segments(args.segments))
    print(render_coverage(pages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
