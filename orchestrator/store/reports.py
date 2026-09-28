"""Citizen error reports (S3.5). Requirements: FR-430, FR-432, NFR-303.

An inbox, not a workflow. Triage and turning a report into an approved
translation are S7.2.

The one rule that shapes this file: **nothing stored identifies the reporter.**
Not an address, not the daily client hash, not a session. The hash exists only
long enough to decide whether to accept the report and is never written down,
so the record cannot later be used to work out who complained about which
government page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

#: What a citizen can say is wrong. A closed list, because free text invites
#: personal detail we would then be storing (NFR-303).
REASONS = ("wrong_meaning", "wrong_term", "not_translated", "formatting", "offensive", "other")

MAX_COMMENT_CHARS = 500

#: FR-432. Per segment, so one bad translation cannot be used to flood the
#: reviewers' queue.
MAX_REPORTS_PER_SEGMENT_PER_DAY = 100


@dataclass(frozen=True)
class ErrorReport:
    segment_key: str
    site_id: str
    reason: str
    comment: str | None = None


class ReportStore(Protocol):
    def record_report(self, report: ErrorReport) -> None: ...

    def count_reports_since(self, segment_key: str, since: datetime) -> int: ...


class InMemoryReportStore:
    """Reference implementation for unit tests; same semantics as PostgresReportStore."""

    def __init__(self, clock: object = None) -> None:
        self.reports: list[tuple[datetime, ErrorReport]] = []
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if callable(self._clock) else datetime.now(UTC)

    def record_report(self, report: ErrorReport) -> None:
        self.reports.append((self._now(), report))

    def count_reports_since(self, segment_key: str, since: datetime) -> int:
        return sum(1 for at, r in self.reports if r.segment_key == segment_key and at >= since)


class PostgresReportStore:
    def __init__(self, conn: object) -> None:
        self.conn = conn

    def record_report(self, report: ErrorReport) -> None:
        self.conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO error_report (segment_key, site_id, reason, comment)"
            " VALUES (%s, %s, %s, %s)",
            (report.segment_key, report.site_id, report.reason, report.comment),
        )

    def count_reports_since(self, segment_key: str, since: datetime) -> int:
        row = self.conn.execute(  # type: ignore[attr-defined]
            "SELECT count(*) FROM error_report WHERE segment_key = %s AND created_at >= %s",
            (segment_key, since),
        ).fetchone()
        return int(row[0]) if row else 0


def segment_is_saturated(store: ReportStore, segment_key: str, now: datetime) -> bool:
    """True when this segment has had its fill of reports for the day (FR-432)."""
    since = now - timedelta(days=1)
    return store.count_reports_since(segment_key, since) >= MAX_REPORTS_PER_SEGMENT_PER_DAY
