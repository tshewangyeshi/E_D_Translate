"""Per-segment outcome of a translation request (S2.1). Requirements: FR-104, FR-611.

Its own module so that metrics can name these values without importing the
service that produces them.
"""

from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    TRANSLATED = "translated"
    PENDING_MT = "pending_mt"  # the only non-final status
    TIER_BLOCKED = "tier_blocked"
    ENTITY_CHECK_FAILED = "entity_check_failed"
    GLOSSARY_TERM_MISSING = "glossary_term_missing"
    TAG_FALLBACK = "tag_fallback"
    UPSTREAM_ERROR = "upstream_error"
