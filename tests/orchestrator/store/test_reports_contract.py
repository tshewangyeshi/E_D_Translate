"""Error-report store contract (S3.5). Requirements: FR-430, FR-432, NFR-303, NFR-305.

Run against the in-memory store and, when PostgreSQL is reachable, the real
one. The in-memory version exists to make unit tests fast, which is only
worth anything if it behaves identically -- so both go through the same tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from orchestrator.store import reports as reports_module
from orchestrator.store.reports import (
    MAX_COMMENT_CHARS,
    MAX_REPORTS_PER_SEGMENT_PER_DAY,
    REASONS,
    ErrorReport,
    is_saturated,
    mask_comment,
)

CONTRACT = json.loads(
    (Path(__file__).parents[2] / "fixtures" / "feedback" / "contract.json").read_text("utf-8")
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
    report = ErrorReport(KEY, "portal", "other")
    for _ in range(MAX_REPORTS_PER_SEGMENT_PER_DAY - 1):
        report_store.record_report(report)
    assert not is_saturated(report_store, report, now)
    report_store.record_report(report)
    assert is_saturated(report_store, report, now)


def test_fr432_a_site_saturates_however_the_key_is_varied(
    report_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Varying the segment key must not buy an unbounded inbox for one site."""
    monkeypatch.setattr(reports_module, "MAX_REPORTS_PER_SITE_PER_DAY", 5)
    now = datetime.now(UTC)
    for n in range(5):
        report_store.record_report(ErrorReport(f"{n:064x}", "portal", "other"))
    assert is_saturated(report_store, ErrorReport(f"{99:064x}", "portal", "other"), now)
    assert not is_saturated(report_store, ErrorReport(f"{99:064x}", "health", "other"), now)


def test_nfr305_old_comments_are_cleared_and_the_reports_kept(report_store: Any) -> None:
    report_store.record_report(ErrorReport(KEY, "portal", "wrong_term", "the fee word"))
    report_store.record_report(ErrorReport(OTHER, "portal", "other", None))
    future = datetime.now(UTC) + timedelta(days=1)
    past = datetime.now(UTC) - timedelta(days=1)

    assert report_store.count_comments_before(past) == 0
    assert report_store.clear_comments_before(past) == 0  # recent comments stay
    assert report_store.count_comments_before(future) == 1  # the report without one is not counted
    assert report_store.clear_comments_before(future) == 1
    assert report_store.count_comments_before(future) == 0
    since = datetime.now(UTC) - timedelta(hours=1)
    assert report_store.count_reports_since(KEY, since) == 1  # the report itself is kept


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


# --- the comment is masked on the way in (NFR-303) ---


@pytest.mark.parametrize(
    ("comment", "stored"),
    [
        ("my CID is 11502001234, call 17123456", "my CID is [cid], call [num]"),
        ("write to someone@example.bt", "write to [email]"),
        ("see https://portal.gov.example/x", "see [url]"),
        ("the fee word is wrong", "the fee word is wrong"),
        ("", None),
        (None, None),
    ],
)
def test_nfr303_numbers_ids_emails_and_links_are_masked(
    comment: str | None, stored: str | None
) -> None:
    assert mask_comment(comment) == stored


def test_fr430_the_server_and_the_widget_share_one_contract() -> None:
    """The widget reads the same file, so neither side can drift alone."""
    assert list(REASONS) == CONTRACT["reasons"]
    assert MAX_COMMENT_CHARS == CONTRACT["max_comment_chars"]


def test_fr430_the_database_accepts_exactly_the_contract_reasons() -> None:
    migrations = Path(__file__).parents[3] / "orchestrator" / "store" / "migrations"
    migration = (migrations / "0003_error_report.sql").read_text("utf-8")
    for reason in CONTRACT["reasons"]:
        assert f"'{reason}'" in migration
