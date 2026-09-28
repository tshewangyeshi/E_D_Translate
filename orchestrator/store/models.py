"""Translation-memory data model (spec §2.6, ER-14). Requirements: FR-410, FR-412, FR-143.

    segment              one row per distinct MASKED source (no tier: tier belongs
                         to the request, the same sentence appears on Tier 1 and 2 pages)
    translation_version  immutable: every machine output and every approval is a new row
    review_item          mutable workflow state; points at the current approved version

Only ``invalidated_at`` ever changes on a translation_version (glossary or model
invalidation, retention); content is never overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Origin(StrEnum):
    MT = "mt"
    HUMAN = "human"


class ReviewState(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    NEEDS_RECHECK = "needs_recheck"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Stored:
    """What a lookup returns: masked target text plus provenance. Never real entity values."""

    version_id: int
    origin: Origin
    masked_target: str


@dataclass(frozen=True)
class TranslationVersion:
    id: int
    segment_key: str
    lookup_key: str
    origin: Origin
    masked_target: str
    gfp: str
    model_version: str | None
    author: str | None
    tag_integrity: bool | None
    created_at: datetime
    invalidated_at: datetime | None = None


@dataclass(frozen=True)
class ReviewItem:
    segment_key: str
    approved_key: str | None
    state: ReviewState
    current_version: int | None
    site_id: str | None
    updated_at: datetime


@dataclass(frozen=True)
class Invalidation:
    """Result of a termbase publish (FR-153)."""

    machine_keys: tuple[str, ...]  # purge from cache; re-warm rate-capped
    approved_keys: tuple[str, ...]  # purge from cache
    recheck_segments: tuple[str, ...]  # review_item -> needs_recheck, not served


@dataclass(frozen=True)
class MigrationReport:
    """Result of a pipeline_version change (FR-154)."""

    rekeyed: int
    needs_recheck: int
