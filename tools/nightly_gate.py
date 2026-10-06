"""Nightly gate on the real endpoint (S10.2, S10.1). Requirements: NFR-200, NFR-201, NFR-202.

    python tools/nightly_gate.py                 # the whole frozen set
    python tools/nightly_gate.py --limit 40      # a sample, to spare the quota

Sends the frozen evaluation set (eval/frozen/segments.jsonl, tools/build_eval_set.py)
through the live pipeline -- masking, glossary, the adopted model format, pieces
and sentences -- to the GovTech endpoint in .env, then checks every answer the
way the service does. Fails (exit 1) when:

  * tag integrity is below 99% (NFR-200), or
  * entity preservation is below 100% (NFR-201): something that would be served
    lacks one of its source's protected values, byte for byte.

chrF++ (NFR-202) is scored on the segments that have a human reference; until
references exist it reports how many are missing. Each run appends one line
to eval/frozen/trend.csv, so the trend is there to read.

The access token comes from the shared Redis store when DZWEB_REDIS_URL is
reachable, so a nightly run does not ask the token server again (GovTech's
request, 2026-10-05).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.pipeline.glossary import (  # noqa: E402
    GlossaryCheckError,
    Termbase,
    restore_terms,
    substitute,
)
from orchestrator.pipeline.protect import (  # noqa: E402
    TRANSLATABLE_KINDS,
    EntityCheckError,
    mask,
    restore,
)
from orchestrator.pipeline.segment import (  # noqa: E402
    DEFAULT_MODEL_FORMAT,
    MODEL_FORMATS,
    SegmentError,
    Text,
    escape,
    parse,
)
from orchestrator.pipeline.tags import check_tags  # noqa: E402
from orchestrator.service.model_call import translate_segment  # noqa: E402
from orchestrator.upstream.errors import UpstreamError  # noqa: E402
from orchestrator.wiring import environment  # noqa: E402
from tools.chrf import corpus_chrf  # noqa: E402

SET = ROOT / "eval" / "frozen" / "segments.jsonl"
TREND = ROOT / "eval" / "frozen" / "trend.csv"
LAST_RUN = ROOT / "eval" / "frozen" / "last-run.jsonl"
EMPTY_TERMBASE = ROOT / "tools" / "demo_site" / "termbase-empty.json"
TAG_INTEGRITY_GATE = 0.99
ENTITY_PRESERVATION_GATE = 1.0


def _translator(env: dict[str, str]) -> Any:
    from orchestrator.upstream.wso2 import Wso2Translator
    from orchestrator.wiring import _wso2

    config = _wso2(env)
    if config is None:
        raise SystemExit("set DZWEB_WSO2_* in .env first (see tools/wso2_probe.py)")
    store = None
    if env.get("DZWEB_REDIS_URL"):
        import redis

        from orchestrator.upstream.token_store import RedisTokenStore

        client = redis.Redis.from_url(env["DZWEB_REDIS_URL"], socket_connect_timeout=0.25)
        store = RedisTokenStore(client, config.token_url, config.client_id)
    return Wso2Translator(config, token_store=store)


async def _one(item: dict[str, Any], translator: Any, termbase: Termbase) -> dict[str, Any]:
    source = parse(escape(item["text"]))
    masked, entities = mask(source, TRANSLATABLE_KINDS)
    with_terms, terms = substitute(masked, termbase)
    fmt = MODEL_FORMATS[DEFAULT_MODEL_FORMAT]
    try:
        decoded = await translate_segment(translator, fmt, with_terms)
    except UpstreamError as err:
        return {"outcome": "upstream", "detail": type(err).__name__}
    except SegmentError as err:
        return {"outcome": "tag", "detail": err.cause, "model": True}
    try:
        restored = restore_terms(restore(decoded, with_terms, entities), with_terms, terms)
        check_tags(restored, with_terms)
    except EntityCheckError as err:
        return {"outcome": "entity", "detail": err.reason, "model": True}
    except GlossaryCheckError as err:
        return {"outcome": "term", "detail": err.cause, "model": True}
    except SegmentError as err:
        return {"outcome": "tag", "detail": err.cause, "model": True}
    text = "".join(t.value for t in restored.tokens if isinstance(t, Text))
    intact = all(e.value in text for e in entities.values())  # independent of restore()
    return {"outcome": "served", "text": text, "intact": intact, "model": True}


async def run(items: list[dict[str, Any]], concurrency: int) -> list[dict[str, Any]]:
    translator = _translator(environment())
    # The configured termbase, or none: the sample termbase has dummy targets
    # that would put invented Dzongkha into the scores.
    termbase = Termbase.load(Path(environment().get("DZWEB_TERMBASE") or EMPTY_TERMBASE))
    slots = asyncio.Semaphore(concurrency)

    async def bounded(item: dict[str, Any]) -> dict[str, Any]:
        async with slots:
            return {**item, **(await _one(item, translator, termbase))}

    try:
        return await asyncio.gather(*(bounded(i) for i in items))
    finally:
        await translator.aclose()


def report(results: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    outcomes = Counter(r["outcome"] for r in results)
    model = [r for r in results if r.get("model")]
    served = [r for r in results if r["outcome"] == "served"]
    tag_integrity = 1 - outcomes["tag"] / len(model) if model else 1.0
    preservation = sum(1 for r in served if r["intact"]) / len(served) if served else 1.0
    with_ref = [r for r in results if r.get("reference")]
    chrf = (
        corpus_chrf(
            [r.get("text") or r["text_en"] for r in with_ref], [r["reference"] for r in with_ref]
        )
        if with_ref
        else None
    )
    summary = {
        "when": datetime.now(UTC).isoformat(timespec="seconds"),
        "segments": len(results),
        "served": len(served),
        "outcomes": dict(outcomes),
        "tag_integrity": round(tag_integrity, 4),
        "entity_preservation": round(preservation, 4),
        "chrf_plus_plus": None if chrf is None else round(chrf, 2),
        "references": len(with_ref),
    }
    ok = tag_integrity >= TAG_INTEGRITY_GATE and preservation >= ENTITY_PRESERVATION_GATE
    return summary, ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--set", type=Path, default=SET)
    parser.add_argument("--limit", type=int, default=0, help="the first N segments only")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args(argv)
    if not args.set.exists():
        print(
            f"no evaluation set at {args.set}: run python tools/build_eval_set.py", file=sys.stderr
        )
        return 2
    items = [json.loads(line) for line in args.set.read_text(encoding="utf-8").splitlines() if line]
    if args.limit:
        items = items[: args.limit]
    for item in items:
        item["text_en"] = item["text"]
    results = asyncio.run(run(items, args.concurrency))
    summary, ok = report(results)
    # Each segment's outcome, for reading why one fell back. Private, like the set.
    with LAST_RUN.open("w", encoding="utf-8") as out:
        for r in results:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=1))
    new_file = not TREND.exists()
    with TREND.open("a", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        if new_file:
            writer.writerow(
                [
                    "when",
                    "segments",
                    "served",
                    "tag_integrity",
                    "entity_preservation",
                    "chrf_plus_plus",
                ]
            )
        writer.writerow(
            [
                summary[k]
                for k in (
                    "when",
                    "segments",
                    "served",
                    "tag_integrity",
                    "entity_preservation",
                    "chrf_plus_plus",
                )
            ]
        )
    if not ok:
        print("NIGHTLY GATE FAILED (NFR-200 / NFR-201)", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
