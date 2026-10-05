"""Translation-memory data model (spec §2.6, ER-14).

Requirements: FR-410, FR-412, FR-143, FR-511.

    segment              one row per distinct MASKED source (no tier: tier belongs
                         to the request, the same sentence appears on Tier 1 and 2 pages)
    translation_version  immutable: every machine output and every approval is a new row
    review_item          mutable workflow state; points at the version a reviewer
                         is asked about (pending, owed) or the approved one

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
    #: Opened past the site's daily cap: on record, not yet in the reviewers'
    #: queue. An operator releases owed items when there is room (FR-511).
    OWED = "owed"
    APPROVED = "approved"
    NEEDS_RECHECK = "needs_recheck"
    REJECTED = "rejected"


class RaisedBy(StrEnum):
    """Why a review item was opened. Only ``REQUEST`` counts against the daily cap (FR-511)."""

    REQUEST = "request"  # a page view
    OPERATOR = "operator"  # pre-warm, re-warm, approval, seed import


#: States a reviewer has not yet dealt with. New machine output re-points them.
OPEN_REVIEW_STATES = frozenset({ReviewState.PENDING_REVIEW, ReviewState.OWED})


class FlagOutcome(StrEnum):
    CREATED = "created"
    EXISTS = "exists"  # the segment already has a review item, in any state
    OWED = "owed"  # past the site's daily cap: recorded as owed, not queued


@dataclass(frozen=True)
class ReviewRequest:
    """Flag the stored machine output for review, on behalf of this site (FR-511)."""

    site_id: str
    raised_by: RaisedBy


@dataclass(frozen=True)
class Stored:
    """What a lookup returns: masked target text plus provenance. Never real entity values."""

    version_id: int
    origin: Origin
    masked_target: str
    #: A review item exists for this segment (FR-511). Only meaningful for
    #: machine output; False means "not known to exist", never "known absent".
    review: bool = False


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
    created_at: datetime | None = None
    raised_by: RaisedBy = RaisedBy.OPERATOR


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
