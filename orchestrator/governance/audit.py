"""Audit trail (backlog S3.3). Requirement: FR-620.

Who changed what the service will say, and when:

    review approval  ─► TranslationStore.approve           (same transaction)
    termbase publish ─► TranslationStore.invalidate_terms  (same transaction)
    termbase loaded  ─┐
    tier-rule change ─┼─► recorded by the API process at start, from its files
    enrolment change ─┘
    seed import      ─► TranslationStore.approve(action=SEED_IMPORT) (orchestrator/ops/seed.py)

Four rules shape this file.

* **Append-only.** There is no update and no delete, here or in the database
  (``0004_audit_event.sql`` refuses both by trigger and stamps ``id`` and
  ``at`` itself). What was recorded stays recorded, including the mistakes.
  The table's owner could still switch the triggers off; closing that needs a
  separate migration role (TODOS.md).

* **No unaudited change.** The store validates the whole record before it
  changes anything, and writes it inside the same transaction as the change
  (``TranslationStore.atomic``), so an approval whose record cannot be written
  does not happen.

* **Identifiers, not content.** ``detail`` holds keys, versions, counts and
  origins. Strings must look like identifiers -- letters, digits and
  ``. _ - : / @ + =``, no spaces -- so a name, a sentence or a citizen's words
  (NFR-303) are refused before they reach an append-only table they could
  never be removed from.

* **One writer for configuration.** Sites, tier rules and the termbase are
  files read at start. Only the API process compares them with the trail
  (``record_configuration`` in wiring.py), under a lock, so tools run from a
  laptop and processes starting together do not write events that never
  happened.

``InMemoryAuditLog`` and ``PostgresAuditLog`` pass the same contract tests.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from orchestrator.governance.sites import Site, SiteRegistry

MAX_ACTOR_CHARS = 128
MAX_SUBJECT_CHARS = 256
MAX_DETAIL_KEYS = 16
#: Long enough for a sha256 and a version string, too short for a sentence.
MAX_DETAIL_STRING_CHARS = 128
MAX_DETAIL_LIST_ITEMS = 64

_IDENTIFIER = re.compile(rf"[A-Za-z0-9._:/@+=-]{{1,{MAX_DETAIL_STRING_CHARS}}}")
_SUBJECT = re.compile(r"(segment|site|termbase|seed):[A-Za-z0-9._:/@+=-]{1,240}")


class Action(StrEnum):
    TERMBASE_PUBLISH = "termbase.publish"
    REVIEW_APPROVE = "review.approve"
    SEED_IMPORT = "seed.import"
    TIER_RULE_CHANGE = "tier_rule.change"
    SITE_ENROLMENT_CHANGE = "site.enrolment_change"


class AuditError(ValueError):
    """The event cannot be recorded as given. The change it describes must not proceed."""


@dataclass(frozen=True)
class AuditEvent:
    actor: str
    action: Action
    subject: str
    detail: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordedEvent:
    id: int
    actor: str
    action: Action
    subject: str
    detail: Mapping[str, object]
    at: datetime


class AuditLog(Protocol):
    def record(self, event: AuditEvent) -> RecordedEvent: ...

    def events(
        self,
        *,
        action: Action | None = None,
        subject: str | None = None,
        limit: int | None = 100,
    ) -> list[RecordedEvent]:
        """Newest first. ``limit=None`` returns everything that matches."""
        ...

    def latest(self, action: Action) -> dict[str, Mapping[str, object]]:
        """The newest ``detail`` recorded for ``action``, per subject."""
        ...


def _scalar(value: object) -> bool:
    if value is None or isinstance(value, bool | int | float):
        return True
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def validated(event: AuditEvent) -> tuple[str, str, str, str]:
    """(actor, action, subject, detail as JSON), or AuditError."""
    actor, subject = event.actor.strip(), event.subject.strip()
    if not actor:
        raise AuditError("an audit event needs an actor")
    if len(actor) > MAX_ACTOR_CHARS:
        raise AuditError(f"audit actor is longer than {MAX_ACTOR_CHARS} characters")
    if not subject:
        raise AuditError("an audit event needs a subject")
    if len(subject) > MAX_SUBJECT_CHARS or _SUBJECT.fullmatch(subject) is None:
        raise AuditError("audit subject must be segment:, site:, termbase: or seed: an identifier")
    try:
        action = Action(event.action)
    except ValueError as err:
        raise AuditError(f"unknown audit action {event.action!r}") from err
    if len(event.detail) > MAX_DETAIL_KEYS:
        raise AuditError("audit detail has too many fields")
    for key, value in event.detail.items():
        if not isinstance(key, str) or _IDENTIFIER.fullmatch(key) is None:
            raise AuditError("audit detail keys must be identifiers")
        if _scalar(value):
            continue
        if (
            isinstance(value, list | tuple)
            and len(value) <= MAX_DETAIL_LIST_ITEMS
            and all(_scalar(item) for item in value)
        ):
            continue
        # Deliberately unhelpful about the value: it may be the very text we refuse to keep.
        raise AuditError(f"audit detail {key!r} must be an identifier, a number or a list of them")
    detail = json.dumps(dict(event.detail), ensure_ascii=False, sort_keys=True)
    return actor, action.value, subject, detail


class InMemoryAuditLog:
    """Reference implementation for unit tests; same semantics as PostgresAuditLog."""

    def __init__(self) -> None:
        # Detail is kept serialised, so a caller holding the dict it passed in
        # (or one it read back) cannot alter the record afterwards.
        self._rows: list[tuple[int, str, str, str, str, datetime]] = []

    def record(self, event: AuditEvent) -> RecordedEvent:
        actor, action, subject, detail = validated(event)
        row = (len(self._rows) + 1, actor, action, subject, detail, datetime.now(UTC))
        self._rows.append(row)
        return _recorded(row)

    def events(
        self,
        *,
        action: Action | None = None,
        subject: str | None = None,
        limit: int | None = 100,
    ) -> list[RecordedEvent]:
        matching = [
            _recorded(row)
            for row in reversed(self._rows)
            if (action is None or row[2] == action.value) and (subject is None or row[3] == subject)
        ]
        return matching if limit is None else matching[:limit]

    def latest(self, action: Action) -> dict[str, Mapping[str, object]]:
        newest: dict[str, Mapping[str, object]] = {}
        for event in self.events(action=action, limit=None):  # newest first
            newest.setdefault(event.subject, event.detail)
        return newest


class PostgresAuditLog:
    def __init__(self, conn: Any) -> None:  # psycopg.Connection, autocommit=True
        self.conn = conn

    def record(self, event: AuditEvent) -> RecordedEvent:
        actor, action, subject, detail = validated(event)
        row = self.conn.execute(
            "INSERT INTO audit_event (actor, action, subject, detail)"
            " VALUES (%s, %s, %s, %s::jsonb)"
            " RETURNING id, actor, action, subject, detail::text, at",
            (actor, action, subject, detail),
        ).fetchone()
        return _recorded(row)

    def events(
        self,
        *,
        action: Action | None = None,
        subject: str | None = None,
        limit: int | None = 100,
    ) -> list[RecordedEvent]:
        rows = self.conn.execute(
            "SELECT id, actor, action, subject, detail::text, at FROM audit_event"
            " WHERE (%(action)s::text IS NULL OR action = %(action)s)"
            "   AND (%(subject)s::text IS NULL OR subject = %(subject)s)"
            " ORDER BY id DESC LIMIT %(limit)s",
            {"action": action.value if action else None, "subject": subject, "limit": limit},
        ).fetchall()
        return [_recorded(r) for r in rows]

    def latest(self, action: Action) -> dict[str, Mapping[str, object]]:
        # Proportional to the number of subjects, not to the length of history.
        rows = self.conn.execute(
            "SELECT DISTINCT ON (subject) subject, detail::text FROM audit_event"
            " WHERE action = %s ORDER BY subject, id DESC",
            (action.value,),
        ).fetchall()
        return {subject: json.loads(detail) for subject, detail in rows}


def _recorded(row: Any) -> RecordedEvent:
    return RecordedEvent(row[0], row[1], Action(row[2]), row[3], json.loads(row[4]), row[5])


# -- configuration changes ----------------------------------------------------
#
# Sites, tier rules and the termbase live in files read at startup, so there is
# no "save" to hook. Instead the API process compares what it loaded with what
# the trail last recorded and writes down the difference.


def _fingerprint(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enrolment_fingerprint(site: Site) -> str:
    """Who may call, and whether at all."""
    return _fingerprint({"origins": sorted(site.origins), "enabled": site.enabled})


def tier_rule_fingerprint(site: Site) -> str:
    """Everything that decides a tier or keeps content out of translation (FR-512)."""
    return _fingerprint(
        {
            "default_tier": site.default_tier,
            "path_rules": sorted([r.pattern, r.tier] for r in site.path_rules),
            "tier1_selectors": sorted(site.tier1_selectors),
            "private_selectors": sorted(site.private_selectors),
        }
    )


def _origins(site: Site) -> list[str] | int:
    """The origins themselves when they fit a detail field, otherwise how many there are."""
    origins = sorted(site.origins)
    fits = len(origins) <= MAX_DETAIL_LIST_ITEMS and all(_scalar(o) for o in origins)
    return origins if fits else len(origins)


def audit_site_changes(log: AuditLog, sites: SiteRegistry, actor: str) -> list[RecordedEvent]:
    """Record enrolment and tier-rule changes since the trail last looked (FR-620)."""
    recorded: list[RecordedEvent] = []

    def note(action: Action, subject: str, detail: dict[str, object]) -> None:
        recorded.append(log.record(AuditEvent(actor, action, subject, detail)))

    enrolled = log.latest(Action.SITE_ENROLMENT_CHANGE)
    tiered = log.latest(Action.TIER_RULE_CHANGE)
    current = {f"site:{site.site_id}": site for site in sites.all()}

    for subject, site in sorted(current.items()):
        fingerprint = enrolment_fingerprint(site)
        before = enrolled.get(subject, {})
        if before.get("fingerprint") != fingerprint:
            known = subject in enrolled and before.get("change") != "removed"
            note(
                Action.SITE_ENROLMENT_CHANGE,
                subject,
                {
                    "change": "changed" if known else "enrolled",
                    "fingerprint": fingerprint,
                    "enabled": site.enabled,
                    "origins": _origins(site),
                },
            )
        fingerprint = tier_rule_fingerprint(site)
        if tiered.get(subject, {}).get("fingerprint") != fingerprint:
            note(
                Action.TIER_RULE_CHANGE,
                subject,
                {
                    "change": "set" if subject not in tiered else "changed",
                    "fingerprint": fingerprint,
                    "default_tier": site.default_tier,
                    "path_rules": len(site.path_rules),
                    "tier1_selectors": len(site.tier1_selectors),
                    "private_selectors": len(site.private_selectors),
                },
            )

    for subject, detail in sorted(enrolled.items()):
        if subject not in current and detail.get("change") != "removed":
            note(Action.SITE_ENROLMENT_CHANGE, subject, {"change": "removed", "fingerprint": None})
    return recorded


def audit_termbase_load(
    log: AuditLog, version: str, fingerprint: str, term_count: int, actor: str
) -> RecordedEvent | None:
    """Record a termbase the service has not served before (FR-620).

    The termbase version is part of every cache key, so a new file changes what
    citizens are shown the moment it is deployed. Invalidating approvals that
    used a changed term stays an explicit publish (``invalidate_terms``).
    """
    subject = f"termbase:{version}"
    if log.latest(Action.TERMBASE_PUBLISH).get(subject, {}).get("fingerprint") == fingerprint:
        return None
    return log.record(
        AuditEvent(
            actor,
            Action.TERMBASE_PUBLISH,
            subject,
            {"change": "loaded", "fingerprint": fingerprint, "term_count": term_count},
        )
    )
