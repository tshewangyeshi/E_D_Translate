"""Translation-memory interface and in-memory implementation.

Requirements: FR-410, FR-411, FR-412, FR-143, FR-153, FR-154, FR-511, NFR-305.

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
    OPEN_REVIEW_STATES,
    FlagOutcome,
    Invalidation,
    MigrationReport,
    Origin,
    RaisedBy,
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
        """ONE batched read (ER-21): current approved and machine translations by key.

        Machine results carry ``review``: whether the segment has a review item,
        so a Tier 2 serve knows whether it still has to flag it (FR-511).
        """
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

    def flag_for_review(
        self,
        *,
        segment_key: str,
        version_id: int,
        site_id: str,
        raised_by: RaisedBy,
        daily_cap: int | None,
        since: datetime,
    ) -> FlagOutcome:
        """Make sure a reviewer will be asked about this machine output (FR-511).

        * No review item yet: open one in ``pending_review``, or in ``owed`` when
          the site has already opened ``daily_cap`` request-raised items since
          ``since`` (``None`` means uncapped). Nothing is ever dropped.
        * An open item (pending or owed): re-point it at this version if it is
          newer, so the reviewer checks what citizens are shown now.
        * Any other item (approved, needs recheck, rejected) is left alone:
          flagging never reopens a decision a reviewer made.
        """
        ...

    def release_owed(self, site_id: str, limit: int) -> int:
        """Move up to ``limit`` of a site's owed items, oldest first, into the queue."""
        ...

    def owed(self, site_id: str | None = None) -> int:
        """How many items are owed, for one site or for all."""
        ...

    def history(self, segment_key: str) -> list[TranslationVersion]: ...

    def review_item(self, segment_key: str) -> ReviewItem | None: ...

    def pending_review(self, site_id: str | None = None) -> int:
        """How many items are waiting for a reviewer, for one site or for all."""
        ...

    def invalidate_terms(self, term_ids: Iterable[str]) -> Invalidation: ...

    def migrate_pipeline(self, rekey: RekeyFn) -> MigrationReport: ...

    def expire_machine(self, before: datetime) -> list[str]:
        """Invalidate old machine translations; the lookup keys, so caches can drop them."""
        ...

    def count_machine_before(self, before: datetime) -> int: ...


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
                approved[item.approved_key] = Stored(v.id, v.origin, v.masked_target, True)
        machine: dict[str, Stored] = {}
        for v in sorted(self._versions.values(), key=lambda v: v.id):
            if v.origin is Origin.MT and v.lookup_key in wanted_m and v.invalidated_at is None:
                reviewed = v.segment_key in self._reviews
                machine[v.lookup_key] = Stored(v.id, v.origin, v.masked_target, reviewed)
        return approved, machine  # latest machine version wins

    def history(self, segment_key: str) -> list[TranslationVersion]:
        return sorted(
            (v for v in self._versions.values() if v.segment_key == segment_key),
            key=lambda v: v.id,
        )

    def review_item(self, segment_key: str) -> ReviewItem | None:
        return self._reviews.get(segment_key)

    def pending_review(self, site_id: str | None = None) -> int:
        return self._count(ReviewState.PENDING_REVIEW, site_id)

    def owed(self, site_id: str | None = None) -> int:
        return self._count(ReviewState.OWED, site_id)

    def _count(self, state: ReviewState, site_id: str | None) -> int:
        return sum(
            1
            for item in self._reviews.values()
            if item.state is state and (site_id is None or item.site_id == site_id)
        )

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
        now = _now()
        self._reviews[segment_key] = ReviewItem(
            segment_key=segment_key,
            approved_key=approved_key,
            state=ReviewState.APPROVED,
            current_version=v.id,
            site_id=site_id if site_id is not None else (previous.site_id if previous else None),
            updated_at=now,
            created_at=previous.created_at if previous else now,
            raised_by=previous.raised_by if previous else RaisedBy.OPERATOR,
        )
        for term in term_ids:
            self._hits.add((term, segment_key, gfp))
        return Stored(v.id, v.origin, v.masked_target)

    def flag_for_review(
        self,
        *,
        segment_key: str,
        version_id: int,
        site_id: str,
        raised_by: RaisedBy,
        daily_cap: int | None,
        since: datetime,
    ) -> FlagOutcome:
        existing = self._reviews.get(segment_key)
        if existing is not None:
            newer = (existing.current_version or 0) < version_id
            if existing.state in OPEN_REVIEW_STATES and newer:
                self._reviews[segment_key] = replace(
                    existing, current_version=version_id, updated_at=_now()
                )
            return FlagOutcome.EXISTS
        state = ReviewState.PENDING_REVIEW
        if daily_cap is not None:
            opened = sum(
                1
                for item in self._reviews.values()
                if item.site_id == site_id
                and item.raised_by is RaisedBy.REQUEST
                and item.state is not ReviewState.OWED
                and item.created_at is not None
                and item.created_at >= since
            )
            if opened >= daily_cap:
                state = ReviewState.OWED
        now = _now()
        self._reviews[segment_key] = ReviewItem(
            segment_key=segment_key,
            approved_key=None,
            state=state,
            current_version=version_id,
            site_id=site_id,
            updated_at=now,
            created_at=now,
            raised_by=raised_by,
        )
        return FlagOutcome.OWED if state is ReviewState.OWED else FlagOutcome.CREATED

    def release_owed(self, site_id: str, limit: int) -> int:
        owed = sorted(
            (
                i
                for i in self._reviews.values()
                if i.state is ReviewState.OWED and i.site_id == site_id
            ),
            key=lambda i: (i.created_at or _now(), i.segment_key),
        )[: max(0, limit)]
        now = _now()
        for item in owed:
            self._reviews[item.segment_key] = replace(
                item, state=ReviewState.PENDING_REVIEW, updated_at=now
            )
        return len(owed)

    def _close_invalidated(self) -> None:
        """Open items whose version is no longer served ask about nothing: remove them."""
        for seg, item in list(self._reviews.items()):
            if item.state in OPEN_REVIEW_STATES and item.current_version is not None:
                if self._versions[item.current_version].invalidated_at is not None:
                    del self._reviews[seg]

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
        self._close_invalidated()
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

    def expire_machine(self, before: datetime) -> list[str]:
        """Retention (NFR-305): invalidate machine translations created before ``before``."""
        keys = []
        for vid, v in list(self._versions.items()):
            if self._expirable(v, before):
                self._versions[vid] = replace(v, invalidated_at=_now())
                keys.append(v.lookup_key)
        self._close_invalidated()
        return keys

    def count_machine_before(self, before: datetime) -> int:
        """What ``expire_machine`` would invalidate, without invalidating it."""
        return sum(1 for v in self._versions.values() if self._expirable(v, before))

    @staticmethod
    def _expirable(v: TranslationVersion, before: datetime) -> bool:
        return v.origin is Origin.MT and v.invalidated_at is None and v.created_at < before
