"""Entity masking, restoration and leak scan (backlog S1.3).

Requirements: FR-140, FR-141, FR-142, FR-143 · Gate: NFR-201. Guarded directory (docs/CLAUDE.md).

    client segment ──► mask() ──► masked segment + EntityMap ──► model
    "Pay Nu. 1,500 by 30 June 2026"                             │
      ─► "Pay ⟦CUR:1⟧ by ⟦DATE:2⟧"   {1: CUR "Nu. 1,500",        │
                                       2: DATE "30 June 2026"}   ▼
    decoded model output ──► restore(output, masked, map) ──► restored segment
                                 │  1. exact multiset: every entity id exactly once,
                                 │     no unknown ids, kinds unchanged      (FR-141)
                                 │  2. leak scan on model text: no numeric
                                 │     character, email or URL             (FR-142)
                                 │  2b. no two entities newly touching, which
                                 │     would render as one different number (FR-140)
                                 │  3. insert the SOURCE bytes for each id  (FR-140)
                                 └─ any failure ► EntityCheckError → source text

The EntityMap belongs to one request and is never persisted (FR-143): storage
holds masked text, and each response is restored from its own map.

PATTERNS is data: any change alters the derived pipeline version (FR-154).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from orchestrator.pipeline.segment import (
    Entity,
    Marker,
    Segment,
    SegmentError,
    Text,
    Token,
    Void,
)

AMOUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)

#: Longest-context first; left to right; non-overlapping; first pattern wins.
#: The final NUM pattern is a catch-all: no digit run reaches the model unmasked.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("URL", re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)]")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("CID", re.compile(r"\b\d{11}\b")),
    (
        "REF",
        re.compile(r"\b[A-Za-z]{2,}(?:[/-][A-Za-z0-9]+)*[/-]\d[A-Za-z0-9]*(?:[/-][A-Za-z0-9]+)*\b"),
    ),
    ("CUR", re.compile(r"(?:\bNu\.?|\bBTN|\bNgultrum)\s?" + AMOUNT)),
    (
        "DATE",
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
            r"|\b\d{1,2}(?:st|nd|rd|th)?\s+" + MONTH + r"\.?,?\s+\d{4}\b"
            r"|\b" + MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
        ),
    ),
    ("PCT", re.compile(r"\b\d+(?:\.\d+)?\s?%")),
    ("NUM", re.compile(r"\d+(?:[.,]\d+)*")),
)

#: Kinds this module creates. Glossary terms ("T") are restored by glossary.py.
ENTITY_KINDS = frozenset(kind for kind, _ in PATTERNS)

_LEAK_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LEAK_URL = re.compile(r"https?://|\bwww\.", re.IGNORECASE)


class EntityCheckError(SegmentError):
    """Restoration refused. The API maps this to ``entity_check_failed`` + source text."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__("entity_check_failed", f"{reason} {detail}".strip())
        self.reason = reason  # stable sub-cause for metrics


@dataclass(frozen=True)
class MaskedEntity:
    kind: str
    value: str  # exact source bytes


EntityMap = dict[int, MaskedEntity]


def _spans(text: str) -> list[tuple[int, int, str]]:
    taken: list[tuple[int, int, str]] = []
    for kind, pattern in PATTERNS:
        for m in pattern.finditer(text):
            if m.start() == m.end():
                continue
            if any(m.start() < b and a < m.end() for a, b, _ in taken):
                continue
            taken.append((m.start(), m.end(), kind))
    return sorted(taken)


def find_entities(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, kind)`` spans the masker would protect, in order. Used by recall tooling."""
    return _spans(text)


def mask(segment: Segment) -> tuple[Segment, EntityMap]:
    """Replace every entity in the segment's text with an ``⟦KIND:n⟧`` token (FR-140).

    Entity ids continue after the segment's highest marker id, so ids stay unique.
    Masking works inside each text run; a value split by inline markup is masked
    piecewise, so its digits are still protected.
    """
    if any(isinstance(m, Entity) for m in segment.structure()):
        raise SegmentError("entity_not_allowed", "segment is already masked")
    next_id = max((m.id for m in segment.structure()), default=0) + 1
    entities: EntityMap = {}
    tokens: list[Token] = []
    for token in segment.tokens:
        if not isinstance(token, Text):
            tokens.append(token)
            continue
        pos = 0
        for start, end, kind in _spans(token.value):
            if start > pos:
                tokens.append(Text(token.value[pos:start]))
            entities[next_id] = MaskedEntity(kind, token.value[start:end])
            tokens.append(Entity(kind, next_id))
            next_id += 1
            pos = end
        if pos < len(token.value):
            tokens.append(Text(token.value[pos:]))
    return Segment(tuple(tokens)), entities


def leak_reason(text: str) -> str | None:
    """Why model text may not be served, or None (FR-142).

    Any character with a numeric value counts: ASCII and Tibetan digits and
    half-digits, other scripts' numerals, fractions. Entities are masked before
    the model, so model-authored text should contain no numbers at all.
    """
    for ch in text:
        if ch.isdigit() or unicodedata.numeric(ch, None) is not None:
            return f"numeral U+{ord(ch):04X}"
    if _LEAK_EMAIL.search(text):
        return "email"
    if _LEAK_URL.search(text):
        return "url"
    return None


def _entity_ids(markers: tuple[Marker, ...]) -> Counter[tuple[str, int]]:
    return Counter((m.kind, m.id) for m in markers if isinstance(m, Entity))


def _own_entities(segment: Segment) -> Counter[tuple[str, int]]:
    ids = _entity_ids(segment.structure())
    return Counter({k: v for k, v in ids.items() if k[0] in ENTITY_KINDS})


def _touching_pairs(segment: Segment) -> set[tuple[int, int]]:
    """Pairs of entity ids that render with nothing visible between them.

    Inline Open/Close markers are invisible, so ``⟦CUR:1⟧⟦/2⟧⟦NUM:3⟧`` renders
    ``1500`` and ``90000001`` as ``150090000001``: a different number. Text and
    void elements (``<br>``, ``<img>``) separate entities.
    """
    pairs: set[tuple[int, int]] = set()
    prev: int | None = None
    for token in segment.tokens:
        if isinstance(token, Entity) and token.kind in ENTITY_KINDS:
            if prev is not None:
                pairs.add((prev, token.id))
            prev = token.id
        elif isinstance(token, (Text, Void)):
            prev = None
    return pairs


def restore(output: Segment, masked_source: Segment, entities: EntityMap) -> Segment:
    """Restore entities into decoded model output, or raise EntityCheckError (FR-140..142).

    Only kinds created by :func:`mask` are restored here; other entity tokens
    (glossary terms) pass through for their own stage.
    """
    wanted = _own_entities(masked_source)
    got = _own_entities(output)
    for (kind, eid), count in got.items():
        if (kind, eid) not in wanted:
            known = eid in entities
            raise EntityCheckError("kind_mismatch" if known else "unknown", f"{kind}:{eid}")
        if count > 1:
            raise EntityCheckError("duplicate", f"{kind}:{eid}")
    missing = wanted - got
    if missing:
        kind, eid = next(iter(missing))
        raise EntityCheckError("missing", f"{kind}:{eid}")

    for token in output.tokens:
        if isinstance(token, Text) and (why := leak_reason(token.value)):
            raise EntityCheckError("leak", why)

    new_touching = _touching_pairs(output) - _touching_pairs(masked_source)
    if new_touching:
        a, b = sorted(new_touching)[0]
        raise EntityCheckError("adjacent", f"{a}+{b} would render as one value")

    tokens: list[Token] = []
    for token in output.tokens:
        if isinstance(token, Entity) and token.kind in ENTITY_KINDS:
            source = entities[token.id]
            if source.kind != token.kind:  # pragma: no cover - guarded by the multiset check
                raise EntityCheckError("kind_mismatch", f"{token.kind}:{token.id}")
            token = Text(source.value)
        if isinstance(token, Text) and tokens and isinstance(tokens[-1], Text):
            tokens[-1] = Text(tokens[-1].value + token.value)
        else:
            tokens.append(token)
    return Segment(tuple(tokens))
