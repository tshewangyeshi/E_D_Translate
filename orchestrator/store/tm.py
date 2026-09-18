"""Translation-memory interface and in-memory implementation.

Requirements: FR-410, FR-411, FR-412, FR-143, FR-153, FR-154, NFR-305.

The PostgreSQL implementation (``postgres_tm.py``) must pass the same contract
tests (``tests/orchestrator/store/test_tm_contract.py``), so behaviour cannot
drift between the in-memory version used by unit tests and production.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from orchestrator.store.models import (
    Invalidation,
    MigrationReport,
    Origin,
    ReviewItem,
    ReviewState,
    Stored,
    TranslationVersion,
)

#: Given (segment_key, masked_source) return the new approved key, or None when
#: the new pipeline masks the source differently (the approval needs a recheck).
RekeyFn = Callable[[str, str, str], str | None]


class TranslationMemory(Protocol):
    def lookup(
        self, approved_keys: Sequence[str], machine_keys: Sequence[str]
    ) -> tuple[dict[str, Stored], dict[str, Stored]]:
        """ONE batched read (ER-21): current approved and machine translations by key."""
        ...

    def store_machine(
        self,
        *,
        segment_key: str,
        masked_source: str,
        machine_key: str,
        masked_target: str,
        gfp: str,
        model_version: str,
        term_ids: Iterable[str],
        tag_integrity: bool | None,
    ) -> Stored: ...

    def approve(
        self,
        *,
        segment_key: str,
        masked_source: str,
        approved_key: str,
        masked_target: str,
        gfp: str,
        author: str,
        term_ids: Iterable[str],
        site_id: str | None = None,
    ) -> Stored: ...

    def history(self, segment_key: str) -> list[TranslationVersion]: ...

    def review_item(self, segment_key: str) -> ReviewItem | None: ...

    def invalidate_terms(self, term_ids: Iterable[str]) -> Invalidation: ...

    def migrate_pipeline(self, rekey: RekeyFn) -> MigrationReport: ...

    def expire_machine(self, before: datetime) -> int: ...


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryTM:
    """Reference implementation for unit tests. Same semantics as PostgresTM."""

    def __init__(self) -> None:
        self._versions: dict[int, TranslationVersion] = {}
        self._segments: dict[str, str] = {}  # segment_key -> masked_source
        self._reviews: dict[str, ReviewItem] = {}
        # (term_id, segment_key, gfp): robust to re-keying after a pipeline change
        self._hits: set[tuple[str, str, str]] = set()
        self._next_id = 1
        self.lookup_calls = 0

    # -- reads ---------------------------------------------------------------

    def lookup(
        self, approved_keys: Sequence[str], machine_keys: Sequence[str]
    ) -> tuple[dict[str, Stored], dict[str, Stored]]:
        self.lookup_calls += 1
        wanted_a, wanted_m = set(approved_keys), set(machine_keys)
        approved: dict[str, Stored] = {}
        for item in self._reviews.values():
            if (
                item.state is ReviewState.APPROVED
                and item.approved_key in wanted_a
                and item.current_version is not None
            ):
                v = self._versions[item.current_version]
                approved[item.approved_key] = Stored(v.id, v.origin, v.masked_target)
        machine: dict[str, Stored] = {}
        for v in sorted(self._versions.values(), key=lambda v: v.id):
            if v.origin is Origin.MT and v.lookup_key in wanted_m and v.invalidated_at is None:
                machine[v.lookup_key] = Stored(v.id, v.origin, v.masked_target)  # latest wins
        return approved, machine

    def history(self, segment_key: str) -> list[TranslationVersion]:
        return sorted(
            (v for v in self._versions.values() if v.segment_key == segment_key),
            key=lambda v: v.id,
        )

    def review_item(self, segment_key: str) -> ReviewItem | None:
        return self._reviews.get(segment_key)

    # -- writes --------------------------------------------------------------

    def _add(self, **fields: object) -> TranslationVersion:
        v = TranslationVersion(id=self._next_id, created_at=_now(), **fields)  # type: ignore[arg-type]
        self._versions[v.id] = v
        self._next_id += 1
        return v

    def store_machine(
        self,
        *,
        segment_key: str,
        masked_source: str,
        machine_key: str,
        masked_target: str,
        gfp: str,
        model_version: str,
        term_ids: Iterable[str],
        tag_integrity: bool | None,
    ) -> Stored:
        self._segments.setdefault(segment_key, masked_source)
        v = self._add(
            segment_key=segment_key,
            lookup_key=machine_key,
            origin=Origin.MT,
            masked_target=masked_target,
            gfp=gfp,
            model_version=model_version,
            author=None,
            tag_integrity=tag_integrity,
        )
        for term in term_ids:
            self._hits.add((term, segment_key, gfp))
        return Stored(v.id, v.origin, v.masked_target)

    def approve(
        self,
        *,
        segment_key: str,
        masked_source: str,
        approved_key: str,
        masked_target: str,
        gfp: str,
        author: str,
        term_ids: Iterable[str],
        site_id: str | None = None,
    ) -> Stored:
        self._segments.setdefault(segment_key, masked_source)
        v = self._add(
            segment_key=segment_key,
            lookup_key=approved_key,
            origin=Origin.HUMAN,
            masked_target=masked_target,
            gfp=gfp,
            model_version=None,
            author=author,
            tag_integrity=True,
        )
        previous = self._reviews.get(segment_key)
        self._reviews[segment_key] = ReviewItem(
            segment_key=segment_key,
            approved_key=approved_key,
            state=ReviewState.APPROVED,
            current_version=v.id,
            site_id=site_id if site_id is not None else (previous.site_id if previous else None),
            updated_at=_now(),
        )
        for term in term_ids:
            self._hits.add((term, segment_key, gfp))
        return Stored(v.id, v.origin, v.masked_target)

    def invalidate_terms(self, term_ids: Iterable[str]) -> Invalidation:
        terms = set(term_ids)
        affected = {(s, g) for t, s, g in self._hits if t in terms}
        machine: set[str] = set()
        approved: set[str] = set()
        recheck: set[str] = set()
        now = _now()
        for vid, v in list(self._versions.items()):
            if (
                v.origin is Origin.MT
                and (v.segment_key, v.gfp) in affected
                and not v.invalidated_at
            ):
                self._versions[vid] = replace(v, invalidated_at=now)
                machine.add(v.lookup_key)
        for seg, item in list(self._reviews.items()):
            if item.state is not ReviewState.APPROVED or item.current_version is None:
                continue
            if (seg, self._versions[item.current_version].gfp) in affected:
                self._reviews[seg] = replace(item, state=ReviewState.NEEDS_RECHECK, updated_at=now)
                approved.add(item.approved_key or "")
                recheck.add(seg)
        return Invalidation(tuple(sorted(machine)), tuple(sorted(approved)), tuple(sorted(recheck)))

    def migrate_pipeline(self, rekey: RekeyFn) -> MigrationReport:
        rekeyed = rechecked = 0
        now = _now()
        for seg, item in list(self._reviews.items()):
            if item.state is not ReviewState.APPROVED or item.current_version is None:
                continue
            gfp = self._versions[item.current_version].gfp
            new_key = rekey(seg, self._segments[seg], gfp)
            if new_key is None:
                self._reviews[seg] = replace(item, state=ReviewState.NEEDS_RECHECK, updated_at=now)
                rechecked += 1
            elif new_key != item.approved_key:
                self._reviews[seg] = replace(item, approved_key=new_key, updated_at=now)
                rekeyed += 1
        return MigrationReport(rekeyed, rechecked)

    def expire_machine(self, before: datetime) -> int:
        """Retention (NFR-305): invalidate machine translations created before ``before``."""
        count = 0
        for vid, v in list(self._versions.items()):
            if v.origin is Origin.MT and v.invalidated_at is None and v.created_at < before:
                self._versions[vid] = replace(v, invalidated_at=_now())
                count += 1
        return count
