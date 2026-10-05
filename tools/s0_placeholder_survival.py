"""S0.1: do placeholders survive the GovTech model? Requirements: FR-122, FR-140, FR-141,
FR-142, FR-401.

    python tools/s0_placeholder_survival.py                       # synthetic corpus
    python tools/s0_placeholder_survival.py blocks.json --formats wire xml
    python tools/s0_placeholder_survival.py --replay              # re-score, no API calls

Each block goes through the real pipeline -- parse, mask entities, substitute
glossary terms -- is encoded once per candidate marker format, sent to the
API **once**, decoded, and checked with exactly the validation the live
service applies (entities byte-identical, leak scan, terms restored, tags in
order). Nothing is retried: the question is how often the first answer is
usable.

Every raw response is recorded under ``tests/fixtures/mt-replay/`` keyed by
source hash, model version and format, so later tests replay them and never
call the API. Recorded text is the masked model input and the raw model
output: entity values never leave the request (FR-143).

Candidate formats:
  wire   ⟦1⟧…⟦/1⟧, ⟦CUR:1⟧      (pipeline/segment.py)
  xml    <x1>…</x1>, <e1/>       (pipeline/segment.py)
  brace  {1}…{/1}, {e1}          (here only; moved into the pipeline if it wins)

Live variant, the pipeline as adopted on 2026-10-05:
  live   xml, lenient decoding, placeholder-only text not sent, text with inline
         tags sent piece by piece (orchestrator/service/model_call.py)

Replay-only variants, scored from the ``xml`` recordings without new calls:
  xml-lenient       accepts the damage the model does to tag syntax but not to
                    tag identity: case, inner spaces, a missing ``/`` on an
                    entity or a known void. Every placeholder must still be there.
  xml-lenient-skip  as above, and a block with no words outside its placeholders
                    is never sent: the output is the input (a lone fee, a lone term).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.pipeline.glossary import (  # noqa: E402
    GlossaryCheckError,
    Termbase,
    restore_terms,
    substitute,
)
from orchestrator.pipeline.protect import EntityCheckError, mask, restore  # noqa: E402
from orchestrator.pipeline.segment import (  # noqa: E402
    MODEL_FORMATS,
    Close,
    Entity,
    ModelFormat,
    Open,
    Segment,
    SegmentError,
    Text,
    Token,
    Void,
    parse,
)
from orchestrator.pipeline.tags import check_tags  # noqa: E402
from orchestrator.service.model_call import translate_segment  # noqa: E402
from orchestrator.upstream.errors import UpstreamError  # noqa: E402
from orchestrator.wiring import ConfigError, Settings, environment  # noqa: E402

CORPUS = ROOT / "tests" / "fixtures" / "s0" / "synthetic-blocks.json"
TERMBASE = ROOT / "tests" / "fixtures" / "glossary" / "termbase-sample.json"
REPLAY = ROOT / "tests" / "fixtures" / "mt-replay"

_BRACE = re.compile(r"\{(/?)(e?)(\d+)(/?)\}")


class BraceFormat:
    """Candidate C: curly-brace markers, ``{1}…{/1}``, ``{2/}``, entities ``{e3}``."""

    name = "brace"

    def encode(self, segment: Segment) -> str:
        out: list[str] = []
        for t in segment.tokens:
            if isinstance(t, Text):
                out.append(t.value.replace("{", "(").replace("}", ")"))
            elif isinstance(t, Open):
                out.append(f"{{{t.id}}}")
            elif isinstance(t, Close):
                out.append(f"{{/{t.id}}}")
            elif isinstance(t, Void):
                out.append(f"{{{t.id}/}}")
            else:
                out.append(f"{{e{t.id}}}")
        return "".join(out)

    def decode(self, text: str, reference: Segment) -> Segment:
        kinds = {m.id: m.kind for m in reference.structure() if isinstance(m, Entity)}
        voids = {m.id for m in reference.structure() if isinstance(m, Void)}
        tokens: list[Token] = []
        pos = 0
        for m in _BRACE.finditer(text):
            if m.start() > pos:
                tokens.append(Text(text[pos : m.start()]))
            slash, entity, num, self_closing = m.group(1), m.group(2), int(m.group(3)), m.group(4)
            if entity:
                if slash or self_closing or num not in kinds:
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
            tokens.append(Text(text[pos:]))
        for t in tokens:
            if isinstance(t, Text) and ("{" in t.value or "}" in t.value):
                raise SegmentError("malformed_marker", "stray brace")
        return Segment(tuple(tokens))


_LENIENT = re.compile(r"<\s*(/?)\s*([xXeE])\s*(\d+)\s*(/?)\s*>")


class LenientXmlFormat:
    """Candidate B, decoding the syntax damage the model was seen to do (2026-10-05)."""

    name = "xml-lenient"

    def encode(self, segment: Segment) -> str:
        return MODEL_FORMATS["xml"].encode(segment)

    def decode(self, text: str, reference: Segment) -> Segment:
        kinds = {m.id: m.kind for m in reference.structure() if isinstance(m, Entity)}
        voids = {m.id for m in reference.structure() if isinstance(m, Void)}
        tokens: list[Token] = []
        pos = 0
        for m in _LENIENT.finditer(text):
            if m.start() > pos:
                tokens.append(Text(_unescape(text[pos : m.start()])))
            slash, kind, num = m.group(1), m.group(2).lower(), int(m.group(3))
            if kind == "e":
                # Entities are always void, so <e1>, <e1/> and <E1 /> all mean entity 1.
                if slash or num not in kinds:
                    raise SegmentError("malformed_marker", m.group(0))
                tokens.append(Entity(kinds[num], num))
            elif num in voids:
                if slash:
                    raise SegmentError("malformed_marker", m.group(0))
                tokens.append(Void(num))
            else:
                tokens.append(Close(num) if slash else Open(num))
            pos = m.end()
        if pos < len(text):
            tokens.append(Text(_unescape(text[pos:])))
        for t in tokens:
            if isinstance(t, Text) and re.search(r"<\s*/?\s*[xXeE]\s*\d", t.value):
                raise SegmentError("malformed_marker", "stray tag")
        return Segment(tuple(tokens))


def _unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def has_words(segment: Segment) -> bool:
    """Any letter outside the placeholders: something for the model to translate."""
    return any(isinstance(t, Text) and re.search(r"[^\W\d_]", t.value) for t in segment.tokens)


FORMATS: dict[str, ModelFormat] = {**MODEL_FORMATS, "brace": BraceFormat()}
LIVE = "live"
REPLAY_ONLY = {"xml-lenient": "xml", "xml-lenient-skip": "xml"}


@dataclass
class Outcome:
    block_type: str
    format: str
    ok: bool
    cause: str
    seconds: float


def cause_of(err: SegmentError) -> str:
    reason = getattr(err, "reason", None) or err.cause
    if isinstance(err, EntityCheckError):
        return f"entity:{reason}"
    if isinstance(err, GlossaryCheckError):
        return f"term:{reason}"
    return f"tag:{reason}"


def record(
    source: str, model_version: str, fmt: str, model_input: str, raw: str | None, outcome: Outcome
) -> None:
    key = hashlib.sha256(f"{source}|{model_version}|{fmt}".encode()).hexdigest()[:24]
    REPLAY.mkdir(parents=True, exist_ok=True)
    (REPLAY / f"{key}.json").write_text(
        json.dumps(
            {
                "source_wire": source,
                "model_version": model_version,
                "format": fmt,
                "model_input": model_input,
                "raw_output": raw,
                "ok": outcome.ok,
                "cause": outcome.cause,
                "block_type": outcome.block_type,
                "recorded": datetime.now(UTC).date().isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


async def measure(blocks: list[dict[str, str]], formats: list[str]) -> list[Outcome]:
    settings = Settings.from_env(
        {
            **environment(),
            "DZWEB_PG_DSN": "unused",
            "DZWEB_REDIS_URL": "unused",
            "DZWEB_TERMBASE": str(TERMBASE),
            "DZWEB_SITES": "unused",
        }
    )
    if settings.wso2 is None:
        raise ConfigError("set DZWEB_WSO2_* in .env first (see tools/wso2_probe.py)")
    from orchestrator.upstream.wso2 import Wso2Translator

    translator = Wso2Translator(settings.wso2)
    termbase = Termbase.load(TERMBASE)
    outcomes: list[Outcome] = []
    try:
        for block in blocks:
            source = parse(block["text"])
            masked, entities = mask(source)
            with_terms, terms = substitute(masked, termbase)
            for name in formats:
                fmt = MODEL_FORMATS["xml"] if name == LIVE else FORMATS[name]
                model_input = fmt.encode(with_terms)
                started = time.perf_counter()
                raw: str | None = None
                try:
                    if name == LIVE:
                        decoded = await translate_segment(translator, fmt, with_terms)
                        raw = fmt.encode(decoded)
                    else:
                        raw = await translator.translate(model_input, with_terms)
                        decoded = fmt.decode(raw, with_terms)
                    restored = restore(decoded, with_terms, entities)
                    restored = restore_terms(restored, with_terms, terms)
                    check_tags(restored, with_terms)
                    outcome = Outcome(block["type"], name, True, "ok", 0.0)
                except SegmentError as err:
                    outcome = Outcome(block["type"], name, False, cause_of(err), 0.0)
                except UpstreamError as err:
                    cause = f"upstream:{type(err).__name__}"
                    outcome = Outcome(block["type"], name, False, cause, 0.0)
                outcome.seconds = time.perf_counter() - started
                outcomes.append(outcome)
                version = translator.model_version
                record(with_terms.to_wire(), version, name, model_input, raw, outcome)
                mark = "ok  " if outcome.ok else "FAIL"
                print(f"{mark} {name:5} {block['type']:22} {outcome.cause}", flush=True)
    finally:
        await translator.aclose()
    return outcomes


def report(outcomes: list[Outcome], formats: list[str]) -> str:
    lines = ["", "== survival by format =="]
    for name in formats:
        mine = [o for o in outcomes if o.format == name]
        ok = sum(o.ok for o in mine)
        mean = sum(o.seconds for o in mine) / len(mine) if mine else 0.0
        lines.append(f"{name:6} {ok}/{len(mine)} usable ({100 * ok / max(1, len(mine)):.0f}%), "
                     f"mean {mean:.2f}s a call")
    lines.append("\n== fallback causes, by format ==")
    for name in formats:
        causes = Counter(o.cause for o in outcomes if o.format == name and not o.ok)
        listed = ", ".join(f"{c}={n}" for c, n in causes.most_common())
        lines.append(f"{name:6} {listed or 'none'}")
    lines.append("\n== usable, by block type and format ==")
    by_type: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for o in outcomes:
        by_type[o.block_type][o.format].append(o.ok)
    for block_type, per_format in sorted(by_type.items()):
        cells = [f"{f}={sum(per_format[f])}/{len(per_format[f])}" for f in formats]
        lines.append(f"{block_type:24} " + "  ".join(cells))
    return "\n".join(lines)


def rescore(blocks: list[dict[str, str]], formats: list[str]) -> list[Outcome]:
    """Score recorded outputs again, with no API calls."""
    termbase = Termbase.load(TERMBASE)
    recorded = {}
    for path in REPLAY.glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        recorded[(row["source_wire"], row["format"])] = row
    outcomes: list[Outcome] = []
    for block in blocks:
        source = parse(block["text"])
        masked, entities = mask(source)
        with_terms, terms = substitute(masked, termbase)
        for name in formats:
            recorded_as = REPLAY_ONLY.get(name, name)
            row = recorded.get((with_terms.to_wire(), recorded_as))
            if row is None:
                continue
            decoder: ModelFormat = LenientXmlFormat() if name in REPLAY_ONLY else FORMATS[name]
            try:
                if name == "xml-lenient-skip" and not has_words(with_terms):
                    decoded = with_terms  # never sent: nothing to translate
                elif row["raw_output"] is None:
                    raise UpstreamError(row["cause"])
                else:
                    decoded = decoder.decode(row["raw_output"], with_terms)
                restored = restore(decoded, with_terms, entities)
                restored = restore_terms(restored, with_terms, terms)
                check_tags(restored, with_terms)
                outcomes.append(Outcome(block["type"], name, True, "ok", 0.0))
            except SegmentError as err:
                outcomes.append(Outcome(block["type"], name, False, cause_of(err), 0.0))
            except UpstreamError as err:
                outcomes.append(Outcome(block["type"], name, False, str(err), 0.0))
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", nargs="?", type=Path, default=CORPUS)
    choices = [*FORMATS, LIVE, *REPLAY_ONLY]
    parser.add_argument("--formats", nargs="+", default=None, choices=choices)
    parser.add_argument("--replay", action="store_true", help="re-score recordings; no API calls")
    args = parser.parse_args(argv)
    blocks = json.loads(args.corpus.read_text(encoding="utf-8"))["blocks"]
    if args.replay:
        formats = [f for f in (args.formats or choices) if f != LIVE]
        print(report(rescore(blocks, formats), formats))
        return 0
    args.formats = [f for f in (args.formats or list(FORMATS)) if f not in REPLAY_ONLY]
    calls = len(blocks) * len(args.formats)
    print(f"{len(blocks)} blocks x {len(args.formats)} formats = {calls} calls to the API\n")
    outcomes = asyncio.run(measure(blocks, args.formats))
    print(report(outcomes, args.formats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
