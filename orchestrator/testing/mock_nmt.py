"""Adversarial NMT mock (backlog S1.4). Supports FR-122, FR-123, FR-141, FR-142.

A model that misbehaves on purpose, so restoration and validation are tested
against realistic failure rather than a cooperative stub. Deterministic under
a seed. Mode mix and rates are to be calibrated from Sprint 0 recordings (S0.1).

It corrupts the ENCODED model text marker by marker, using the same
ModelFormat the pipeline uses, so every candidate token format is exercised:

    segment ─► per-token encode ─► pieces [text][⟦1⟧][text][⟦/1⟧][text]
                                          │ drop / duplicate / reorder / invent /
                                          │ truncate / mangle / digits ...
                                          ▼
                                    joined model output
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from orchestrator.locale.dz import to_tibetan_digits
from orchestrator.pipeline.segment import (
    Entity,
    ModelFormat,
    Segment,
    Text,
    Token,
    Void,
)


class Mode(StrEnum):
    WELL_BEHAVED = "well_behaved"
    DROP = "drop"  # a placeholder disappears
    DUPLICATE = "duplicate"  # a placeholder appears twice
    REORDER = "reorder"  # two placeholders swap places
    INVENT = "invent"  # a placeholder with an unknown id appears
    TRUNCATE = "truncate"  # a placeholder loses its closing character
    MANGLE = "mangle"  # a placeholder's id changes
    TIBETAN_DIGITS = "tibetan_digits"  # an entity is replaced by Tibetan digits
    INVENT_NUMBER = "invent_number"  # a number appears in plain text
    EMPTY = "empty"  # the model returns nothing
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"  # HTTP 503


CORRUPTING_MODES: tuple[Mode, ...] = (
    Mode.DROP,
    Mode.DUPLICATE,
    Mode.REORDER,
    Mode.INVENT,
    Mode.TRUNCATE,
    Mode.MANGLE,
    Mode.TIBETAN_DIGITS,
    Mode.INVENT_NUMBER,
    Mode.EMPTY,
)


class UpstreamError(Exception):
    """Base class for simulated upstream failures."""


class UpstreamTimeout(UpstreamError):
    pass


class UpstreamUnavailable(UpstreamError):
    status = 503


@dataclass(frozen=True)
class _Piece:
    text: str
    token: Token | None  # None for pieces the mock invented


def _identity(text: str) -> str:
    return text


class MockNMT:
    """``translate(segment) -> str`` in the configured ModelFormat.

    ``modes`` maps each mode to a relative weight; one mode is drawn per call.
    ``translate_text`` stands in for the real translation of plain text runs.
    """

    def __init__(
        self,
        fmt: ModelFormat,
        *,
        seed: int = 0,
        modes: dict[Mode, float] | None = None,
        translate_text: Callable[[str], str] = _identity,
    ) -> None:
        self.fmt = fmt
        self.rng = random.Random(seed)  # noqa: S311 - test double, not crypto
        self.modes = modes or {Mode.WELL_BEHAVED: 1.0}
        self.translate_text = translate_text
        self.last_mode: Mode | None = None

    def pick_mode(self) -> Mode:
        modes = list(self.modes)
        weights = [self.modes[m] for m in modes]
        picked: Mode = self.rng.choices(modes, weights=weights, k=1)[0]
        return picked

    def translate(self, segment: Segment, mode: Mode | None = None) -> str:
        mode = mode or self.pick_mode()
        self.last_mode = mode
        if mode is Mode.TIMEOUT:
            raise UpstreamTimeout("simulated timeout")
        if mode is Mode.UNAVAILABLE:
            raise UpstreamUnavailable("simulated 503")
        if mode is Mode.EMPTY:
            return ""
        pieces = [self._encode_piece(t) for t in segment.tokens]
        pieces = self._corrupt(pieces, segment, mode)
        return "".join(p.text for p in pieces)

    def _encode_piece(self, token: Token) -> _Piece:
        if isinstance(token, Text):
            token = Text(self.translate_text(token.value))
        return _Piece(self.fmt.encode(Segment((token,))), token)

    def _text_piece(self, text: str) -> _Piece:
        return _Piece(self.fmt.encode(Segment((Text(text),))), None)

    def _marker_indexes(
        self, pieces: Sequence[_Piece], *, entities_only: bool = False
    ) -> list[int]:
        out = []
        for i, p in enumerate(pieces):
            if p.token is None or isinstance(p.token, Text):
                continue
            if entities_only and not isinstance(p.token, Entity):
                continue
            out.append(i)
        return out

    def _corrupt(self, pieces: list[_Piece], segment: Segment, mode: Mode) -> list[_Piece]:
        rng = self.rng
        markers = self._marker_indexes(pieces)
        if mode is Mode.WELL_BEHAVED:
            return pieces
        if mode is Mode.INVENT_NUMBER:
            return [*pieces, self._text_piece(" 42")]
        if mode is Mode.INVENT:
            new_id = max((m.id for m in segment.structure()), default=0) + 1
            at = rng.randrange(len(pieces) + 1)
            invented = _Piece(self.fmt.encode(Segment((Void(new_id),))), None)
            return [*pieces[:at], invented, *pieces[at:]]
        if not markers:
            return pieces  # nothing structural to corrupt; still a valid adversarial draw
        i = rng.choice(markers)
        if mode is Mode.DROP:
            return [p for k, p in enumerate(pieces) if k != i]
        if mode is Mode.DUPLICATE:
            return [*pieces[: i + 1], pieces[i], *pieces[i + 1 :]]
        if mode is Mode.REORDER:
            if len(markers) < 2:
                return [p for k, p in enumerate(pieces) if k != i]  # degrade to a drop
            a, b = rng.sample(markers, 2)
            swapped = list(pieces)
            swapped[a], swapped[b] = swapped[b], swapped[a]
            return swapped
        if mode is Mode.TRUNCATE:
            p = pieces[i]
            return [*pieces[:i], _Piece(p.text[:-1], None), *pieces[i + 1 :]]
        if mode is Mode.MANGLE:
            p = pieces[i]
            mangled = "".join(str((int(c) + 1) % 10) if c.isdigit() else c for c in p.text)
            return [*pieces[:i], _Piece(mangled, None), *pieces[i + 1 :]]
        if mode is Mode.TIBETAN_DIGITS:
            ents = self._marker_indexes(pieces, entities_only=True)
            if not ents:
                return [*pieces, self._text_piece(to_tibetan_digits(" 7"))]
            j = rng.choice(ents)
            digits = self._text_piece(to_tibetan_digits(str(rng.randrange(10, 99999))))
            return [*pieces[:j], digits, *pieces[j + 1 :]]
        raise AssertionError(f"unhandled mode {mode}")  # pragma: no cover
