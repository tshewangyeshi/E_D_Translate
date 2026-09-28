"""Citizen error-report intake (S3.5). Requirements: FR-430, FR-432, NFR-303.

Two properties carry this endpoint.

**Nothing identifies the reporter.** A citizen who says a government page is
mistranslated should not become a row that says so. The client hash exists only
to decide whether to accept and is never written down.

**Every outcome looks the same.** Stored, rate-limited, saturated and honeypot
all answer 202 with an identical body. Any difference makes this an oracle: a
probe could map which segments are saturated, or tune against the limiter until
it finds the edge. It also spares an honest reader who trips a limit from being
told their report did not count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.ratelimit import RateLimiter
from orchestrator.store.reports import (
    MAX_REPORTS_PER_SEGMENT_PER_DAY,
    ErrorReport,
    InMemoryReportStore,
)
from orchestrator.testing.rig import LEGAL_ORIGIN, ORIGIN, make_rig

KEY = "a" * 64
OTHER_KEY = "b" * 64


def make(**limits: object) -> tuple[TestClient, InMemoryReportStore]:
    rig = make_rig()
    store = InMemoryReportStore()
    app = create_app(
        service=rig.service,
        sites=rig.sites,
        termbase_version="sample",
        reports=store,
        origin_limiter=RateLimiter(per_minute=60_000, burst=10_000),
        client_limiter=RateLimiter(per_minute=60_000, burst=10_000),
        feedback_limiter=limits.get("feedback_limiter"),  # type: ignore[arg-type]
    )
    return TestClient(app, client=("203.0.113.10", 50000)), store


def report(**extra: object) -> dict[str, object]:
    return {"site": "portal", "segment_key": KEY, "reason": "wrong_meaning", **extra}


class TestStoring:
    def test_fr430_a_report_is_stored_against_its_segment(self) -> None:
        client, store = make()
        response = client.post("/v1/feedback", json=report(), headers={"Origin": ORIGIN})
        assert response.status_code == 202
        assert len(store.reports) == 1
        stored = store.reports[0][1]
        assert stored.segment_key == KEY
        assert stored.site_id == "portal"
        assert stored.reason == "wrong_meaning"

    def test_fr430_an_optional_comment_is_kept(self) -> None:
        client, store = make()
        client.post(
            "/v1/feedback",
            json=report(comment="The fee is translated as the wrong word."),
            headers={"Origin": ORIGIN},
        )
        assert store.reports[0][1].comment == "The fee is translated as the wrong word."

    def test_fr430_an_unknown_reason_is_recorded_as_other(self) -> None:
        client, store = make()
        client.post("/v1/feedback", json=report(reason="made-up"), headers={"Origin": ORIGIN})
        assert store.reports[0][1].reason == "other"

    def test_nfr303_nothing_stored_identifies_the_reporter(self) -> None:
        """The whole record, checked field by field, holds no reporter trace."""
        client, store = make()
        client.post("/v1/feedback", json=report(), headers={"Origin": ORIGIN})
        stored = store.reports[0][1]
        fields = set(vars(stored))
        assert fields == {"segment_key", "site_id", "reason", "comment"}
        text = repr(stored)
        for trace in ("203.0.113.10", "client", "hash", "ip", "session"):
            assert trace not in text.lower()


class TestIndistinguishableOutcomes:
    """Stored, limited, saturated and honeypot must be one response."""

    def test_fr432_a_honeypot_hit_is_dropped_and_looks_accepted(self) -> None:
        client, store = make()
        good = client.post("/v1/feedback", json=report(), headers={"Origin": ORIGIN})
        bot = client.post(
            "/v1/feedback",
            json=report(segment_key=OTHER_KEY, website="http://spam.example"),
            headers={"Origin": ORIGIN},
        )
        assert bot.status_code == good.status_code == 202
        assert bot.content == good.content
        assert [r.segment_key for _, r in store.reports] == [KEY]  # the bot's was dropped

    def test_fr432_rate_limited_reports_are_dropped_and_look_accepted(self) -> None:
        client, store = make(feedback_limiter=RateLimiter(per_minute=10 / 60, burst=3))
        responses = [
            client.post("/v1/feedback", json=report(), headers={"Origin": ORIGIN}) for _ in range(6)
        ]
        assert {r.status_code for r in responses} == {202}
        assert len({r.content for r in responses}) == 1  # one body, always
        assert len(store.reports) == 3  # only the burst got through

    def test_fr432_a_saturated_segment_stops_accepting_for_the_day(self) -> None:
        client, store = make(feedback_limiter=RateLimiter(per_minute=10_000, burst=10_000))
        now = datetime.now(UTC)
        for _ in range(MAX_REPORTS_PER_SEGMENT_PER_DAY):
            store.reports.append((now, ErrorReport(KEY, "portal", "other")))

        response = client.post("/v1/feedback", json=report(), headers={"Origin": ORIGIN})
        assert response.status_code == 202
        assert len(store.reports) == MAX_REPORTS_PER_SEGMENT_PER_DAY  # nothing added

        # A different segment is unaffected: the cap is per segment, so one bad
        # translation cannot silence reporting for the rest of the site.
        response = client.post(
            "/v1/feedback", json=report(segment_key=OTHER_KEY), headers={"Origin": ORIGIN}
        )
        assert len(store.reports) == MAX_REPORTS_PER_SEGMENT_PER_DAY + 1

    def test_fr432_yesterdays_reports_do_not_count_against_today(self) -> None:
        client, store = make(feedback_limiter=RateLimiter(per_minute=10_000, burst=10_000))
        old = datetime.now(UTC) - timedelta(days=2)
        for _ in range(MAX_REPORTS_PER_SEGMENT_PER_DAY):
            store.reports.append((old, ErrorReport(KEY, "portal", "other")))

        client.post("/v1/feedback", json=report(), headers={"Origin": ORIGIN})
        assert len(store.reports) == MAX_REPORTS_PER_SEGMENT_PER_DAY + 1


class TestEnrolment:
    def test_nfr301_an_unenrolled_origin_is_refused(self) -> None:
        client, store = make()
        response = client.post(
            "/v1/feedback", json=report(), headers={"Origin": "https://not-enrolled.example"}
        )
        assert response.status_code == 403
        assert store.reports == []

    def test_nfr301_an_origin_enrolled_elsewhere_is_refused(self) -> None:
        client, store = make()
        response = client.post("/v1/feedback", json=report(), headers={"Origin": LEGAL_ORIGIN})
        assert response.status_code == 403
        assert store.reports == []

    @pytest.mark.parametrize(
        "bad",
        [
            {"site": "portal"},  # no segment_key
            {"site": "portal", "segment_key": "too-short"},
            {"site": "portal", "segment_key": "Z" * 64},  # not hex
            {"segment_key": KEY},  # no site
        ],
    )
    def test_fr430_a_malformed_report_is_a_400(self, bad: dict[str, object]) -> None:
        client, store = make()
        response = client.post("/v1/feedback", json=bad, headers={"Origin": ORIGIN})
        assert response.status_code == 400
        assert store.reports == []

    def test_fr430_an_over_long_comment_is_refused_rather_than_truncated(self) -> None:
        client, store = make()
        response = client.post(
            "/v1/feedback", json=report(comment="x" * 5000), headers={"Origin": ORIGIN}
        )
        assert response.status_code == 400
        assert store.reports == []

    def test_nfr301_preflight_allows_an_enrolled_origin_only(self) -> None:
        client, _ = make()
        ok = client.options("/v1/feedback", headers={"Origin": ORIGIN})
        assert ok.status_code == 204
        assert "POST" in ok.headers["Access-Control-Allow-Methods"]
        blocked = client.options("/v1/feedback", headers={"Origin": "https://evil.example"})
        assert blocked.status_code == 403
