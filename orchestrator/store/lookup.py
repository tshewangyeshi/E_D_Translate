"""Tier-gated lookup over cache and translation memory (backlog S1.7).

Requirements: FR-411, FR-421, FR-510, FR-143, FR-151, FR-153, NFR-304, NFR-305, NFR-410.

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
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from orchestrator.store.cache import APPROVED_NS, MACHINE_NS, Cache, SeenCounter
from orchestrator.store.keys import SegmentKeys
from orchestrator.store.models import Invalidation, Origin, Stored
from orchestrator.store.tm import TranslationMemory


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
    source: str  # "cache" | "tm"


@dataclass
class StoreSettings:
    machine_ttl_seconds: int = 7 * 86_400
    approved_ttl_seconds: int | None = None  # approvals stay until invalidated
    distinct_clients_to_persist: int = 3  # NFR-304


class TranslationStore:
    def __init__(
        self,
        tm: TranslationMemory,
        cache: Cache,
        seen: SeenCounter,
        settings: StoreSettings | None = None,
    ) -> None:
        self.tm = tm
        self.cache = cache
        self.seen = seen
        self.settings = settings or StoreSettings()

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
                results[n] = Hit(a, "cache")
                continue
            if tier >= 2:
                # Safe to serve without asking the TM for an approval: approve() evicts the
                # machine entry, and Redis runs volatile-lru, so approvals (no TTL) are never
                # evicted while a machine entry (TTL) survives. See docs/02-technical-spec.md.
                m = cached.get(MACHINE_NS + item.keys.machine_key)
                if m is not None and m.origin is Origin.MT:
                    results[n] = Hit(m, "cache")
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
                    results[n] = Hit(a2, "tm")
                    self.cache.set(
                        APPROVED_NS + item.keys.approved_key, a2, self.settings.approved_ttl_seconds
                    )
                elif tier >= 2:
                    m2 = tm_m.get(item.keys.machine_key)
                    if m2 is not None and m2.origin is Origin.MT:
                        results[n] = Hit(m2, "tm")
                        self.cache.set(
                            MACHINE_NS + item.keys.machine_key,
                            m2,
                            self.settings.machine_ttl_seconds,
                        )
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
    ) -> Stored:
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
        self.cache.set(MACHINE_NS + keys.machine_key, stored, self.settings.machine_ttl_seconds)
        return stored

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
        self.cache.set(APPROVED_NS + keys.approved_key, stored, self.settings.approved_ttl_seconds)
        self.cache.delete_many([MACHINE_NS + keys.machine_key])  # approval supersedes now (FR-421)
        return stored

    def invalidate_terms(self, term_ids: Iterable[str]) -> Invalidation:
        """Termbase publish (FR-153): exactly the affected entries, TM and cache."""
        result = self.tm.invalidate_terms(term_ids)
        self.cache.delete_many(
            [MACHINE_NS + k for k in result.machine_keys]
            + [APPROVED_NS + k for k in result.approved_keys]
        )
        return result

    def expire_machine(self, before: datetime) -> int:
        """Retention for unapproved machine translations (NFR-305)."""
        return self.tm.expire_machine(before)
