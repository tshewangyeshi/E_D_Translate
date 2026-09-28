"""Segment grammar: parse, validate, serialise and split placeholder segments.

Requirements: FR-120, FR-121, FR-122, FR-124 (backlog S1.2). Guarded directory (docs/CLAUDE.md).

Wire format (spec §2.3), produced by adapters and consumed by the pipeline::

    Click ⟦1⟧here⟦/1⟧ to apply.     inline pair (id 1)
    Line one⟦v2/⟧line two           void marker (id 2)
    Pay ⟦CUR:3⟧ today               entity token (id 3): added by protect.py only
    Use ⟦⟦ and ⟧⟧ literally         escaped literal delimiters

    parse(wire) ──► Segment(tokens) ──► to_wire()     byte-identical round trip
                           │
                           ├─► slots()        text between markers (widget write-back units)
                           ├─► structure()    marker sequence, compared by the validator (FR-122)
                           └─► ModelFormat    wire ⇄ model token format (chosen in Sprint 0, S0.1)

Client input is parsed with ``allow_entities=False``: a browser must never be
able to forge an entity token, or restoration could inject values into output.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

OPEN = "⟦"
CLOSE = "⟧"

_OPEN_RE = re.compile(r"(\d+)")
_CLOSE_RE = re.compile(r"/(\d+)")
_VOID_RE = re.compile(r"v(\d+)/")
_ENTITY_RE = re.compile(r"([A-Z]+):(\d+)")


class SegmentError(ValueError):
    """Invalid segment. ``cause`` is a stable machine-readable reason for metrics."""

    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(f"{cause}: {detail}" if detail else cause)
        self.cause = cause


@dataclass(frozen=True)
class Text:
    value: str  # unescaped


@dataclass(frozen=True)
class Open:
    id: int


@dataclass(frozen=True)
class Close:
    id: int


@dataclass(frozen=True)
class Void:
    id: int


@dataclass(frozen=True)
class Entity:
    kind: str  # e.g. "NUM", "CUR" (protect.py) or "T" (glossary term)
    id: int


Marker = Open | Close | Void | Entity
Token = Text | Marker


def escape(text: str) -> str:
    return text.replace(OPEN, OPEN + OPEN).replace(CLOSE, CLOSE + CLOSE)


def marker_wire(marker: Marker) -> str:
    if isinstance(marker, Open):
        return f"{OPEN}{marker.id}{CLOSE}"
    if isinstance(marker, Close):
        return f"{OPEN}/{marker.id}{CLOSE}"
    if isinstance(marker, Void):
        return f"{OPEN}v{marker.id}/{CLOSE}"
    return f"{OPEN}{marker.kind}:{marker.id}{CLOSE}"


@dataclass(frozen=True)
class Segment:
    tokens: tuple[Token, ...]

    def to_wire(self) -> str:
        return "".join(
            escape(t.value) if isinstance(t, Text) else marker_wire(t) for t in self.tokens
        )

    def structure(self) -> tuple[Marker, ...]:
        """Ordered markers. Two segments are structurally compatible iff these are equal."""
        return tuple(t for t in self.tokens if not isinstance(t, Text))

    def slots(self) -> list[str]:
        """Text between markers: ``len(slots()) == len(structure()) + 1``."""
        out = [""]
        for t in self.tokens:
            if isinstance(t, Text):
                out[-1] += t.value
            else:
                out.append("")
        return out

    def plain_text(self) -> str:
        return "".join(t.value for t in self.tokens if isinstance(t, Text))


def _read_marker(body: str, allow_entities: bool) -> Marker:
    if m := _OPEN_RE.fullmatch(body):
        return Open(int(m.group(1)))
    if m := _CLOSE_RE.fullmatch(body):
        return Close(int(m.group(1)))
    if m := _VOID_RE.fullmatch(body):
        return Void(int(m.group(1)))
    if m := _ENTITY_RE.fullmatch(body):
        if not allow_entities:
            raise SegmentError("entity_not_allowed", body)
        return Entity(m.group(1), int(m.group(2)))
    raise SegmentError("malformed_marker", body)


def tokenize(wire: str, *, allow_entities: bool = False) -> tuple[Token, ...]:
    """Split wire text into tokens. Structural validity is checked by :func:`validate`."""
    tokens: list[Token] = []
    buf: list[str] = []
    i, n = 0, len(wire)

    def flush() -> None:
        if buf:
            tokens.append(Text("".join(buf)))
            buf.clear()

    while i < n:
        ch = wire[i]
        if ch == OPEN:
            if i + 1 < n and wire[i + 1] == OPEN:
                buf.append(OPEN)
                i += 2
                continue
            end = wire.find(CLOSE, i + 1)
            nested = wire.find(OPEN, i + 1)
            if end == -1 or (nested != -1 and nested < end):
                raise SegmentError("unterminated_marker", wire[i : i + 12])
            flush()
            tokens.append(_read_marker(wire[i + 1 : end], allow_entities))
            i = end + 1
        elif ch == CLOSE:
            if i + 1 < n and wire[i + 1] == CLOSE:
                buf.append(CLOSE)
                i += 2
                continue
            raise SegmentError("unescaped_delimiter", wire[max(0, i - 6) : i + 1])
        else:
            buf.append(ch)
            i += 1
    flush()
    return tuple(tokens)


def validate(tokens: Sequence[Token]) -> None:
    """Pairs balanced and properly nested; every id used exactly once (FR-121, FR-122)."""
    seen: set[int] = set()
    stack: list[int] = []
    for t in tokens:
        if isinstance(t, Text):
            continue
        if isinstance(t, Close):
            if not stack or stack[-1] != t.id:
                raise SegmentError("unbalanced", f"close {t.id}")
            stack.pop()
            continue
        if t.id in seen:
            raise SegmentError("duplicate_id", str(t.id))
        seen.add(t.id)
        if isinstance(t, Open):
            stack.append(t.id)
    if stack:
        raise SegmentError("unbalanced", f"unclosed {stack[-1]}")


def parse(wire: str, *, allow_entities: bool = False) -> Segment:
    """Parse and validate wire text. Raises SegmentError; callers map it to ``tag_fallback``."""
    tokens = tokenize(wire, allow_entities=allow_entities)
    validate(tokens)
    return Segment(tokens)


def _depth_zero_breaks(
    tokens: Sequence[Token], terminators: frozenset[str]
) -> list[tuple[int, int]]:
    """Split points ``(token index, char offset)`` after a terminator at nesting depth 0."""
    points: list[tuple[int, int]] = []
    depth = 0
    for ti, t in enumerate(tokens):
        if isinstance(t, Open):
            depth += 1
        elif isinstance(t, Close):
            depth -= 1
        elif isinstance(t, Text) and depth == 0:
            v = t.value
            for ci, ch in enumerate(v):
                if ch in terminators and (ci + 1 == len(v) or v[ci + 1].isspace()):
                    end = ci + 1
                    while end < len(v) and v[end].isspace():
                        end += 1  # trailing whitespace stays with the left piece
                    points.append((ti, end))
    return points


def _cut(tokens: Sequence[Token], at: tuple[int, int]) -> tuple[list[Token], list[Token]]:
    ti, ci = at
    left: list[Token] = list(tokens[:ti])
    right: list[Token] = list(tokens[ti + 1 :])
    t = tokens[ti]
    if not isinstance(t, Text):  # pragma: no cover - split points are always in Text tokens
        raise SegmentError("internal", "split point outside text")
    if t.value[:ci]:
        left.append(Text(t.value[:ci]))
    if t.value[ci:]:
        right.insert(0, Text(t.value[ci:]))
    return left, right


def _wire_len(tokens: Iterable[Token]) -> int:
    return len(Segment(tuple(tokens)).to_wire())


def split_long(segment: Segment, max_len: int, terminators: frozenset[str]) -> list[Segment]:
    """Split a segment longer than ``max_len`` wire characters (FR-124).

    Splits only after a sentence terminator at nesting depth 0, so no piece ever
    cuts through a placeholder pair. Pieces keep their original marker ids and
    concatenate back to the original wire text exactly. If no terminator lets a
    piece fit, raises ``SegmentError("too_long")`` and the caller falls back.
    ``terminators`` comes from the locale module (NFR-500), never from here.
    """
    pieces: list[Segment] = []
    rest: list[Token] = list(segment.tokens)
    while _wire_len(rest) > max_len:
        best: tuple[int, int] | None = None
        for point in _depth_zero_breaks(rest, terminators):
            left, right = _cut(rest, point)
            if right and _wire_len(left) <= max_len:
                best = point
        if best is None:
            raise SegmentError("too_long", f"{_wire_len(rest)} > {max_len}")
        left, rest = _cut(rest, best)
        pieces.append(Segment(tuple(left)))
    pieces.append(Segment(tuple(rest)))
    return pieces


class ModelFormat(Protocol):
    """Wire ⇄ model token format. The pilot's format is chosen by Sprint 0 (S0.1)."""

    name: str

    def encode(self, segment: Segment) -> str: ...

    def decode(self, text: str, reference: Segment) -> Segment: ...


class WireFormat:
    """Candidate A: send wire markers (``⟦1⟧…⟦/1⟧``) to the model unchanged."""

    name = "wire"

    def encode(self, segment: Segment) -> str:
        return segment.to_wire()

    def decode(self, text: str, reference: Segment) -> Segment:
        allow = any(isinstance(m, Entity) for m in reference.structure())
        return Segment(tokenize(text, allow_entities=allow))


_XML_TAG = re.compile(r"<(/?)([xe])(\d+)(/?)>")
_XML_STRAY = re.compile(r"</?[xe]\d")


class XmlLikeFormat:
    """Candidate B: XML-like tags (``<x1>…</x1>``, ``<x2/>``; entities as ``<e3/>``).

    Literal ``&``, ``<`` and ``>`` in text are entity-escaped so model output
    decodes unambiguously.
    """

    name = "xml"

    @staticmethod
    def _esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _unesc(text: str) -> str:
        return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

    def encode(self, segment: Segment) -> str:
        out: list[str] = []
        for t in segment.tokens:
            if isinstance(t, Text):
                out.append(self._esc(t.value))
            elif isinstance(t, Open):
                out.append(f"<x{t.id}>")
            elif isinstance(t, Close):
                out.append(f"</x{t.id}>")
            elif isinstance(t, Void):
                out.append(f"<x{t.id}/>")
            else:
                out.append(f"<e{t.id}/>")
        return "".join(out)

    def decode(self, text: str, reference: Segment) -> Segment:
        kinds = {m.id: m.kind for m in reference.structure() if isinstance(m, Entity)}
        voids = {m.id for m in reference.structure() if isinstance(m, Void)}
        tokens: list[Token] = []
        pos = 0
        for m in _XML_TAG.finditer(text):
            if m.start() > pos:
                tokens.append(Text(self._unesc(text[pos : m.start()])))
            slash, kind, num, self_closing = m.group(1), m.group(2), int(m.group(3)), m.group(4)
            if kind == "e":
                if slash or not self_closing or num not in kinds:
                    raise SegmentError("malformed_marker", m.group(0))
                tokens.append(Entity(kinds[num], num))
            elif self_closing:
                if slash or num not in voids:
                    raise SegmentError("malformed_marker", m.group(0))
                tokens.append(Void(num))
            else:
                tokens.append(Close(num) if slash else Open(num))
            pos = m.end()
        if pos < len(text):
            tokens.append(Text(self._unesc(text[pos:])))
        for t in tokens:
            if isinstance(t, Text) and _XML_STRAY.search(t.value):
                raise SegmentError("malformed_marker", "stray tag")
        return Segment(tuple(tokens))


MODEL_FORMATS: dict[str, ModelFormat] = {f.name: f for f in (WireFormat(), XmlLikeFormat())}
