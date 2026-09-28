"""Translate a batch of widget segments (backlog S2.1).

Requirements: FR-100, FR-104, FR-122, FR-140..143, FR-155, FR-156, FR-400, FR-401,
FR-510, FR-512, NFR-304, NFR-410, NFR-412.

    per segment: parse ─► mask ─► glossary ─► keys ─► effective tier
                   │ malformed                              │
                   ▼                                        ▼
              tag_fallback            ONE batched lookup (tier-gated, S1.7)
                                         │ hit                  │ miss
                                         ▼                      ▼
                       finalise: entities, terms,   tier 1  ─► tier_blocked
                       tags (on EVERY serve)        tier 2+ ─┬ < N clients ─► pending_mt
                                                             ├ no quota ─► enqueue, pending_mt
                                                             └ live MT within budget:
                                                                 ok, valid ─► store, translated
                                                                 invalid   ─► entity/tag/term fail
                                                                 upstream  ─► upstream_error+enqueue
                                                                 too slow  ─► enqueue, pending_mt

Every failure returns the source text (NFR-410). Nothing here raises for a
translation failure; the API never turns one into a 5xx.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from orchestrator.governance.sites import Site, resolve_tier
from orchestrator.pipeline.glossary import (
    GlossaryCheckError,
    Termbase,
    TermMap,
    fingerprint,
    restore_terms,
    substitute,
)
from orchestrator.pipeline.protect import EntityCheckError, EntityMap, mask, restore
from orchestrator.pipeline.segment import ModelFormat, Segment, SegmentError, parse
from orchestrator.pipeline.tags import check_tags
from orchestrator.queue.jobs import PRIORITY_LIVE_DEFERRED, Job, JobQueue
from orchestrator.store.keys import SegmentKeys, Versions, keys_for
from orchestrator.store.lookup import Hit, LookupItem, TranslationStore
from orchestrator.store.models import Origin
from orchestrator.testing.mock_nmt import UpstreamError
from orchestrator.upstream.quota import QuotaManager
from orchestrator.upstream.translator import Translator

log = logging.getLogger(__name__)


class Status(StrEnum):
    TRANSLATED = "translated"
    PENDING_MT = "pending_mt"  # the only non-final status
    TIER_BLOCKED = "tier_blocked"
    ENTITY_CHECK_FAILED = "entity_check_failed"
    GLOSSARY_TERM_MISSING = "glossary_term_missing"
    TAG_FALLBACK = "tag_fallback"
    UPSTREAM_ERROR = "upstream_error"


@dataclass(frozen=True)
class SegmentIn:
    id: str
    text: str  # wire format from the widget
    tier: object = None  # hint only (FR-512)
    selector_tier: object = None


@dataclass(frozen=True)
class SegmentOut:
    id: str
    text: str  # wire format: translation, or the source on any failure
    status: Status
    segment_key: str | None = None
    origin: Origin | None = None
    reason: str | None = None  # stable sub-cause for metrics; not shown to citizens


@dataclass
class ServiceSettings:
    live_budget_seconds: float = 1.5  # FR-155
    source_lang: str = "en"
    target_lang: str = "dz"


@dataclass
class _Prepared:
    seg_in: SegmentIn
    source: Segment
    with_terms: Segment
    entities: EntityMap
    terms: TermMap
    keys: SegmentKeys
    tier: int


@dataclass
class Metrics:
    statuses: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    queue_full: int = 0


class TranslateService:
    def __init__(
        self,
        *,
        store: TranslationStore,
        termbase: Termbase,
        translator: Translator,
        model_format: ModelFormat,
        queue: JobQueue,
        quota: QuotaManager,
        pipeline_version: str,
        settings: ServiceSettings | None = None,
    ) -> None:
        self.store = store
        self.termbase = termbase
        self.translator = translator
        self.fmt = model_format
        self.queue = queue
        self.quota = quota
        self.settings = settings or ServiceSettings()
        self.versions = Versions(
            self.settings.source_lang,
            self.settings.target_lang,
            pipeline_version,
            translator.model_version,
        )
        self.metrics = Metrics()

    # -- public ---------------------------------------------------------------

    async def translate(
        self,
        site: Site,
        client_hash: str,
        segments: list[SegmentIn],
        path: object = None,
    ) -> list[SegmentOut]:
        out: dict[int, SegmentOut] = {}
        prepared: dict[int, _Prepared] = {}
        for n, seg_in in enumerate(segments):
            try:
                prepared[n] = self._prepare(site, seg_in, path)
            except SegmentError as err:
                out[n] = self._fail(seg_in, None, Status.TAG_FALLBACK, err.cause)

        try:
            hits = self.store.lookup([LookupItem(p.keys, p.tier) for p in prepared.values()])
        except Exception as err:  # noqa: BLE001 - storage down must never become a 5xx
            log.warning("lookup failed; serving source text: %s", type(err).__name__)
            for n, p in prepared.items():
                out[n] = self._fail(p.seg_in, p.keys, Status.UPSTREAM_ERROR, "store_unavailable")
            return self._ordered(segments, out)

        live: list[int] = []
        for (n, p), hit in zip(prepared.items(), hits, strict=True):
            if hit is not None:
                out[n] = self._serve_hit(p, hit)
            elif p.tier == 1:
                out[n] = self._fail(
                    p.seg_in, p.keys, Status.TIER_BLOCKED, "no_approved_translation"
                )
            elif not self._safe_persist(p, client_hash):
                out[n] = self._fail(
                    p.seg_in, p.keys, Status.PENDING_MT, "awaiting_distinct_clients"
                )
            elif self.quota.try_live():
                live.append(n)
            else:
                out[n] = self._defer(site, p, "no_live_quota")

        if live:
            out.update(await self._translate_live(site, {n: prepared[n] for n in live}))
        return self._ordered(segments, out)

    # -- steps ----------------------------------------------------------------

    def prepare(self, site: Site, seg_in: SegmentIn, path: object = None) -> _Prepared:
        """Parse, mask, apply the glossary, derive keys and tier. Raises SegmentError."""
        return self._prepare(site, seg_in, path)

    def job_for(self, site: Site, p: _Prepared, priority: int = PRIORITY_LIVE_DEFERRED) -> Job:
        """A queue job for a prepared segment. Masked text only (FR-143)."""
        return Job(
            machine_key=p.keys.machine_key,
            segment_key=p.keys.segment_key,
            approved_key=p.keys.approved_key,
            masked_source=p.with_terms.to_wire(),
            gfp=p.keys.gfp,
            term_ids=tuple(sorted({t.term_id for t in p.terms.values()})),
            site_id=site.site_id,
            model_version=self.translator.model_version,
            priority=priority,
        )

    def _prepare(self, site: Site, seg_in: SegmentIn, path: object = None) -> _Prepared:
        source = parse(seg_in.text)  # client input: entity tokens are rejected (S1.2)
        masked, entities = mask(source)
        with_terms, terms = substitute(masked, self.termbase)
        keys = keys_for(with_terms, fingerprint(terms.values()), self.versions)
        tier = resolve_tier(site, seg_in.tier, seg_in.selector_tier, path)
        return _Prepared(seg_in, source, with_terms, entities, terms, keys, tier)

    def _finalise(self, p: _Prepared, model_output: Segment) -> Segment:
        """Entities restored byte-identical, tags exact, terms restored. Raises on any failure.

        Entities and terms first, so a duplicated entity or term is reported as
        entity_check_failed / glossary_term_missing, not as a generic tag failure.
        """
        restored = restore(model_output, p.with_terms, p.entities)
        restored = restore_terms(restored, p.with_terms, p.terms)
        check_tags(restored, p.with_terms)
        return restored

    def _serve_hit(self, p: _Prepared, hit: Hit) -> SegmentOut:
        try:
            stored = parse(hit.stored.masked_target, allow_entities=True)
            final = self._finalise(p, stored)
        except SegmentError as err:
            # Stored output is validated before storage; failing now means drift. Never serve it.
            log.error("stored translation failed re-validation: %s", err.cause)
            return self._fail(p.seg_in, p.keys, _status_for(err), f"stored:{_reason(err)}")
        return self._ok(p, final, hit.stored.origin)

    def _safe_persist(self, p: _Prepared, client_hash: str) -> bool:
        try:
            return self.store.should_persist(p.keys, client_hash)
        except Exception:  # noqa: BLE001 - fail safe: do not persist or translate (NFR-304)
            return False

    def _enqueue(self, site: Site, p: _Prepared) -> bool:
        try:
            queued = self.queue.enqueue(self.job_for(site, p))
        except Exception:  # noqa: BLE001 - a queue outage must not fail the request
            queued = False
        if not queued:
            self.metrics.queue_full += 1
        return queued

    def _defer(self, site: Site, p: _Prepared, reason: str) -> SegmentOut:
        if not self._enqueue(site, p):
            reason = f"{reason}+queue_full"
        return self._fail(p.seg_in, p.keys, Status.PENDING_MT, reason)

    async def _translate_live(
        self, site: Site, items: dict[int, _Prepared]
    ) -> dict[int, SegmentOut]:
        tasks = {
            n: asyncio.create_task(
                self.translator.translate(self.fmt.encode(p.with_terms), p.with_terms)
            )
            for n, p in items.items()
        }
        done, pending = await asyncio.wait(
            tasks.values(), timeout=self.settings.live_budget_seconds
        )
        for task in pending:
            task.cancel()
        out: dict[int, SegmentOut] = {}
        for n, task in tasks.items():
            p = items[n]
            if task in pending:
                out[n] = self._defer(site, p, "live_budget_exceeded")
                continue
            try:
                raw = task.result()
            except UpstreamError as err:
                self._enqueue(site, p)  # retry in the background
                out[n] = self._fail(p.seg_in, p.keys, Status.UPSTREAM_ERROR, type(err).__name__)
                continue
            except Exception as err:  # noqa: BLE001 - an unexpected client error is an upstream error
                out[n] = self._fail(p.seg_in, p.keys, Status.UPSTREAM_ERROR, type(err).__name__)
                continue
            out[n] = self._accept_model_output(p, raw)
        return out

    def _accept_model_output(self, p: _Prepared, raw: str) -> SegmentOut:
        try:
            decoded = self.fmt.decode(raw, p.with_terms)
            final = self._finalise(p, decoded)
        except SegmentError as err:
            return self._fail(p.seg_in, p.keys, _status_for(err), _reason(err))
        try:
            self.store.record_machine(
                keys=p.keys,
                masked_source=p.with_terms.to_wire(),
                masked_target=decoded.to_wire(),  # masked: entity values never stored (FR-143)
                model_version=self.translator.model_version,
                term_ids=sorted({t.term_id for t in p.terms.values()}),
                tag_integrity=True,
            )
        except Exception as err:  # noqa: BLE001 - serving still works; storage retries later
            log.warning("could not store machine translation: %s", type(err).__name__)
        return self._ok(p, final, Origin.MT)

    # -- results --------------------------------------------------------------

    def _ok(self, p: _Prepared, final: Segment, origin: Origin) -> SegmentOut:
        self.metrics.statuses[Status.TRANSLATED] += 1
        return SegmentOut(
            p.seg_in.id, final.to_wire(), Status.TRANSLATED, p.keys.segment_key, origin
        )

    def _fail(
        self, seg_in: SegmentIn, keys: SegmentKeys | None, status: Status, reason: str
    ) -> SegmentOut:
        self.metrics.statuses[status] += 1
        self.metrics.reasons[reason] += 1
        return SegmentOut(
            seg_in.id, seg_in.text, status, keys.segment_key if keys else None, None, reason
        )

    @staticmethod
    def _ordered(segments: list[SegmentIn], out: dict[int, SegmentOut]) -> list[SegmentOut]:
        return [out[n] for n in range(len(segments))]


def _status_for(err: SegmentError) -> Status:
    if isinstance(err, EntityCheckError):
        return Status.ENTITY_CHECK_FAILED
    if isinstance(err, GlossaryCheckError):
        return Status.GLOSSARY_TERM_MISSING
    return Status.TAG_FALLBACK


def _reason(err: SegmentError) -> str:
    return getattr(err, "reason", None) or err.cause
