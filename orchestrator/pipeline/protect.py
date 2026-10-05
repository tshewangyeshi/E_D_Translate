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
    # With a scheme, or a bare www. host as the pilot portal writes them
    # ("www.judiciary.gov.bt"); the leak scan treats both as addresses.
    (
        "URL",
        re.compile(r"(?:https?://|(?<![\w.@])www\.)[^\s<>\"']+[^\s<>\"'.,;:!?)]", re.IGNORECASE),
    ),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("CID", re.compile(r"\b\d{11}\b")),
    # Phone numbers: +975 with digits and spaces, or a standalone 7-8 digit run
    # (Bhutan landlines and mobiles). Never sent to the model, whatever the
    # numbers setting: a number written in Tibetan digits cannot be dialled.
    ("PHONE", re.compile(r"\+975[\d\s-]{6,12}\d|\b\d{7,8}\b")),
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

#: Kinds the model may translate when the service is set to (DZWEB_NUMBERS=model,
#: FR-144): their text goes to the model and comes back in Dzongkha, Tibetan
#: digits included, and every numeric value is checked afterwards. Identifiers
#: -- citizen IDs, phone numbers, references, emails, URLs -- are never in it.
TRANSLATABLE_KINDS = frozenset({"CUR", "DATE", "PCT", "NUM"})

#: Digits after a Latin letter -- "G2C", "B2B" -- are part of a name, not a
#: value, and the model may rightly translate the name by its meaning. Digits
#: before one -- "45mm" -- are a value with its unit.
_NUMBER_RUN = re.compile(r"(?<![A-Za-z\d])\d+(?:[.,]\d+)*")
#: "9:00" and "9" are the same time; the model often writes just the hour.
_ON_THE_HOUR = re.compile(r"(?<![\d:])(\d{1,2}):00(?![\d:])")

#: A month named in the source may come back as its number: a Dzongkha date
#: can say "the sixth month" for June.
_MONTH_NUMBERS = tuple(
    (re.compile(rf"\b{name}", re.IGNORECASE), str(n))
    for n, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
)


def _month_numbers(text: str) -> Counter[str]:
    """Numbers of the months named inside dates; "you may apply" is not a month."""
    dates = [text[a:b] for a, b, kind in _spans(text) if kind == "DATE"]
    return Counter(n for date in dates for pattern, n in _MONTH_NUMBERS if pattern.search(date))


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


def mask(segment: Segment, translate: frozenset[str] = frozenset()) -> tuple[Segment, EntityMap]:
    """Replace every entity in the segment's text with an ``⟦KIND:n⟧`` token (FR-140).

    Entity ids continue after the segment's highest marker id, so ids stay unique.
    Masking works inside each text run; a value split by inline markup is masked
    piecewise, so its digits are still protected.

    Kinds in ``translate`` (a subset of :data:`TRANSLATABLE_KINDS`) are left in
    the text for the model; ``restore`` then checks their values instead of
    their bytes (FR-144).
    """
    if not translate <= TRANSLATABLE_KINDS:
        raise ValueError(f"only {sorted(TRANSLATABLE_KINDS)} may be translated")
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
            if kind in translate:
                continue  # left in the text: the model translates it
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
    half-digits, other scripts' numerals, fractions. This is the rule when
    every number was masked: model-authored text should then contain none.
    """
    for ch in text:
        if ch.isdigit() or unicodedata.numeric(ch, None) is not None:
            return f"numeral U+{ord(ch):04X}"
    return _address_leak(text)


def _address_leak(text: str) -> str | None:
    if _LEAK_EMAIL.search(text):
        return "email"
    if _LEAK_URL.search(text):
        return "url"
    return None


def number_values(text: str) -> Counter[str]:
    """The numbers in text by value, whatever the script (FR-144).

    ``1,500`` and the same digits in Tibetan script are both ``1500``; ``06``
    and ``6`` are the same; ``12.5`` stays ``12.5`` in any script; the 2 of
    ``G2C`` is part of a name, not a number; ``9:00`` is ``9``. A numeric character that is not a
    decimal digit -- a fraction, a Tibetan half-digit -- raises ValueError: its
    value cannot be compared, so it is never accepted.
    """
    plain = []
    for ch in text:
        if ch.isdecimal():
            plain.append(str(unicodedata.decimal(ch)))
        elif unicodedata.numeric(ch, None) is not None or ch.isdigit():
            raise ValueError(f"numeral U+{ord(ch):04X}")
        else:
            plain.append(ch)
    values: Counter[str] = Counter()
    for m in _NUMBER_RUN.finditer(_ON_THE_HOUR.sub(r"\1", "".join(plain))):
        run = re.sub(r",(?=\d{3}(?!\d))", "", m.group(0))  # thousands separators
        whole, dot, fraction = run.partition(".")
        values[(whole.lstrip("0") or "0") + (dot + fraction if fraction else "")] += 1
    return values


def _number_leak(output: Segment, masked_source: Segment) -> str | None:
    """Model text may carry exactly the numbers the source let it see, by value (FR-144).

    With everything masked the source shows none, so any numeral is a leak, as
    before. With amounts and dates left to the model, a number may change
    script but never value: lost, changed or invented numbers all fail.
    """
    source_text = "".join(t.value for t in masked_source.tokens if isinstance(t, Text))
    output_text = "".join(t.value for t in output.tokens if isinstance(t, Text))
    allowed = number_values(source_text)
    if not allowed:
        return leak_reason(output_text)
    try:
        got = number_values(output_text)
    except ValueError as err:
        return str(err)
    lost = allowed - got
    extra = (got - allowed) - _month_numbers(source_text)  # June may come back as 6
    if lost or extra:
        return f"numbers changed: lost {sorted(lost.elements())}, new {sorted(extra.elements())}"
    return _address_leak(output_text)


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

    if why := _number_leak(output, masked_source):
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
