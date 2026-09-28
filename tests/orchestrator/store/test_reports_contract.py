"""Error-report store contract (S3.5). Requirements: FR-430, FR-432, NFR-303.

Run against the in-memory store and, when PostgreSQL is reachable, the real
one. The in-memory version exists to make unit tests fast, which is only
worth anything if it behaves identically -- so both go through the same tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator.store.reports import (
    MAX_REPORTS_PER_SEGMENT_PER_DAY,
    ErrorReport,
    segment_is_saturated,
)

KEY = "c" * 64
OTHER = "d" * 64


def test_fr430_a_report_round_trips(report_store: Any) -> None:
    report_store.record_report(ErrorReport(KEY, "portal", "wrong_term", "the fee word is wrong"))
    since = datetime.now(UTC) - timedelta(hours=1)
    assert report_store.count_reports_since(KEY, since) == 1


def test_fr430_counts_are_per_segment(report_store: Any) -> None:
    report_store.record_report(ErrorReport(KEY, "portal", "other"))
    report_store.record_report(ErrorReport(KEY, "portal", "other"))
    report_store.record_report(ErrorReport(OTHER, "portal", "other"))
    since = datetime.now(UTC) - timedelta(hours=1)
    assert report_store.count_reports_since(KEY, since) == 2
    assert report_store.count_reports_since(OTHER, since) == 1


def test_fr432_counts_respect_the_window(report_store: Any) -> None:
    report_store.record_report(ErrorReport(KEY, "portal", "other"))
    future = datetime.now(UTC) + timedelta(hours=1)
    assert report_store.count_reports_since(KEY, future) == 0


def test_fr432_saturation_is_reached_at_the_cap(report_store: Any) -> None:
    now = datetime.now(UTC)
    for _ in range(MAX_REPORTS_PER_SEGMENT_PER_DAY - 1):
        report_store.record_report(ErrorReport(KEY, "portal", "other"))
    assert not segment_is_saturated(report_store, KEY, now)
    report_store.record_report(ErrorReport(KEY, "portal", "other"))
    assert segment_is_saturated(report_store, KEY, now)


def test_fr430_an_optional_comment_may_be_absent(report_store: Any) -> None:
    report_store.record_report(ErrorReport(KEY, "portal", "formatting", None))
    assert report_store.count_reports_since(KEY, datetime.now(UTC) - timedelta(hours=1)) == 1


@pytest.mark.parametrize(
    "reason", ["wrong_meaning", "wrong_term", "not_translated", "formatting", "offensive", "other"]
)
def test_fr430_every_declared_reason_is_accepted(report_store: Any, reason: str) -> None:
    """The database CHECK constraint and the Python list must agree."""
    report_store.record_report(ErrorReport(KEY, "portal", reason))
    assert report_store.count_reports_since(KEY, datetime.now(UTC) - timedelta(hours=1)) == 1
