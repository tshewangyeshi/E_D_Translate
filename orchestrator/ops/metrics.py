"""Operational metrics (backlog S2.3). Requirement: FR-611.

What FR-611 asks for, and where each number comes from:

    cache hit rate        lookups answered by the cache / all lookups
    tag integrity         model outputs whose tags survived / model outputs
    entity preservation   model outputs whose entities survived / model outputs
    glossary compliance   glossary terms back exactly once / glossary terms sent
    latency               /v1/translate request time, as a histogram
    upstream errors       by error type
    fallback by cause     every segment answered with source text, by cause
    pending_mt rate       segments answered pending_mt / all segments
    queue depth and age   read from the database, at most every few seconds

Counters live in this process and start at zero when it starts. Each API
replica is scraped on its own, and the counters describe the live request path
only. The background worker is a separate process with no HTTP surface; it
logs a summary line per batch it works on, and exporting its counts is a
separate item in TODOS.md.

Nothing here carries segment text, a path or a client. Labels are statuses and
causes the code defines, never anything a request supplied.

The output is the Prometheus text exposition format, written by hand: it is a
few lines of string formatting and saves a dependency.
"""

from __future__ import annotations

import logging
import time
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from itertools import accumulate
from typing import TYPE_CHECKING

from orchestrator.pipeline.glossary import ComplianceStats
from orchestrator.service.status import Status
from orchestrator.store.lookup import SOURCE_CACHE

if TYPE_CHECKING:
    from orchestrator.service.translate import TranslateService

log = logging.getLogger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Seconds. 0.3 is the cached-batch target (NFR-100), 0.8 the Redis-down target
#: (ER-21) and 1.5 the live budget (FR-155), so each has a bucket edge of its own.
LATENCY_BUCKETS = (0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.5, 3.0)

MODEL_OUTPUT_OK = "ok"

#: Database-backed gauges are read at most this often, however often scraped.
GAUGE_REUSE_SECONDS = 10.0


@dataclass
class Histogram:
    bounds: tuple[float, ...] = LATENCY_BUCKETS
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    observations: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.bounds) + 1)  # the last one is +Inf

    def observe(self, value: float) -> None:
        value = max(0.0, value)
        self.counts[bisect_left(self.bounds, value)] += 1  # value <= bound, or +Inf
        self.total += value
        self.observations += 1

    def cumulative(self) -> list[tuple[str, int]]:
        """(upper bound, observations at or below it), ending with +Inf."""
        running = accumulate(self.counts)
        return [(_number(b), n) for b, n in zip(self.bounds, running, strict=False)] + [
            ("+Inf", self.observations)
        ]


@dataclass
class Metrics:
    statuses: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    queue_full: int = 0
    #: Translations served although they could not be stored (and so were queued).
    store_failures: int = 0
    #: One entry per output the model returned: "ok" or the status it failed with.
    model_outputs: Counter[str] = field(default_factory=Counter)
    upstream_errors: Counter[str] = field(default_factory=Counter)
    terms: ComplianceStats = field(default_factory=ComplianceStats)
    latency: Histogram = field(default_factory=Histogram)

    def ratio_of_outputs_without(self, failure: Status) -> float:
        """1.0 when the model has returned nothing yet: no evidence is not a failure."""
        outputs = sum(self.model_outputs.values())
        return 1.0 if outputs == 0 else 1.0 - self.model_outputs[failure.value] / outputs

    def ratio_of_segments(self, status: Status) -> float:
        segments = sum(self.statuses.values())
        return 0.0 if segments == 0 else self.statuses[status.value] / segments


@dataclass(frozen=True)
class Family:
    name: str
    kind: str  # "counter" | "gauge" | "histogram"
    help: str
    samples: tuple[tuple[str, tuple[tuple[str, str], ...], float], ...]  # (suffix, labels, value)


#: name -> (help, read). A read that raises leaves its gauge out of the scrape.
Gauges = Mapping[str, tuple[str, Callable[[], float | None]]]


def service_gauges(service: TranslateService) -> dict[str, tuple[str, Callable[[], float | None]]]:
    """The database-backed gauges, read through the service's own stores.

    Production passes the same reads on a separate connection (wiring.py), so a
    scrape never queues behind citizen requests.
    """
    return {
        "dzweb_queue_depth": ("Jobs waiting for the worker.", service.queue.depth),
        "dzweb_queue_oldest_pending_seconds": (
            "Age of the oldest job still waiting.",
            lambda: service.queue.oldest_pending_seconds() or 0.0,
        ),
        "dzweb_review_pending": (
            "Machine translations waiting for a reviewer.",
            service.store.tm.pending_review,
        ),
        "dzweb_review_owed": (
            "Review items past the daily cap, not yet released to reviewers.",
            service.store.tm.owed,
        ),
    }


class GaugeReader:
    """Reads database gauges off the request path, at most every few seconds."""

    def __init__(self, gauges: Gauges, reuse_seconds: float | None = None) -> None:
        self.gauges = gauges
        self.reuse_seconds = GAUGE_REUSE_SECONDS if reuse_seconds is None else reuse_seconds
        self._last: tuple[float, list[Family]] | None = None

    def read(self) -> list[Family]:
        """Blocking; call from a thread."""
        if self._last is not None and time.monotonic() - self._last[0] < self.reuse_seconds:
            return self._last[1]
        families = []
        for name, (help_, read) in self.gauges.items():
            try:
                value = read()
            except Exception as err:  # noqa: BLE001 - readable exactly when things break
                log.info("gauge %s unavailable: %s", name, type(err).__name__)
                continue
            if value is not None:
                families.append(_single(name, "gauge", help_, value))
        self._last = (time.monotonic(), families)
        return families


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render(families: Iterable[Family]) -> str:
    lines: list[str] = []
    for family in families:
        lines.append(f"# HELP {family.name} {family.help}")
        lines.append(f"# TYPE {family.name} {family.kind}")
        for suffix, labels, value in family.samples:
            text = ",".join(f'{k}="{_label_value(v)}"' for k, v in labels)
            braces = "{" + text + "}" if text else ""
            lines.append(f"{family.name}{suffix}{braces} {_number(value)}")
    return "\n".join(lines) + "\n"


def _counter(name: str, help_: str, label: str, values: Mapping[str, int]) -> Family:
    samples = tuple(("", ((label, key),), float(n)) for key, n in sorted(values.items()))
    return Family(name, "counter", help_, samples)


def _single(name: str, kind: str, help_: str, value: float) -> Family:
    return Family(name, kind, help_, (("", (), float(value)),))


def collect(service: TranslateService) -> list[Family]:
    """The in-memory counters. Cheap: no I/O."""
    m, store = service.metrics, service.store
    lookups = store.lookups
    total_lookups = sum(lookups.values())

    families = [
        _counter("dzweb_segments_total", "Segments answered, by status.", "status", m.statuses),
        _counter(
            "dzweb_fallback_total",
            "Segments answered with source text, by cause.",
            "cause",
            m.reasons,
        ),
        _counter(
            "dzweb_lookups_total",
            "Segment lookups, by where the answer came from.",
            "source",
            lookups,
        ),
        _counter(
            "dzweb_model_outputs_total",
            "Outputs returned by the model on the live path, by validation result.",
            "result",
            m.model_outputs,
        ),
        _counter(
            "dzweb_glossary_terms_total",
            "Glossary terms sent to the model on the live path, by outcome.",
            "result",
            {"restored": m.terms.restored, "missing": m.terms.found - m.terms.restored},
        ),
        _counter(
            "dzweb_upstream_errors_total",
            "Failed calls to the translation model, by error type.",
            "type",
            m.upstream_errors,
        ),
        _counter(
            "dzweb_review_flags_total",
            "Attempts to flag Tier 2 machine output for review, by outcome.",
            "outcome",
            store.review_flags,
        ),
        _single(
            "dzweb_queue_full_total",
            "counter",
            "Segments that could not be queued because the queue was full.",
            m.queue_full,
        ),
        _single(
            "dzweb_store_failures_total",
            "counter",
            "Translations served although they could not be stored; they were queued.",
            m.store_failures,
        ),
        _single(
            "dzweb_cache_failures_total",
            "counter",
            "Cache operations that failed and were treated as misses.",
            getattr(store.cache, "failures", 0),
        ),
        _single(
            "dzweb_seen_counter_failures_total",
            "counter",
            "Distinct-client counts that failed; the text was not translated.",
            getattr(store.seen, "failures", 0),
        ),
        _single(
            "dzweb_tag_integrity_ratio",
            "gauge",
            "Model outputs whose tags survived, as a share of model outputs.",
            m.ratio_of_outputs_without(Status.TAG_FALLBACK),
        ),
        _single(
            "dzweb_entity_preservation_ratio",
            "gauge",
            "Model outputs whose entities survived, as a share of model outputs.",
            m.ratio_of_outputs_without(Status.ENTITY_CHECK_FAILED),
        ),
        _single(
            "dzweb_glossary_compliance_ratio",
            "gauge",
            "Glossary terms restored, as a share of glossary terms sent.",
            m.terms.rate,
        ),
        _single(
            "dzweb_pending_mt_ratio",
            "gauge",
            "Segments answered pending_mt, as a share of all segments.",
            m.ratio_of_segments(Status.PENDING_MT),
        ),
        Family(
            "dzweb_request_seconds",
            "histogram",
            "Time to answer /v1/translate.",
            (
                *(("_bucket", (("le", le),), float(n)) for le, n in m.latency.cumulative()),
                ("_sum", (), m.latency.total),
                ("_count", (), float(m.latency.observations)),
            ),
        ),
    ]
    # Left out until there is a lookup to count: a fresh replica reading 0% would
    # trip a low-hit-rate alert on every restart.
    if total_lookups:
        families.append(
            _single(
                "dzweb_cache_hit_ratio",
                "gauge",
                "Lookups answered by the cache, as a share of all lookups.",
                lookups[SOURCE_CACHE] / total_lookups,
            )
        )
    return families
