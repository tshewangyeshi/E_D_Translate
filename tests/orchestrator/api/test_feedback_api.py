"""Citizen error-report intake (S3.5). Requirements: FR-430, FR-432, NFR-301, NFR-303.

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

import logging
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


# --- found by the pre-landing review, 2026-09-29 ---------------------------


class _Down:
    """A report store whose database is away."""

    def record_report(self, report: ErrorReport) -> None:
        raise ConnectionError("down")

    def count_reports_since(self, segment_key: str, since: datetime) -> int:
        raise ConnectionError("down")

    def count_site_reports_since(self, site_id: str, since: datetime) -> int:
        raise ConnectionError("down")

    def clear_comments_before(self, before: datetime) -> int:
        raise ConnectionError("down")

    def count_comments_before(self, before: datetime) -> int:
        raise ConnectionError("down")


class _Counting(InMemoryReportStore):
    """Counts the queries a request costs."""

    def __init__(self) -> None:
        super().__init__()
        self.queries = 0

    def count_reports_since(self, segment_key: str, since: datetime) -> int:
        self.queries += 1
        return super().count_reports_since(segment_key, since)

    def count_site_reports_since(self, site_id: str, since: datetime) -> int:
        self.queries += 1
        return super().count_site_reports_since(site_id, since)


def app_with(store: object, **limits: object) -> TestClient:
    rig = make_rig()
    app = create_app(
        service=rig.service,
        sites=rig.sites,
        termbase_version="sample",
        reports=store,  # type: ignore[arg-type]
        origin_limiter=limits.get("origin_limiter")  # type: ignore[arg-type]
        or RateLimiter(per_minute=60_000, burst=10_000),
        feedback_limiter=limits.get("feedback_limiter"),  # type: ignore[arg-type]
    )
    host = str(limits.get("host", "203.0.113.10"))
    return TestClient(app, client=(host, 50000), raise_server_exceptions=False)


class TestNoOracle:
    """Found by review: some inputs and faults answered 500, which told a caller more."""

    def test_fr432_a_store_outage_looks_like_any_other_outcome(self) -> None:
        response = app_with(_Down()).post("/v1/feedback", json=report(), headers={"Origin": ORIGIN})
        assert response.status_code == 202 and response.content == b""
        assert response.headers["access-control-allow-origin"] == ORIGIN

    @pytest.mark.parametrize("control", ["\u0000", "\u0007", "\u001b", "\u007f"])
    def test_fr432_a_control_character_is_refused_whatever_the_limits_say(
        self, control: str
    ) -> None:
        """Refused from the request alone, so the answer says nothing about the limiter.

        Regression: a NUL passed validation and PostgreSQL refused it, so the
        route answered 500 when a report would have been stored and 202 when
        it would have been dropped.
        """
        client, store = make(feedback_limiter=RateLimiter(per_minute=10 / 60, burst=1))
        body = report(comment=f"wrong{control}word")
        codes = [
            client.post("/v1/feedback", json=body, headers={"Origin": ORIGIN}).status_code
            for _ in range(3)
        ]
        assert codes == [400, 400, 400]
        assert store.reports == []

    def test_fr430_line_breaks_and_tabs_are_ordinary_text(self) -> None:
        client, store = make()
        comment = "line one\nline two\ttab"
        client.post("/v1/feedback", json=report(comment=comment), headers={"Origin": ORIGIN})
        assert store.reports[0][1].comment == comment

    @pytest.mark.parametrize("route", ["/v1/feedback", "/v1/translate"])
    def test_fr430_a_deeply_nested_body_is_a_400_not_a_500(self, route: str) -> None:
        client = app_with(InMemoryReportStore())
        response = client.post(
            route,
            content=b"[" * 200_000,
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
        assert response.status_code == 400

    def test_fr430_an_over_long_reason_is_refused(self) -> None:
        client, store = make()
        response = client.post(
            "/v1/feedback", json=report(reason="x" * 33), headers={"Origin": ORIGIN}
        )
        assert response.status_code == 400
        assert store.reports == []

    def test_fr432_an_over_long_honeypot_is_refused(self) -> None:
        client, _ = make()
        response = client.post(
            "/v1/feedback", json=report(website="x" * 257), headers={"Origin": ORIGIN}
        )
        assert response.status_code == 400


class TestCostAndLimits:
    def test_fr432_a_caller_over_its_limit_costs_no_database_work(self) -> None:
        store = _Counting()
        client = app_with(store, feedback_limiter=RateLimiter(per_minute=10 / 60, burst=2))
        for _ in range(10):
            client.post("/v1/feedback", json=report(), headers={"Origin": ORIGIN})
        assert store.queries == 2 * 2  # two admitted reports, two counts each
        assert len(store.reports) == 2

    def test_fr432_a_honeypot_hit_costs_no_database_work(self) -> None:
        store = _Counting()
        client = app_with(store)
        client.post("/v1/feedback", json=report(website="x"), headers={"Origin": ORIGIN})
        assert store.queries == 0

    def test_fr432_the_default_is_ten_an_hour_per_client(self) -> None:
        store = InMemoryReportStore()
        one = app_with(store)
        for n in range(11):
            one.post(
                "/v1/feedback", json=report(segment_key=f"{n:064x}"), headers={"Origin": ORIGIN}
            )
        assert len(store.reports) == 10

    def test_fr432_the_limit_is_per_client(self) -> None:
        store = InMemoryReportStore()
        rig = make_rig()
        app = create_app(
            service=rig.service,
            sites=rig.sites,
            termbase_version="sample",
            reports=store,
            feedback_limiter=RateLimiter(per_minute=10 / 60, burst=1),
        )
        a = TestClient(app, client=("203.0.113.10", 50000))
        b = TestClient(app, client=("203.0.113.99", 50000))
        for client in (a, a, b):
            client.post("/v1/feedback", json=report(), headers={"Origin": ORIGIN})
        assert len(store.reports) == 2

    def test_fr432_an_origin_has_a_ceiling_too(self) -> None:
        store = InMemoryReportStore()
        client = app_with(store, origin_limiter=RateLimiter(per_minute=1 / 60, burst=1))
        for n in range(3):
            response = client.post(
                "/v1/feedback", json=report(segment_key=f"{n:064x}"), headers={"Origin": ORIGIN}
            )
            assert response.status_code == 202
        assert len(store.reports) == 1


class TestWhatIsKept:
    def test_nfr303_numbers_and_ids_in_a_comment_are_masked_before_storage(self) -> None:
        client, store = make()
        client.post(
            "/v1/feedback",
            json=report(comment="My CID 11502001234 shows the wrong fee"),
            headers={"Origin": ORIGIN},
        )
        assert store.reports[0][1].comment == "My CID [cid] shows the wrong fee"

    def test_nfr303_the_log_carries_the_stored_reason_never_the_raw_one(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client, store = make()
        with caplog.at_level(logging.DEBUG):
            client.post(
                "/v1/feedback",
                json=report(reason="Testperson\nforged"),
                headers={"Origin": ORIGIN},
            )
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "Testperson" not in logged and "forged" not in logged
        assert "reason=other" in logged
        assert store.reports[0][1].reason == "other"


class TestGuards:
    def test_fr430_an_oversized_report_is_a_413(self) -> None:
        client, store = make()
        response = client.post(
            "/v1/feedback", content=b"x" * (512 * 1024 + 1), headers={"Origin": ORIGIN}
        )
        assert response.status_code == 413 and store.reports == []

    def test_nfr301_a_report_without_an_origin_is_refused(self) -> None:
        client, store = make()
        assert client.post("/v1/feedback", json=report()).status_code == 403
        assert store.reports == []

    def test_fr430_without_a_store_a_report_is_accepted_and_discarded(self) -> None:
        rig = make_rig()
        app = create_app(service=rig.service, sites=rig.sites, termbase_version="sample")
        response = TestClient(app).post("/v1/feedback", json=report(), headers={"Origin": ORIGIN})
        assert response.status_code == 202
