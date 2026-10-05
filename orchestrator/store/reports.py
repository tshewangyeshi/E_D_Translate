"""Citizen error reports (S3.5). Requirements: FR-430, FR-432, NFR-303, NFR-305.

An inbox, not a workflow. Triage and turning a report into an approved
translation are S7.2.

The one rule that shapes this file: **nothing stored identifies the reporter.**
Not an address, not the daily client hash, not a session. The hash exists only
long enough to decide whether to accept the report and is never written down,
so the record cannot later be used to work out who complained about which
government page.

The optional comment is the one piece of free text, and people type personal
details into boxes like it. So it is masked before storage -- numbers, IDs,
emails and links become ``[id]``, ``[url]`` and so on, using the same patterns
that keep them from the model -- and cleared by the retention job after the
retention period, while the report itself is kept. Masking does not catch
names or addresses; the widget asks the reader not to type them.

``reason`` and the honeypot come from the shared contract fixture
(tests/fixtures/feedback/contract.json) as far as tests are concerned: the
widget and the server are both checked against it, so they cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from orchestrator.pipeline.protect import find_entities

#: What a citizen can say is wrong. A closed list, because free text invites
#: personal detail we would then be storing (NFR-303).
REASONS = ("wrong_meaning", "wrong_term", "not_translated", "formatting", "offensive", "other")

MAX_COMMENT_CHARS = 500

#: FR-432. Per segment, so one bad translation cannot be used to flood the
#: reviewers' queue.
MAX_REPORTS_PER_SEGMENT_PER_DAY = 100

#: Per site, so varying the segment key cannot flood a site's inbox either.
#: Proposed: roughly ten times what a busy pilot site should see.
MAX_REPORTS_PER_SITE_PER_DAY = 1000


@dataclass(frozen=True)
class ErrorReport:
    segment_key: str
    site_id: str
    reason: str
    comment: str | None = None


def mask_comment(comment: str | None) -> str | None:
    """Replace every number, ID, email and link with its kind, e.g. ``[cid]``."""
    if not comment:
        return None
    out, last = [], 0
    for start, end, kind in find_entities(comment):
        out.append(comment[last:start])
        out.append(f"[{kind.lower()}]")
        last = end
    out.append(comment[last:])
    return "".join(out)


class ReportStore(Protocol):
    def record_report(self, report: ErrorReport) -> None: ...

    def count_reports_since(self, segment_key: str, since: datetime) -> int: ...

    def count_site_reports_since(self, site_id: str, since: datetime) -> int: ...

    def clear_comments_before(self, before: datetime) -> int:
        """Retention (NFR-305): drop the free text of older reports, keep the reports."""
        ...

    def count_comments_before(self, before: datetime) -> int: ...


class InMemoryReportStore:
    """Reference implementation for unit tests; same semantics as PostgresReportStore."""

    def __init__(self) -> None:
        self.reports: list[tuple[datetime, ErrorReport]] = []

    def record_report(self, report: ErrorReport) -> None:
        self.reports.append((datetime.now(UTC), report))

    def count_reports_since(self, segment_key: str, since: datetime) -> int:
        return sum(1 for at, r in self.reports if r.segment_key == segment_key and at >= since)

    def count_site_reports_since(self, site_id: str, since: datetime) -> int:
        return sum(1 for at, r in self.reports if r.site_id == site_id and at >= since)

    def clear_comments_before(self, before: datetime) -> int:
        cleared = 0
        for n, (at, report) in enumerate(self.reports):
            if at < before and report.comment is not None:
                self.reports[n] = (at, replace(report, comment=None))
                cleared += 1
        return cleared

    def count_comments_before(self, before: datetime) -> int:
        return sum(1 for at, r in self.reports if at < before and r.comment is not None)


class PostgresReportStore:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def record_report(self, report: ErrorReport) -> None:
        self.conn.execute(
            "INSERT INTO error_report (segment_key, site_id, reason, comment)"
            " VALUES (%s, %s, %s, %s)",
            (report.segment_key, report.site_id, report.reason, report.comment),
        )

    def count_reports_since(self, segment_key: str, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM error_report WHERE segment_key = %s AND created_at >= %s",
            (segment_key, since),
        ).fetchone()
        return int(row[0]) if row else 0

    def count_site_reports_since(self, site_id: str, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM error_report WHERE site_id = %s AND created_at >= %s",
            (site_id, since),
        ).fetchone()
        return int(row[0]) if row else 0

    def clear_comments_before(self, before: datetime) -> int:
        cur = self.conn.execute(
            "UPDATE error_report SET comment = NULL"
            " WHERE created_at < %s AND comment IS NOT NULL",
            (before,),
        )
        return int(cur.rowcount)

    def count_comments_before(self, before: datetime) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM error_report WHERE created_at < %s AND comment IS NOT NULL",
            (before,),
        ).fetchone()
        return int(row[0]) if row else 0


def is_saturated(store: ReportStore, report: ErrorReport, now: datetime) -> bool:
    """True when the segment or the site has had its fill of reports for the day (FR-432)."""
    since = now - timedelta(days=1)
    return (
        store.count_reports_since(report.segment_key, since) >= MAX_REPORTS_PER_SEGMENT_PER_DAY
        or store.count_site_reports_since(report.site_id, since) >= MAX_REPORTS_PER_SITE_PER_DAY
    )
