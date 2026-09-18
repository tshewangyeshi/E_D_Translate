"""Dzongkha (Tibetan/Uchen script) specifics.

NFR-500: this module and ``adapters/widget/src/locale-dz.ts`` are the only
places outside ``tests/`` allowed to contain Tibetan code points or
Dzongkha-specific branches. ``tools/check_locale.py`` enforces it in CI.

Named ``orchestrator/locale`` rather than a top-level ``locale`` package so it
cannot shadow Python's standard-library ``locale`` module.
"""

from __future__ import annotations

TSHEG = "་"  # U+0F0B syllable delimiter (NOT a word boundary)
SHAD = "།"  # U+0F0D sentence terminator
DOUBLE_SHAD = "༎"  # U+0F0E
ZWSP = "\u200b"

TIBETAN_DIGITS = "༠༡༢༣༤༥༦༧༨༩"  # U+0F20..U+0F29

#: Sentence terminators for splitting over-long segments (FR-124): Dzongkha
#: shad and double shad plus English terminal punctuation.
SPLIT_TERMINATORS: frozenset[str] = frozenset({SHAD, DOUBLE_SHAD, ".", "!", "?"})

#: Characters that must never appear in stored text or TTS input (FR-160, FR-330).
RENDER_ARTEFACTS: frozenset[str] = frozenset({ZWSP, "\u200c", "\ufeff"})


def insert_breaks(text: str) -> str:
    """Render-time only (FR-160): allow line breaks after each tsheg."""
    return text.replace(TSHEG, TSHEG + ZWSP)


def strip_render_artefacts(text: str) -> str:
    """Remove zero-width characters before storage or TTS (FR-160, FR-330)."""
    return "".join(ch for ch in text if ch not in RENDER_ARTEFACTS)


def is_tibetan_digit(ch: str) -> bool:
    return len(ch) == 1 and ch in TIBETAN_DIGITS


def to_tibetan_digits(text: str) -> str:
    """ASCII digits → Tibetan digits. Used by the adversarial mock (S1.4) to
    imitate a model converting numbers, which the leak scan must catch (FR-142)."""
    return "".join(TIBETAN_DIGITS[int(ch)] if "0" <= ch <= "9" else ch for ch in text)
