"""Glossary substitution from a versioned termbase (backlog S1.6).

Requirements: FR-400, FR-401, FR-402, FR-153. Guarded directory (docs/CLAUDE.md).

Runs AFTER entity masking (spec §2.1), on the masked segment's text runs:

    masked:   "Apply to the Department of Immigration by ⟦DATE:2⟧"
    substitute()
    to model: "Apply to the ⟦T:3⟧ by ⟦DATE:2⟧"        term map {3: T-0001 v2}
    model:    "... ⟦T:3⟧ ... ⟦DATE:2⟧ ..."
    restore_terms()  each T id exactly once, else GlossaryCheckError
    served:   "... <approved Dzongkha target> ... <date>"

The model never sees the English term, so it cannot produce a variant (FR-401).
The glossary fingerprint (FR-153) identifies exactly which term versions a
segment depends on, so a term change invalidates only those segments.

Matching: longest term first, left to right, non-overlapping, whole words,
case-sensitive only where the entry says so. Terms are matched within one text
run: a term split by inline markup is not matched and shows up as lower
compliance rather than as a wrong substitution.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.locale.dz import RENDER_ARTEFACTS
from orchestrator.pipeline.segment import CLOSE, OPEN, Entity, Segment, SegmentError, Text, Token

TERM_KIND = "T"


class TermbaseError(ValueError):
    """The termbase file is invalid; it must not be loaded."""


class GlossaryCheckError(SegmentError):
    """A term placeholder is missing, duplicated or unknown. Block falls back to source."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__("glossary_term_missing", f"{reason} {detail}".strip())
        self.reason = reason


@dataclass(frozen=True)
class Term:
    term_id: str
    term_version: int
    source: str
    target: str  # DCDD-approved Dzongkha
    case_sensitive: bool


@dataclass(frozen=True)
class Termbase:
    version: str
    terms: tuple[Term, ...]
    _by_first: dict[str, tuple[Term, ...]] = field(default_factory=dict, repr=False, compare=False)

    @staticmethod
    def from_dict(data: dict[str, object]) -> Termbase:
        version = data.get("termbase_version")
        if not isinstance(version, str) or not version.strip():
            raise TermbaseError("termbase_version must be a non-empty string")
        raw_terms = data.get("terms")
        if not isinstance(raw_terms, list):
            raise TermbaseError("terms must be a list")
        terms: list[Term] = []
        for i, raw in enumerate(raw_terms):
            if not isinstance(raw, dict):
                raise TermbaseError(f"terms[{i}] must be an object")
            if raw.get("status", "active") == "retired":
                continue
            term = _parse_term(raw, i)
            for other in terms:
                if other.term_id == term.term_id:
                    raise TermbaseError(f"duplicate term id {term.term_id}")
                if _ambiguous(other, term):
                    raise TermbaseError(f"{term.term_id} duplicates the source of {other.term_id}")
            terms.append(term)
        return Termbase(version.strip(), tuple(terms), _index(terms))

    @staticmethod
    def load(path: Path) -> Termbase:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise TermbaseError(f"cannot read termbase {path}: {err}") from err
        if not isinstance(data, dict):
            raise TermbaseError("termbase root must be an object")
        return Termbase.from_dict(data)

    def candidates(self, first_char: str) -> tuple[Term, ...]:
        return self._by_first.get(first_char.lower(), ())


def _parse_term(raw: dict[str, object], i: int) -> Term:
    term_id, version = raw.get("id"), raw.get("version")
    source, target = raw.get("source"), raw.get("target")
    case_sensitive = raw.get("case_sensitive", False)
    if not isinstance(term_id, str) or not term_id:
        raise TermbaseError(f"terms[{i}].id must be a non-empty string")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise TermbaseError(f"{term_id}: version must be a positive integer")
    if not isinstance(source, str) or not source.strip() or source != source.strip():
        raise TermbaseError(f"{term_id}: source must be non-empty with no outer whitespace")
    if not isinstance(target, str) or not target.strip():
        raise TermbaseError(f"{term_id}: target must be non-empty")
    if not isinstance(case_sensitive, bool):
        raise TermbaseError(f"{term_id}: case_sensitive must be true or false")
    if any(ch.isdigit() or unicodedata.numeric(ch, None) is not None for ch in source):
        # Entities are masked before glossary matching, so this term could never match.
        raise TermbaseError(f"{term_id}: source contains a numeral and can never match")
    for text in (source, target):
        if OPEN in text or CLOSE in text:
            raise TermbaseError(f"{term_id}: placeholder delimiters are not allowed")
    if any(ch in RENDER_ARTEFACTS for ch in target):
        raise TermbaseError(f"{term_id}: target contains zero-width characters (FR-160)")
    return Term(term_id, version, source, target, case_sensitive)


def _ambiguous(a: Term, b: Term) -> bool:
    """Two entries could match the same text: same source ignoring case, unless both
    are case-sensitive and differ in case (e.g. "UN" vs "Un")."""
    if a.source.lower() != b.source.lower():
        return False
    return not (a.case_sensitive and b.case_sensitive) or a.source == b.source


def _index(terms: Iterable[Term]) -> dict[str, tuple[Term, ...]]:
    by_first: dict[str, list[Term]] = {}
    for t in terms:
        by_first.setdefault(t.source[0].lower(), []).append(t)
    return {k: tuple(sorted(v, key=lambda t: -len(t.source))) for k, v in by_first.items()}


TermMap = dict[int, Term]


def _is_word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _matches_at(text: str, i: int, term: Term) -> bool:
    end = i + len(term.source)
    if end > len(text):
        return False
    piece = text[i:end]
    if term.case_sensitive:
        if piece != term.source:
            return False
    elif piece.lower() != term.source.lower():
        return False
    if _is_word(term.source[0]) and i > 0 and _is_word(text[i - 1]):
        return False
    return not (_is_word(term.source[-1]) and end < len(text) and _is_word(text[end]))


def find_terms(text: str, termbase: Termbase) -> list[tuple[int, int, Term]]:
    """Longest-match-first, left-to-right, non-overlapping whole-word matches."""
    found: list[tuple[int, int, Term]] = []
    i = 0
    while i < len(text):
        for term in termbase.candidates(text[i]):
            if _matches_at(text, i, term):
                found.append((i, i + len(term.source), term))
                i += len(term.source)
                break
        else:
            i += 1
    return found


def substitute(segment: Segment, termbase: Termbase) -> tuple[Segment, TermMap]:
    """Replace glossary terms in text runs with ``⟦T:n⟧`` placeholders (FR-400, FR-401)."""
    next_id = max((m.id for m in segment.structure()), default=0) + 1
    terms: TermMap = {}
    tokens: list[Token] = []
    for token in segment.tokens:
        if not isinstance(token, Text):
            tokens.append(token)
            continue
        pos = 0
        for start, end, term in find_terms(token.value, termbase):
            if start > pos:
                tokens.append(Text(token.value[pos:start]))
            terms[next_id] = term
            tokens.append(Entity(TERM_KIND, next_id))
            next_id += 1
            pos = end
        if pos < len(token.value):
            tokens.append(Text(token.value[pos:]))
    return Segment(tuple(tokens)), terms


def fingerprint(terms: Iterable[Term]) -> str:
    """Glossary fingerprint (FR-153): sha256 over the sorted distinct (term_id, version)."""
    pairs = sorted({(t.term_id, t.term_version) for t in terms})
    return hashlib.sha256("|".join(f"{i}:{v}" for i, v in pairs).encode("utf-8")).hexdigest()


def _term_ids(segment: Segment) -> Counter[int]:
    markers = segment.structure()
    return Counter(m.id for m in markers if isinstance(m, Entity) and m.kind == TERM_KIND)


def restore_terms(output: Segment, source: Segment, terms: TermMap) -> Segment:
    """Replace each ``⟦T:n⟧`` with its approved target, exactly once each (FR-401)."""
    wanted, got = _term_ids(source), _term_ids(output)
    for tid, count in got.items():
        if tid not in wanted:
            raise GlossaryCheckError("unknown", f"T:{tid}")
        if count > 1:
            raise GlossaryCheckError("duplicate", f"T:{tid}")
    missing = wanted - got
    if missing:
        raise GlossaryCheckError("missing", f"T:{next(iter(missing))}")
    out: list[Token] = []
    for token in output.tokens:
        if isinstance(token, Entity) and token.kind == TERM_KIND:
            token = Text(terms[token.id].target)
        if isinstance(token, Text) and out and isinstance(out[-1], Text):
            out[-1] = Text(out[-1].value + token.value)
        else:
            out.append(token)
    return Segment(tuple(out))


@dataclass
class ComplianceStats:
    """Glossary compliance per batch (FR-402): restored terms / terms found."""

    found: int = 0
    restored: int = 0

    def record(self, terms_found: int, restored: bool) -> None:
        self.found += terms_found
        if restored:
            self.restored += terms_found

    @property
    def rate(self) -> float:
        return 1.0 if self.found == 0 else self.restored / self.found
