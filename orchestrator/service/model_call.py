"""How one segment is sent to the model (S0.1 outcome). Requirements: FR-122, FR-140,
FR-155, FR-210.

    segment ─► no words outside placeholders? ─► not sent: returned as it is
            ─► no inline tags?                ─► one call
            ─► inline tags                    ─► one call per piece of text between
                                                 tags, in parallel; the tags are put
                                                 back by us, in the source's order

Why pieces: measured on 2026-10-05, the GovTech model drops the closing tag of
a link or emphasis more often than not, and a block whose tags do not come
back has to stay English (FR-210). Translating the text between tags
separately means no tag is ever sent, so none can be lost. The price is
fluency: each piece is translated without the rest of the sentence, and the
pieces keep the English order. Decided by the product owner, 2026-10-05.

The whitespace around a piece is kept from the source rather than asked of the
model, so the gaps around a link stay where the page had them. So are the
values after a label -- "Email ID: <e2/>", "Phone: <e2/> / <e3/>" -- and of
values in brackets at the end, "... the G2C system (<e2/>).": only the words
are sent. Seen on the pilot portal, 2026-10-05: with little else to
translate, the model writes such a placeholder in Tibetan letters, and the
value it stood for could not be put back. A value inside a sentence is still
sent, so the model can place it in Dzongkha word order.

Each piece then goes sentence by sentence, the sentences in parallel and
rejoined with the source's own spacing. Seen on the pilot portal, 2026-10-05:
given a long paragraph, the model dropped part of it (a citation's year), and
the whole paragraph had to stay English. A sentence ends at ``.``, ``!`` or
``?`` before a capital letter, but not after an abbreviation (``Nu.``,
``e.g.``) or an initial; a list number ("1.") stays with its sentence.

Raises UpstreamError when a call fails and SegmentError when an answer cannot
be decoded; the caller treats both exactly as it treated a single call.
"""

from __future__ import annotations

import asyncio
import re

from orchestrator.pipeline.segment import (
    Entity,
    ModelFormat,
    Piece,
    Segment,
    Text,
    Token,
    has_words,
    split_at_tags,
)
from orchestrator.upstream.translator import Translator

_LEADING = re.compile(r"^\s+")
_TRAILING = re.compile(r"\s+$")
#: A label ends in a colon or a dash: "Email ID:", "Contact -".
_LABEL_END = re.compile(r"[:：\-–—]\s*$")
#: Between values after a label: spaces and separators, no words.
_SEPARATOR = re.compile(r"[^\w]*")
#: Values in brackets at the end: "... the G2C system (".
_OPEN_BRACKET = re.compile(r"\s*\(\s*$")
#: A sentence end: punctuation, closing quotes or brackets, space, then a capital
#: or a list number;
#: or, for "(2009).The" as the pilot portal writes it, a full stop with no space
#: after a lowercase word or a bracket, before a capitalised word.
_SENTENCE_END = re.compile(
    r"[.!?][\"'’”)\]]*(\s+)(?=[\"'‘“(\[]?[A-Z]|\d{1,2}[.)]\s)|(?<=[a-z)])\.(?=[A-Z][a-z])"
)
_WORD_BEFORE = re.compile(r"(\w+)$")
#: Words a full stop follows without ending the sentence.
_ABBREVIATIONS = frozenset(
    "nu no nos mr mrs ms dr st sr jr vs etc eg ie govt dept ltd co inc approx fig ref "
    "jan feb mar apr jun jul aug sep sept oct nov dec".split()
)


def calls_needed(segment: Segment) -> int:
    """How many model calls translating this segment takes (for the quota, FR-156)."""
    if not has_words(segment):
        return 0
    pieces = split_at_tags(segment)
    return sum(len(_sentences(p)) for p in pieces if isinstance(p, Segment) and has_words(p))


async def translate_segment(translator: Translator, fmt: ModelFormat, segment: Segment) -> Segment:
    """The model's translation of ``segment``, decoded, in the same structure."""
    if not has_words(segment):
        return segment
    pieces = split_at_tags(segment)
    if len(pieces) == 1:
        return await _piece(translator, fmt, segment)
    translated = await asyncio.gather(
        *(_piece(translator, fmt, p) for p in pieces if isinstance(p, Segment))
    )
    texts = iter(translated)
    tokens: list[Token] = []
    for piece in pieces:
        tokens.extend(next(texts).tokens if isinstance(piece, Segment) else (piece,))
    return Segment(tuple(_merge_text(tokens)))


async def _one(translator: Translator, fmt: ModelFormat, segment: Segment) -> Segment:
    return fmt.decode(await translator.translate(fmt.encode(segment), segment), segment)


async def _piece(translator: Translator, fmt: ModelFormat, piece: Piece) -> Segment:
    assert isinstance(piece, Segment)
    if not has_words(piece):
        return piece  # punctuation, a lone entity: nothing to translate
    lead, core, trail = _trim(piece)
    core, values = _label_values(core)
    sentences = split_sentences(core)
    translated = await asyncio.gather(*(_one(translator, fmt, s) for s, _ in sentences))
    body: list[Token] = []
    for (_, gap), sentence in zip(sentences, translated, strict=True):
        body.extend((*sentence.tokens, Text(gap)))
    return Segment(tuple(_merge_text([*lead, *body, *values, *trail])))


def _sentences(piece: Segment) -> list[tuple[Segment, str]]:
    """The sentences ``_piece`` sends for this piece."""
    return split_sentences(_label_values(_trim(piece)[1])[0])


def split_sentences(core: Segment) -> list[tuple[Segment, str]]:
    """The sentences of ``core``, each with the whitespace that followed it.

    Boundaries are found inside text only, never next to a placeholder; a
    stretch with no words (a list number, a closing bracket) joins its
    neighbour, so every sentence returned has words when ``core`` does.
    """
    found: list[tuple[list[Token], str]] = []
    current: list[Token] = []
    for token in core.tokens:
        if not isinstance(token, Text):
            current.append(token)
            continue
        value, start = token.value, 0
        for m in _SENTENCE_END.finditer(value):
            if m.start() > 0 and value[m.start() - 1].isspace():
                continue  # "based on . Net": a stop after a space ends nothing
            word = _WORD_BEFORE.search(value[: m.start()])
            if word and (len(word.group(1)) == 1 or word.group(1).lower() in _ABBREVIATIONS):
                continue  # "Nu. 500", "e.g. This", "A. B. Tshering"
            end = m.start(1) if m.group(1) is not None else m.end()
            current.append(Text(value[start:end]))
            found.append((current, m.group(1) or ""))
            current, start = [], m.end()
        current.append(Text(value[start:]))
    found.append((current, ""))

    merged: list[tuple[list[Token], str]] = []
    for tokens, gap in found:
        if merged and not _has_words(merged[-1][0]):
            before, before_gap = merged.pop()
            tokens = [*before, Text(before_gap), *tokens]
        merged.append((tokens, gap))
    if len(merged) > 1 and not _has_words(merged[-1][0]):
        tokens, gap = merged.pop()
        before, before_gap = merged.pop()
        merged.append(([*before, Text(before_gap), *tokens], gap))
    return [(Segment(tuple(_merge_text(tokens))), gap) for tokens, gap in merged]


def _has_words(tokens: list[Token]) -> bool:
    return has_words(Segment(tuple(_merge_text(tokens))))


def _label_values(core: Segment) -> tuple[Segment, tuple[Token, ...]]:
    """Split "Label: <values>" into the label and the values that follow it."""
    tokens = list(core.tokens)
    cut = len(tokens)
    while cut > 0 and _is_value_or_separator(tokens[cut - 1]):
        cut -= 1
    values = tokens[cut:]
    if not any(isinstance(t, Entity) for t in values) or cut == 0:
        return core, ()
    before = tokens[cut - 1]
    if not isinstance(before, Text):
        return core, ()
    if bracket := _OPEN_BRACKET.search(before.value):
        if not before.value[: bracket.start()].strip():
            return core, ()
        label = before.value[: bracket.start()]
        return Segment((*tokens[: cut - 1], Text(label))), (Text(bracket.group(0)), *values)
    if not _LABEL_END.search(before.value):
        return core, ()
    gap = _TRAILING.search(before.value)
    label = before.value[: gap.start()] if gap else before.value
    space = (Text(gap.group(0)),) if gap else ()
    return Segment((*tokens[: cut - 1], Text(label))), (*space, *values)


def _is_value_or_separator(token: Token) -> bool:
    if isinstance(token, Entity):
        return True
    return isinstance(token, Text) and _SEPARATOR.fullmatch(token.value) is not None


def _trim(piece: Segment) -> tuple[tuple[Token, ...], Segment, tuple[Token, ...]]:
    """Split off the source's own whitespace at both ends of a piece."""
    tokens = list(piece.tokens)
    lead: tuple[Token, ...] = ()
    trail: tuple[Token, ...] = ()
    first = tokens[0]
    if isinstance(first, Text) and (m := _LEADING.match(first.value)):
        lead = (Text(m.group(0)),)
        tokens[0] = Text(first.value[m.end() :])
    last = tokens[-1]
    if isinstance(last, Text) and (m := _TRAILING.search(last.value)):
        trail = (Text(m.group(0)),)
        tokens[-1] = Text(last.value[: m.start()])
    core = [t for t in tokens if not (isinstance(t, Text) and t.value == "")]
    return lead, Segment(tuple(core)), trail


def _merge_text(tokens: list[Token]) -> list[Token]:
    """Adjacent text tokens joined, empty ones dropped: the shape the validators expect."""
    out: list[Token] = []
    for token in tokens:
        if isinstance(token, Text):
            if not token.value:
                continue
            if out and isinstance(out[-1], Text):
                out[-1] = Text(out[-1].value + token.value)
                continue
        out.append(token)
    return out
