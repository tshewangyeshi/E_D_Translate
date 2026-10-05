"""Audit trail (S3.3). Requirement: FR-620.

An audit trail is only worth having if it cannot be talked out of what it
saw. So beyond "an event round-trips", these tests try to change the record
after the fact, to record a change nobody can be named for, and to slip a
citizen's words into it. The contract runs against the in-memory log and,
when PostgreSQL is reachable, the real one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator.governance.audit import (
    Action,
    AuditError,
    AuditEvent,
    InMemoryAuditLog,
    PostgresAuditLog,
    audit_site_changes,
    audit_termbase_load,
)
from orchestrator.governance.sites import PathRule, Site, SiteRegistry

SEGMENT = "segment:" + "a" * 64


def _approval(**changes: Any) -> AuditEvent:
    event = AuditEvent("reviewer-1", Action.REVIEW_APPROVE, SEGMENT, {"version_id": 7})
    return replace(event, **changes)


# --- the contract, on both backends ---


def test_fr620_an_event_records_actor_action_subject_and_time(audit_log: Any) -> None:
    before = datetime.now(UTC) - timedelta(seconds=5)
    recorded = audit_log.record(_approval())
    assert recorded.actor == "reviewer-1"
    assert recorded.action is Action.REVIEW_APPROVE
    assert recorded.subject == SEGMENT
    assert recorded.detail == {"version_id": 7}
    assert before <= recorded.at <= datetime.now(UTC) + timedelta(seconds=5)
    assert audit_log.events() == [recorded]


def test_fr620_every_audited_action_can_be_recorded(audit_log: Any) -> None:
    """The database CHECK constraint and the Python enum must agree."""
    for action in Action:
        audit_log.record(_approval(action=action))
    assert {e.action for e in audit_log.events()} == set(Action)


def test_fr620_events_come_back_newest_first_and_filter(audit_log: Any) -> None:
    audit_log.record(_approval(actor="first"))
    audit_log.record(_approval(actor="second", subject="segment:" + "b" * 64))
    audit_log.record(AuditEvent("dcdd", Action.TERMBASE_PUBLISH, "termbase:2026.10"))

    assert [e.actor for e in audit_log.events()] == ["dcdd", "second", "first"]
    assert [e.actor for e in audit_log.events(action=Action.REVIEW_APPROVE)] == ["second", "first"]
    assert [e.actor for e in audit_log.events(subject=SEGMENT)] == ["first"]
    assert [e.actor for e in audit_log.events(limit=1)] == ["dcdd"]
    assert len(audit_log.events(limit=None)) == 3


def test_fr620_the_record_does_not_change_when_the_caller_changes_its_copy(
    audit_log: Any,
) -> None:
    detail = {"version_id": 7, "terms": ["T-0005"]}
    audit_log.record(_approval(detail=detail))
    detail["version_id"] = 8
    detail["terms"].append("T-0001")
    read_back = audit_log.events()[0]
    read_back.detail["version_id"] = 9  # type: ignore[index]

    assert audit_log.events()[0].detail == {"version_id": 7, "terms": ["T-0005"]}


@pytest.mark.parametrize("actor", ["", "   ", "x" * 129])
def test_fr620_a_change_nobody_can_be_named_for_is_refused(audit_log: Any, actor: str) -> None:
    with pytest.raises(AuditError):
        audit_log.record(_approval(actor=actor))
    assert audit_log.events() == []


@pytest.mark.parametrize("subject", ["", "  "])
def test_fr620_a_change_to_nothing_in_particular_is_refused(audit_log: Any, subject: str) -> None:
    with pytest.raises(AuditError):
        audit_log.record(_approval(subject=subject))


def test_fr620_an_action_outside_the_list_is_refused(audit_log: Any) -> None:
    with pytest.raises(AuditError):
        audit_log.record(_approval(action="review.quietly_edit"))
    assert audit_log.events() == []


@pytest.mark.parametrize(
    "detail",
    [
        # Person-shaped but invented: long enough to be a sentence about someone.
        {"comment": "Testperson Examplename of Nowhereton asked for this wording to change " * 2},
        {"nested": {"text": "anything"}},
        {"items": [{"text": "anything"}]},
        {"": "empty key"},
        {f"k{n}": n for n in range(17)},
    ],
)
def test_nfr303_detail_holds_identifiers_not_content(
    audit_log: Any, detail: dict[str, Any]
) -> None:
    """A free-text field is where a citizen's words would end up being stored."""
    with pytest.raises(AuditError) as refused:
        audit_log.record(_approval(detail=detail))
    assert "Testperson" not in str(refused.value)  # the refusal must not repeat the text
    assert audit_log.events() == []


def test_fr620_the_log_offers_no_way_to_rewrite() -> None:
    """Append-only by construction: there is nothing to call."""
    for log in (InMemoryAuditLog, PostgresAuditLog):
        public = {name for name in vars(log) if not name.startswith("_")}
        assert public == {"record", "events", "latest"}


# --- append-only is enforced by the database, not by good manners ---


@pytest.mark.integration
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_event SET actor = 'someone-else'",
        "UPDATE audit_event SET detail = '{}'::jsonb",
        "UPDATE audit_event SET at = now() - interval '1 year'",
        "DELETE FROM audit_event",
        "TRUNCATE audit_event",
    ],
)
def test_fr620_the_database_refuses_to_rewrite_history(pg_conn: Any, statement: str) -> None:
    import psycopg

    log = PostgresAuditLog(pg_conn)
    original = log.record(_approval())
    with pytest.raises(psycopg.Error, match="append-only"):
        pg_conn.execute(statement)
    assert log.events() == [original]


# --- enrolment and tier-rule changes ---


def _portal(**changes: Any) -> Site:
    site = Site(
        "portal",
        frozenset({"https://portal.gov.example"}),
        default_tier=2,
        tier1_selectors=(".fees",),
        path_rules=(PathRule("/legal/*", 1),),
    )
    return replace(site, **changes)


def _actions(events: list[Any]) -> list[tuple[str, str, Any]]:
    return sorted((e.action.value, e.subject, e.detail.get("change")) for e in events)


def test_fr620_first_sight_of_a_site_records_enrolment_and_tier_rules(audit_log: Any) -> None:
    recorded = audit_site_changes(audit_log, SiteRegistry([_portal()]), "operator-1")
    assert _actions(recorded) == [
        ("site.enrolment_change", "site:portal", "enrolled"),
        ("tier_rule.change", "site:portal", "set"),
    ]
    assert {e.actor for e in recorded} == {"operator-1"}


def test_fr620_an_unchanged_configuration_records_nothing(audit_log: Any) -> None:
    """Every process start reloads the file; only a change is an event."""
    audit_site_changes(audit_log, SiteRegistry([_portal()]), "ops")
    assert audit_site_changes(audit_log, SiteRegistry([_portal()]), "ops") == []
    assert len(audit_log.events(limit=None)) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"default_tier": 3},
        {"path_rules": ()},  # a Tier 1 rule quietly dropped
        {"path_rules": (PathRule("/legal/*", 2),)},  # or loosened
        {"tier1_selectors": ()},
        {"private_selectors": ("[data-account]",)},
    ],
)
def test_fr620_a_tier_rule_change_is_recorded(audit_log: Any, changes: dict[str, Any]) -> None:
    audit_site_changes(audit_log, SiteRegistry([_portal()]), "ops")
    recorded = audit_site_changes(audit_log, SiteRegistry([_portal(**changes)]), "operator-2")
    assert _actions(recorded) == [("tier_rule.change", "site:portal", "changed")]
    assert recorded[0].actor == "operator-2"


@pytest.mark.parametrize(
    "changes",
    [
        {"origins": frozenset({"https://portal.gov.example", "https://evil.example"})},
        {"enabled": False},
    ],
)
def test_fr620_an_enrolment_change_is_recorded(audit_log: Any, changes: dict[str, Any]) -> None:
    audit_site_changes(audit_log, SiteRegistry([_portal()]), "ops")
    recorded = audit_site_changes(audit_log, SiteRegistry([_portal(**changes)]), "ops")
    assert _actions(recorded) == [("site.enrolment_change", "site:portal", "changed")]


def test_fr620_the_origins_allowed_to_call_are_on_the_record(audit_log: Any) -> None:
    (enrolment, _) = audit_site_changes(audit_log, SiteRegistry([_portal()]), "ops")
    assert enrolment.detail["origins"] == ["https://portal.gov.example"]
    assert enrolment.detail["enabled"] is True


def test_fr620_removing_a_site_is_recorded_once_and_so_is_its_return(audit_log: Any) -> None:
    other = replace(_portal(), site_id="other")
    audit_site_changes(audit_log, SiteRegistry([_portal(), other]), "ops")

    removed = audit_site_changes(audit_log, SiteRegistry([_portal()]), "ops")
    assert _actions(removed) == [("site.enrolment_change", "site:other", "removed")]
    assert audit_site_changes(audit_log, SiteRegistry([_portal()]), "ops") == []

    back = audit_site_changes(audit_log, SiteRegistry([_portal(), other]), "ops")
    assert ("site.enrolment_change", "site:other", "enrolled") in _actions(back)
    assert audit_site_changes(audit_log, SiteRegistry([_portal(), other]), "ops") == []


# --- found by the pre-landing review, 2026-09-29 ---------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Testperson Examplename",  # a name has a space
        "call me on 17 12 34 56",
        "line\nbreak",
        "x" * 129,
    ],
)
def test_nfr303_a_detail_string_must_look_like_an_identifier(audit_log: Any, value: str) -> None:
    with pytest.raises(AuditError):
        audit_log.record(_approval(detail={"note": value}))
    assert audit_log.events() == []


@pytest.mark.parametrize(
    "value",
    ["T-0005", "sample-2026.09.1", "https://portal.gov.example", "a" * 64, "config:sites.json"],
)
def test_fr620_identifiers_versions_and_origins_are_accepted(audit_log: Any, value: str) -> None:
    audit_log.record(_approval(detail={"value": value, "values": [value]}))
    assert audit_log.events()[0].detail == {"value": value, "values": [value]}


@pytest.mark.parametrize(
    "subject", ["portal", "segment:has space", "person:Testperson", "site:" + "x" * 300]
)
def test_fr620_a_subject_names_a_known_kind_of_thing(audit_log: Any, subject: str) -> None:
    with pytest.raises(AuditError):
        audit_log.record(_approval(subject=subject))


def test_fr620_an_over_long_actor_says_so(audit_log: Any) -> None:
    """Regression: the message said the actor was missing, which it was not."""
    with pytest.raises(AuditError, match="longer than 128"):
        audit_log.record(_approval(actor="x" * 129))


def test_fr620_latest_is_the_newest_detail_per_subject(audit_log: Any) -> None:
    audit_log.record(_approval(detail={"version_id": 1}))
    audit_log.record(_approval(detail={"version_id": 2}))
    audit_log.record(_approval(subject="segment:" + "b" * 64, detail={"version_id": 3}))
    audit_log.record(AuditEvent("dcdd", Action.TERMBASE_PUBLISH, "termbase:v1"))
    assert audit_log.latest(Action.REVIEW_APPROVE) == {
        SEGMENT: {"version_id": 2},
        "segment:" + "b" * 64: {"version_id": 3},
    }


@pytest.mark.integration
def test_fr620_the_database_sets_id_and_time_whatever_the_caller_sends(pg_conn: Any) -> None:
    """A record cannot be backdated or slotted in between two others."""
    log = PostgresAuditLog(pg_conn)
    first = log.record(_approval())
    pg_conn.execute(
        "INSERT INTO audit_event (id, actor, action, subject, at)"
        " VALUES (1, 'backdater', 'review.approve', 'segment:x', now() - interval '1 year')"
    )
    newest = log.events()[0]
    assert newest.actor == "backdater"
    assert newest.id > first.id
    assert newest.at >= first.at


def test_fr620_a_termbase_is_recorded_once_per_content(audit_log: Any) -> None:
    first = audit_termbase_load(audit_log, "2026.10", "a" * 64, 120, "release-7")
    assert first is not None and first.subject == "termbase:2026.10"
    assert first.detail == {"change": "loaded", "fingerprint": "a" * 64, "term_count": 120}
    assert audit_termbase_load(audit_log, "2026.10", "a" * 64, 120, "release-8") is None
    # Same version label, different content: that is a change, and it is recorded.
    assert audit_termbase_load(audit_log, "2026.10", "b" * 64, 121, "release-9") is not None


def test_fr620_a_site_with_many_origins_records_how_many(audit_log: Any) -> None:
    origins = frozenset(f"https://s{n}.gov.example" for n in range(65))
    sites = SiteRegistry([_portal(origins=origins)])
    (enrolment, _) = audit_site_changes(audit_log, sites, "ops")
    assert enrolment.detail["origins"] == 65
    assert audit_site_changes(audit_log, sites, "ops") == []
