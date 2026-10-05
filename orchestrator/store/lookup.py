"""Tier-gated lookup over cache and translation memory (backlog S1.7).

Requirements: FR-411, FR-421, FR-510, FR-511, FR-620, FR-143, FR-151, FR-153, NFR-304,
NFR-305, NFR-410.

    for each segment:  effective tier (already resolved by the server, FR-512)
                         │
            TIER GATE ───┤  tier 1 : approved only          (FR-510, ER-1)
            (first!)     │  tier 2+: approved, then machine
                         ▼
    cache.get_many(all wanted keys) ─► hits (origin checked)
                         │ misses
                         ▼
    tm.lookup(approved_keys, machine_keys)   ONE batched read (ER-21)
                         │
                         ▼
    backfill cache ─► results: Hit | None (None = miss: live MT or pending_mt, S2.1/S2.4)

A value whose origin does not match its namespace is ignored, so machine
output can never be served from the approved namespace, and Tier 1 never
receives machine output from any path.

Writes that change what the service will say are audited in the same
transaction as the change (FR-620): if the audit record cannot be written, the
change does not happen. The whole record is validated before anything
changes, so a record that could never be written stops the change on every
backend, transaction or not.

Tier 2 machine output is flagged for review (FR-511) wherever it is shown,
not only where it is first stored: keys carry no tier, so the same text may
have been stored for a Tier 3 page first. The review bit travels with the
stored value, so the flag is written once per segment, not once per page view.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from orchestrator.governance.audit import (
    MAX_DETAIL_LIST_ITEMS,
    Action,
    AuditEvent,
    AuditLog,
    InMemoryAuditLog,
    validated,
)
from orchestrator.store.cache import APPROVED_NS, MACHINE_NS, Cache, SeenCounter
from orchestrator.store.keys import SegmentKeys
from orchestrator.store.models import (
    FlagOutcome,
    Invalidation,
    Origin,
    RaisedBy,
    ReviewRequest,
    Stored,
)
from orchestrator.store.tm import TranslationMemory

log = logging.getLogger(__name__)

#: Where a lookup was answered from; also the labels of dzweb_lookups_total.
SOURCE_CACHE = "cache"
SOURCE_TM = "tm"
MISS = "miss"

#: Counted in review_flags, beside the FlagOutcome values, when flagging failed.
FLAG_FAILED = "failed"


def gate_tier(tier: object) -> int:
    """Anything but the integers 2 or 3 is Tier 1, the most restrictive (FR-500).

    ``2.0 == 2`` in Python, and ``True == 1``: only real ints count.
    """
    if isinstance(tier, int) and not isinstance(tier, bool) and tier in (2, 3):
        return tier
    return 1


@dataclass(frozen=True)
class LookupItem:
    keys: SegmentKeys
    tier: int


@dataclass(frozen=True)
class Hit:
    stored: Stored
    source: str  # SOURCE_CACHE | SOURCE_TM


@dataclass
class StoreSettings:
    machine_ttl_seconds: int = 7 * 86_400
    approved_ttl_seconds: int | None = None  # approvals stay until invalidated
    distinct_clients_to_persist: int = 3  # NFR-304
    #: FR-511. Review items one site may open per day through page views; proposed.
    review_items_per_site_per_day: int = 500


class TranslationStore:
    def __init__(
        self,
        tm: TranslationMemory,
        cache: Cache,
        seen: SeenCounter,
        settings: StoreSettings | None = None,
        *,
        audit: AuditLog | None = None,
        atomic: Callable[[], AbstractContextManager[Any]] | None = None,
    ) -> None:
        self.tm = tm
        self.cache = cache
        self.seen = seen
        self.settings = settings or StoreSettings()
        # Never None: every audited change takes the same path in tests and in
        # production. Production wiring passes the PostgreSQL log.
        self.audit: AuditLog = audit if audit is not None else InMemoryAuditLog()
        # One database transaction around a change and its audit record.
        # In-memory stores have nothing to roll back, hence the no-op default.
        self.atomic: Callable[[], AbstractContextManager[Any]] = atomic or nullcontext
        self.review_flags: Counter[str] = Counter()  # FlagOutcome or FLAG_FAILED -> count
        self.lookups: Counter[str] = Counter()  # SOURCE_CACHE | SOURCE_TM | MISS -> count

    # -- reads ---------------------------------------------------------------

    def lookup(self, items: Sequence[LookupItem]) -> list[Hit | None]:
        tiers = [gate_tier(i.tier) for i in items]
        approved_keys = [i.keys.approved_key for i in items]
        machine_keys = [i.keys.machine_key for i, t in zip(items, tiers, strict=True) if t >= 2]

        cached = self.cache.get_many(
            [APPROVED_NS + k for k in approved_keys] + [MACHINE_NS + k for k in machine_keys]
        )
        results: list[Hit | None] = [None] * len(items)
        need_a: set[str] = set()
        need_m: set[str] = set()
        for n, (item, tier) in enumerate(zip(items, tiers, strict=True)):
            a = cached.get(APPROVED_NS + item.keys.approved_key)
            if a is not None and a.origin is Origin.HUMAN:
                results[n] = Hit(a, SOURCE_CACHE)
                continue
            if tier >= 2:
                # Safe to serve without asking the TM for an approval: approve() evicts the
                # machine entry, and Redis runs volatile-lru, so approvals (no TTL) are never
                # evicted while a machine entry (TTL) survives. See docs/02-technical-spec.md.
                m = cached.get(MACHINE_NS + item.keys.machine_key)
                if m is not None and m.origin is Origin.MT:
                    results[n] = Hit(m, SOURCE_CACHE)
                    continue
                need_m.add(item.keys.machine_key)
            need_a.add(item.keys.approved_key)

        if need_a or need_m:
            tm_a, tm_m = self.tm.lookup(sorted(need_a), sorted(need_m))
            for n, (item, tier) in enumerate(zip(items, tiers, strict=True)):
                if results[n] is not None:
                    continue
                a2 = tm_a.get(item.keys.approved_key)
                if a2 is not None and a2.origin is Origin.HUMAN:
                    results[n] = Hit(a2, SOURCE_TM)
                    self.cache.set(
                        APPROVED_NS + item.keys.approved_key, a2, self.settings.approved_ttl_seconds
                    )
                elif tier >= 2:
                    m2 = tm_m.get(item.keys.machine_key)
                    if m2 is not None and m2.origin is Origin.MT:
                        results[n] = Hit(m2, SOURCE_TM)
                        self.cache.set(
                            MACHINE_NS + item.keys.machine_key,
                            m2,
                            self.settings.machine_ttl_seconds,
                        )
        for result in results:
            self.lookups[result.source if result is not None else MISS] += 1
        return results

    # -- writes --------------------------------------------------------------

    def should_persist(self, keys: SegmentKeys, client_hash: str) -> bool:
        """NFR-304: persist or translate Tier 2 text only after N distinct clients saw it."""
        count = self.seen.observe(keys.segment_key, client_hash)
        return count >= self.settings.distinct_clients_to_persist

    def record_machine(
        self,
        *,
        keys: SegmentKeys,
        masked_source: str,
        masked_target: str,
        model_version: str,
        term_ids: Iterable[str],
        tag_integrity: bool | None,
        review: ReviewRequest | None = None,
    ) -> Stored:
        """Store machine output and, for Tier 2, flag it for review (FR-511).

        Both or neither: if the review item cannot be opened the translation is
        not stored, and the error reaches the caller. Past the site's daily cap
        the item is still opened, as owed, so nothing a citizen is shown goes
        unrecorded.
        """
        outcome: FlagOutcome | None = None
        with self.atomic():
            stored = self.tm.store_machine(
                segment_key=keys.segment_key,
                masked_source=masked_source,
                machine_key=keys.machine_key,
                masked_target=masked_target,
                gfp=keys.gfp,
                model_version=model_version,
                term_ids=term_ids,
                tag_integrity=tag_integrity,
            )
            if review is not None:
                outcome = self._flag(keys, stored, review)
                stored = replace(stored, review=True)
        if outcome is not None:
            self.review_flags[outcome.value] += 1
        self.cache.set(MACHINE_NS + keys.machine_key, stored, self.settings.machine_ttl_seconds)
        return stored

    def ensure_review(self, keys: SegmentKeys, stored: Stored, site_id: str) -> None:
        """Flag machine output a Tier 2 page is being shown, if nobody has yet (FR-511).

        Called on every Tier 2 serve of machine output whose value does not say
        it is flagged. Writes once per segment: afterwards the cached value
        carries the review bit. Never raises: a page is not failed because the
        review queue is unavailable. The failure is counted, and the next serve
        tries again.
        """
        if stored.origin is not Origin.MT or stored.review:
            return
        try:
            outcome = self._flag(keys, stored, ReviewRequest(site_id, RaisedBy.REQUEST))
        except Exception as err:  # noqa: BLE001 - serving must not depend on the review queue
            log.warning("could not flag served output for review: %s", type(err).__name__)
            self.review_flags[FLAG_FAILED] += 1
            return
        self.review_flags[outcome.value] += 1
        self.cache.set(
            MACHINE_NS + keys.machine_key,
            replace(stored, review=True),
            self.settings.machine_ttl_seconds,
        )

    def _flag(self, keys: SegmentKeys, stored: Stored, review: ReviewRequest) -> FlagOutcome:
        capped = review.raised_by is RaisedBy.REQUEST
        return self.tm.flag_for_review(
            segment_key=keys.segment_key,
            version_id=stored.version_id,
            site_id=review.site_id,
            raised_by=review.raised_by,
            daily_cap=self.settings.review_items_per_site_per_day if capped else None,
            since=datetime.now(UTC) - timedelta(days=1),
        )

    def release_owed(self, site_id: str, limit: int) -> int:
        """Release owed review items into the reviewers' queue (FR-511)."""
        return self.tm.release_owed(site_id, limit)

    def approve(
        self,
        *,
        keys: SegmentKeys,
        masked_source: str,
        masked_target: str,
        author: str,
        term_ids: Iterable[str],
        site_id: str | None = None,
    ) -> Stored:
        subject = f"segment:{keys.segment_key}"
        # Refuse before changing anything: an approval whose record could not be
        # written must not exist, on any backend, with or without a transaction.
        # Only the version id is unknown yet, and any id validates the same.
        validated(
            AuditEvent(
                author,
                Action.REVIEW_APPROVE,
                subject,
                {"version_id": 0, "site_id": site_id},
            )
        )
        with self.atomic():
            stored = self.tm.approve(
                segment_key=keys.segment_key,
                masked_source=masked_source,
                approved_key=keys.approved_key,
                masked_target=masked_target,
                gfp=keys.gfp,
                author=author,
                term_ids=term_ids,
                site_id=site_id,
            )
            self.audit.record(
                AuditEvent(
                    actor=author,
                    action=Action.REVIEW_APPROVE,
                    subject=subject,
                    detail={"version_id": stored.version_id, "site_id": site_id},
                )
            )
        # The cache is touched only once the approval and its record are both in.
        self.cache.set(APPROVED_NS + keys.approved_key, stored, self.settings.approved_ttl_seconds)
        self.cache.delete_many([MACHINE_NS + keys.machine_key])  # approval supersedes now (FR-421)
        return stored

    def invalidate_terms(
        self, term_ids: Iterable[str], *, actor: str, termbase_version: str | None = None
    ) -> Invalidation:
        """Termbase publish (FR-153): exactly the affected entries, TM and cache."""
        terms = sorted(set(term_ids))
        subject = f"termbase:{termbase_version or 'unversioned'}"

        def event(machine: int, recheck: int) -> AuditEvent:
            return AuditEvent(
                actor=actor,
                action=Action.TERMBASE_PUBLISH,
                subject=subject,
                detail={
                    "terms": terms[:MAX_DETAIL_LIST_ITEMS],
                    "term_count": len(terms),
                    "machine_invalidated": machine,
                    "approved_needing_recheck": recheck,
                },
            )

        validated(event(0, 0))  # the whole record, before anything changes
        with self.atomic():
            result = self.tm.invalidate_terms(terms)
            self.audit.record(event(len(result.machine_keys), len(result.recheck_segments)))
        self.cache.delete_many(
            [MACHINE_NS + k for k in result.machine_keys]
            + [APPROVED_NS + k for k in result.approved_keys]
        )
        return result

    def expire_machine(self, before: datetime) -> int:
        """Retention for unapproved machine translations (NFR-305)."""
        return self.tm.expire_machine(before)

    def count_machine_before(self, before: datetime) -> int:
        """Size of the next retention run, so an operator can look before leaping."""
        return self.tm.count_machine_before(before)
