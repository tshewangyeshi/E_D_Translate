"""chrF++ (Popovic 2017), corpus level, as sacrebleu computes it (S10.1, NFR-202).

Character n-grams of order 1-6 and word n-grams of order 1-2, F-score with
beta = 2, statistics summed over the corpus. Whitespace is ignored for the
character n-grams. Checked against sacrebleu 2.x on 2026-10-06
(tests/orchestrator/test_chrf.py pins the agreed values), so a score here is
comparable with published chrF++ figures without carrying the dependency.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

CHAR_ORDER = 6
WORD_ORDER = 2
BETA = 2.0
EPS = 1e-16
#: Punctuation sacrebleu splits off a word before taking word n-grams.
_PUNCTS = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def _char_ngrams(text: str, n: int) -> Counter[str]:
    s = "".join(text.split())
    return Counter(s[i : i + n] for i in range(len(s) - n + 1))


def _words(text: str) -> list[str]:
    """sacrebleu's rule: one punctuation mark split off the end, else the start."""
    out: list[str] = []
    for w in text.split():
        if len(w) == 1:
            out.append(w)
        elif w[-1] in _PUNCTS:
            out += [w[:-1], w[-1]]
        elif w[0] in _PUNCTS:
            out += [w[0], w[1:]]
        else:
            out.append(w)
    return out


def _word_ngrams(words: list[str], n: int) -> Counter[tuple[str, ...]]:
    return Counter(tuple(words[i : i + n]) for i in range(len(words) - n + 1))


def _counts(h: Counter[Any], r: Counter[Any]) -> tuple[int, int, int]:
    """Matching, hypothesis and reference n-grams of one order."""
    return sum((h & r).values()), sum(h.values()), sum(r.values())


def corpus_chrf(hypotheses: Sequence[str], references: Sequence[str]) -> float:
    """chrF++ in 0..100 for aligned hypotheses and single references."""
    orders = CHAR_ORDER + WORD_ORDER
    match = [0] * orders
    hyp_total = [0] * orders
    ref_total = [0] * orders
    for hyp, ref in zip(hypotheses, references, strict=True):
        h_words, r_words = _words(hyp), _words(ref)
        for i in range(orders):
            if i < CHAR_ORDER:
                counts = _counts(_char_ngrams(hyp, i + 1), _char_ngrams(ref, i + 1))
            else:
                n = i - CHAR_ORDER + 1
                counts = _counts(_word_ngrams(h_words, n), _word_ngrams(r_words, n))
            match[i] += counts[0]
            hyp_total[i] += counts[1]
            ref_total[i] += counts[2]

    precision = recall = 0.0
    effective = 0
    for i in range(orders):
        if hyp_total[i] > 0 and ref_total[i] > 0:
            precision += match[i] / hyp_total[i]
            recall += match[i] / ref_total[i]
            effective += 1
    if effective == 0:
        return 0.0
    precision /= effective
    recall /= effective
    if precision + recall == 0:
        return 0.0
    beta2 = BETA**2
    return 100 * (1 + beta2) * precision * recall / (beta2 * precision + recall + EPS)
