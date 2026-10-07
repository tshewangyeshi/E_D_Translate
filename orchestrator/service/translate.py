"""Translate a batch of widget segments (backlog S2.1).

Requirements: FR-100, FR-104, FR-122, FR-140..143, FR-155, FR-156, FR-400, FR-401,
FR-510, FR-511, FR-512, FR-610, FR-611, NFR-304, NFR-410, NFR-412.

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
                                                                   (tier 2: flag for review;
                                                                    store fails: queue it)
                                                                 invalid   ─► entity/tag/term fail
                                                                 upstream  ─► upstream_error+enqueue
                                                                 too slow  ─► enqueue, pending_mt

Every failure returns the source text (NFR-410). Nothing here raises for a
translation failure; the API never turns one into a 5xx.

Tier 2 machine output is flagged for review wherever it is served, cache hits
included (FR-511): the same text may first have been stored for a Tier 3 page,
and keys carry no tier.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, replace

from orchestrator.governance.sites import Site, resolve_tier
from orchestrator.ops.health import UpstreamHealth
from orchestrator.ops.metrics import MODEL_OUTPUT_OK, Metrics
from orchestrator.pipeline.glossary import (
    TERM_KIND,
    GlossaryCheckError,
    Termbase,
    TermMap,
    fingerprint,
    restore_terms,
    substitute,
)
from orchestrator.pipeline.protect import EntityCheckError, EntityMap, mask, restore
from orchestrator.pipeline.segment import Entity, ModelFormat, Segment, SegmentError, parse
from orchestrator.pipeline.tags import check_tags, collapse_formatting
from orchestrator.queue.jobs import PRIORITY_LIVE_DEFERRED, Job, JobQueue
from orchestrator.service.model_call import calls_needed, translate_segment
from orchestrator.service.status import Status
from orchestrator.store.keys import SegmentKeys, Versions, keys_for
from orchestrator.store.lookup import Hit, LookupItem, TranslationStore
from orchestrator.store.models import Origin, RaisedBy, ReviewRequest
from orchestrator.upstream.errors import UpstreamError
from orchestrator.upstream.quota import QuotaManager
from orchestrator.upstream.translator import Translator

__all__ = ["SegmentIn", "SegmentOut", "ServiceSettings", "Status", "TranslateService"]

log = logging.getLogger(__name__)


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
    #: The model answered for this segment in this request and its output was
    #: checked, whatever the outcome: the denominator of tag integrity (S1.5).
    model_checked: bool = False


@dataclass
class ServiceSettings:
    live_budget_seconds: float = 1.5  # FR-155
    source_lang: str = "en"
    target_lang: str = "dz"
    #: Entity kinds left for the model to translate, values checked (FR-144).
    #: Empty means every number is masked and restored byte-identical (FR-140).
    translate_kinds: frozenset[str] = frozenset()


@dataclass
class _Prepared:
    seg_in: SegmentIn
    source: Segment
    with_terms: Segment
    entities: EntityMap
    terms: TermMap
    keys: SegmentKeys
    tier: int


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
        self.upstream = UpstreamHealth()  # the model, as this process has seen it (FR-610)

    # -- public ---------------------------------------------------------------

    async def translate(
        self,
        site: Site,
        client_hash: str,
        segments: list[SegmentIn],
        path: object = None,
        *,
        markup: bool = False,
    ) -> list[SegmentOut]:
        """Translate a batch. ``markup`` is for callers that write new markup
        (proxy, CMS, ``/v1/translate/html``): a tag-integrity failure there
        serves the translation with its formatting collapsed (FR-123) instead of
        English. The widget never sets it (FR-210).

        Store work (lookups, writes, the queue) runs in a worker thread, so a
        slow database holds up this request only, not every request this process
        is serving (S2.4). Model calls stay on the event loop."""
        out, prepared, live = await asyncio.to_thread(self._plan, site, client_hash, segments, path)
        if live:
            out.update(await self._translate_live(site, {n: prepared[n] for n in live}, markup))
        return self._ordered(segments, out)

    def _plan(
        self, site: Site, client_hash: str, segments: list[SegmentIn], path: object
    ) -> tuple[dict[int, SegmentOut], dict[int, _Prepared], list[int]]:
        """Everything before the model: answers so far, and which segments go live."""
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
            return out, prepared, []

        live: list[int] = []
        for (n, p), hit in zip(prepared.items(), hits, strict=True):
            if hit is not None:
                out[n] = self._serve_hit(site, p, hit)
            elif p.tier == 1:
                out[n] = self._fail(
                    p.seg_in, p.keys, Status.TIER_BLOCKED, "no_approved_translation"
                )
            elif not self._safe_persist(p, client_hash):
                out[n] = self._fail(
                    p.seg_in, p.keys, Status.PENDING_MT, "awaiting_distinct_clients"
                )
            elif self.quota.try_live(calls_needed(p.with_terms)):  # one token per model call
                live.append(n)
            else:
                out[n] = self._defer(site, p, "no_live_quota")
        return out, prepared, live

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
            review=p.tier == 2,  # FR-511: the worker flags what it stores for Tier 2
        )

    def _prepare(self, site: Site, seg_in: SegmentIn, path: object = None) -> _Prepared:
        source = parse(seg_in.text)  # client input: entity tokens are rejected (S1.2)
        masked, entities = mask(source, self.settings.translate_kinds)
        with_terms, terms = substitute(masked, self.termbase)
        keys = keys_for(with_terms, fingerprint(terms.values()), self.versions)
        tier = resolve_tier(site, seg_in.tier, seg_in.selector_tier, path)
        return _Prepared(seg_in, source, with_terms, entities, terms, keys, tier)

    def _finalise(self, p: _Prepared, model_output: Segment) -> Segment:
        """Entities restored byte-identical, tags exact, terms restored. Raises on any failure.

        Entities and terms first, so a duplicated entity or term is reported as
        entity_check_failed / glossary_term_missing, not as a generic tag failure.
        """
        restored = self._restore(p, model_output)
        check_tags(restored, p.with_terms)
        return restored

    def _restore(self, p: _Prepared, model_output: Segment) -> Segment:
        restored = restore(model_output, p.with_terms, p.entities)
        return restore_terms(restored, p.with_terms, p.terms)

    def _serve_hit(self, site: Site, p: _Prepared, hit: Hit) -> SegmentOut:
        try:
            stored = parse(hit.stored.masked_target, allow_entities=True)
            final = self._finalise(p, stored)
        except SegmentError as err:
            # Stored output is validated before storage; failing now means drift. Never serve it.
            log.error("stored translation failed re-validation: %s", err.cause)
            return self._fail(p.seg_in, p.keys, _status_for(err), f"stored:{_reason(err)}")
        if p.tier == 2:
            self.store.ensure_review(p.keys, hit.stored, site.site_id)  # FR-511; never raises
        return self._ok(p, final, hit.stored.origin)

    def _safe_persist(self, p: _Prepared, client_hash: str) -> bool:
        try:
            return self.store.should_persist(p.keys, client_hash)
        except Exception:  # noqa: BLE001 - fail safe: do not persist or translate (NFR-304)
            return False

    def _enqueue(self, site: Site, p: _Prepared) -> bool:
        """Queue for the worker. Tier 2 jobs carry the review flag (FR-511)."""
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
        self, site: Site, items: dict[int, _Prepared], markup: bool = False
    ) -> dict[int, SegmentOut]:
        tasks = {
            n: asyncio.create_task(translate_segment(self.translator, self.fmt, p.with_terms))
            for n, p in items.items()
        }
        done, pending = await asyncio.wait(
            tasks.values(), timeout=self.settings.live_budget_seconds
        )
        for task in pending:
            task.cancel()
        # Checking and storing the answers is store work: off the event loop too.
        return await asyncio.to_thread(self._settle, site, items, tasks, pending, markup)

    def _settle(
        self,
        site: Site,
        items: dict[int, _Prepared],
        tasks: dict[int, asyncio.Task[Segment]],
        pending: set[asyncio.Task[Segment]],
        markup: bool,
    ) -> dict[int, SegmentOut]:
        out: dict[int, SegmentOut] = {}
        for n, task in tasks.items():
            p = items[n]
            if task in pending:
                self._upstream_failed("timeout")
                out[n] = self._defer(site, p, "live_budget_exceeded")
                continue
            try:
                decoded = task.result()
            except SegmentError as err:
                # The model answered, but its markers could not be read: nothing
                # further was checked, the glossary included.
                self.upstream.succeeded()
                self.metrics.model_outputs[Status.TAG_FALLBACK.value] += 1
                out[n] = replace(
                    self._fail(p.seg_in, p.keys, Status.TAG_FALLBACK, _reason(err)),
                    model_checked=True,
                )
                continue
            except UpstreamError as err:
                self._upstream_failed(type(err).__name__)
                self._enqueue(site, p)  # retry in the background
                out[n] = self._fail(p.seg_in, p.keys, Status.UPSTREAM_ERROR, type(err).__name__)
                continue
            except Exception as err:  # noqa: BLE001 - an unexpected client error is an upstream error
                self._upstream_failed(type(err).__name__)
                out[n] = self._fail(p.seg_in, p.keys, Status.UPSTREAM_ERROR, type(err).__name__)
                continue
            if calls_needed(p.with_terms):
                self.upstream.succeeded()
            out[n] = replace(
                self._accept_model_output(site, p, decoded, markup), model_checked=True
            )
        return out

    def _upstream_failed(self, kind: str) -> None:
        self.upstream.failed(kind)
        self.metrics.upstream_errors[kind] += 1

    def _accept_model_output(
        self, site: Site, p: _Prepared, decoded: Segment, markup: bool = False
    ) -> SegmentOut:
        try:
            final = self._finalise(p, decoded)
        except SegmentError as err:
            status = _status_for(err)
            if markup and status is Status.TAG_FALLBACK:
                collapsed = self._collapse(p, decoded)
                if collapsed is not None:
                    # Served, never stored: the widget shares these keys and must
                    # never receive restructured formatting (FR-210).
                    self.metrics.model_outputs[status.value] += 1
                    self.metrics.statuses[status] += 1
                    self.metrics.reasons["formatting_collapsed"] += 1
                    return SegmentOut(
                        p.seg_in.id,
                        collapsed.to_wire(),
                        status,
                        p.keys.segment_key,
                        Origin.MT,
                        "formatting_collapsed",
                    )
            self.metrics.model_outputs[status.value] += 1
            # Entities are checked before terms, so an entity failure says
            # nothing about the glossary either way and is left out of its rate.
            # Any later failure means the terms themselves were checked.
            if status is not Status.ENTITY_CHECK_FAILED:
                self._record_terms(p, decoded)
            return self._fail(p.seg_in, p.keys, status, _reason(err))
        self.metrics.model_outputs[MODEL_OUTPUT_OK] += 1
        self._record_terms(p, decoded)
        review = ReviewRequest(site.site_id, RaisedBy.REQUEST) if p.tier == 2 else None
        try:
            self.store.record_machine(
                keys=p.keys,
                masked_source=p.with_terms.to_wire(),
                masked_target=decoded.to_wire(),  # masked: entity values never stored (FR-143)
                model_version=self.translator.model_version,
                term_ids=sorted({t.term_id for t in p.terms.values()}),
                tag_integrity=True,
                # FR-511: what a citizen is shown at Tier 2 is what a reviewer is asked to check.
                review=review,
            )
        except Exception as err:  # noqa: BLE001 - the citizen still gets the translation
            # Nothing was stored, the review item included. Serve it anyway (a
            # page is not failed for a storage fault) and queue the segment: the
            # worker stores and, for Tier 2, flags it once storage is back.
            log.warning("could not store machine translation: %s", type(err).__name__)
            self.metrics.store_failures += 1
            self._enqueue(site, p)
        return self._ok(p, final, Origin.MT)

    def _collapse(self, p: _Prepared, decoded: Segment) -> Segment | None:
        """FR-123: entities and terms still exact, formatting applied to the whole."""
        try:
            return collapse_formatting(self._restore(p, decoded), p.with_terms)
        except SegmentError:
            return None  # entities or terms failed too: English, as for the widget

    def _record_terms(self, p: _Prepared, output: Segment) -> None:
        """Glossary compliance per term (FR-402): each sent term back exactly once."""
        sent = {t.id for t in p.with_terms.tokens if isinstance(t, Entity) and t.kind == TERM_KIND}
        back = Counter(t.id for t in output.tokens if isinstance(t, Entity) and t.kind == TERM_KIND)
        self.metrics.terms.found += len(sent)
        self.metrics.terms.restored += sum(1 for tid in sent if back[tid] == 1)

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
