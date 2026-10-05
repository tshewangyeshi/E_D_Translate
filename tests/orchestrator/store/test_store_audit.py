"""Audited writes (S3.3). Requirements: FR-620, FR-421, FR-153.

The audit log has its own contract tests. These are about the call sites: a
log nothing writes to audits nothing, and a change that goes through while
its record fails is exactly the change an audit trail exists to catch.
"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.governance.audit import Action, AuditError, AuditEvent, InMemoryAuditLog
from orchestrator.pipeline.protect import mask
from orchestrator.pipeline.segment import parse
from orchestrator.store.cache import (
    APPROVED_NS,
    InMemoryCache,
    InMemorySeenCounter,
    ResilientCache,
)
from orchestrator.store.keys import Versions, keys_for
from orchestrator.store.lookup import LookupItem, TranslationStore
from orchestrator.store.tm import InMemoryTM

VERSIONS = Versions("en", "dz", "p-test", "nllb-1")
GFP = "0" * 64


def _keys(text: str) -> Any:
    masked, _ = mask(parse(text))
    return keys_for(masked, GFP, VERSIONS), masked


def _store(tm: Any = None, **kw: Any) -> tuple[TranslationStore, InMemoryAuditLog, InMemoryCache]:
    audit, cache = InMemoryAuditLog(), InMemoryCache()
    store = TranslationStore(
        tm or InMemoryTM(), ResilientCache(cache), InMemorySeenCounter(), audit=audit, **kw
    )
    return store, audit, cache


def _approve(store: TranslationStore, author: str = "reviewer-1", text: str = "Pay Nu. 500") -> Any:
    keys, masked = _keys(text)
    store.approve(
        keys=keys,
        masked_source=masked.to_wire(),
        masked_target="⟦CUR:1⟧ approved",
        author=author,
        term_ids=("T-0005",),
        site_id="portal",
    )
    return keys


class _BrokenAudit:
    """An audit log that is down. Validation still works; writing does not."""

    def record(self, event: AuditEvent) -> Any:
        raise ConnectionError("audit store unavailable")

    def events(self, **_: Any) -> list[Any]:
        return []


# --- approvals ---


def test_fr620_an_approval_is_recorded_with_its_reviewer() -> None:
    store, audit, _ = _store()
    keys = _approve(store, author="reviewer-7")

    (event,) = audit.events()
    assert event.actor == "reviewer-7"
    assert event.action is Action.REVIEW_APPROVE
    assert event.subject == f"segment:{keys.segment_key}"
    assert event.detail["site_id"] == "portal"
    version = store.tm.history(keys.segment_key)[-1]
    assert event.detail["version_id"] == version.id


def test_fr620_each_approval_of_the_same_segment_is_its_own_record() -> None:
    """Re-approving replaces the translation; it must not replace the history."""
    store, audit, _ = _store()
    _approve(store, author="reviewer-1")
    _approve(store, author="reviewer-2")
    assert [e.actor for e in audit.events()] == ["reviewer-2", "reviewer-1"]
    assert len({e.detail["version_id"] for e in audit.events()}) == 2


def test_fr620_the_record_holds_no_translated_text() -> None:
    store, audit, _ = _store()
    _approve(store)
    (event,) = audit.events()
    assert "approved" not in str(event.detail) and "Pay" not in str(event.detail)


@pytest.mark.parametrize("author", ["", "   "])
def test_fr620_an_approval_without_a_reviewer_does_not_happen(author: str) -> None:
    store, audit, cache = _store()
    keys, _ = _keys("Pay Nu. 500")
    with pytest.raises(AuditError):
        _approve(store, author=author)

    assert store.tm.history(keys.segment_key) == []
    assert store.lookup([LookupItem(keys, 1)]) == [None]
    assert cache.data == {} and audit.events() == []


def test_fr620_the_cache_is_untouched_when_the_record_cannot_be_written() -> None:
    """Otherwise the unrecorded approval would be served from the cache anyway."""
    cache = InMemoryCache()
    store = TranslationStore(
        InMemoryTM(), ResilientCache(cache), InMemorySeenCounter(), audit=_BrokenAudit()
    )
    with pytest.raises(ConnectionError):
        _approve(store)
    assert not any(key.startswith(APPROVED_NS) for key in cache.data)


@pytest.mark.integration
def test_fr620_an_approval_whose_record_fails_is_rolled_back(pg_conn: Any) -> None:
    """One transaction: no audit record, no approval."""
    from orchestrator.store.postgres_tm import PostgresTM

    tm = PostgresTM(pg_conn)
    store = TranslationStore(
        tm,
        ResilientCache(InMemoryCache()),
        InMemorySeenCounter(),
        audit=_BrokenAudit(),
        atomic=pg_conn.transaction,
    )
    keys, _ = _keys("Pay Nu. 500")
    with pytest.raises(ConnectionError):
        _approve(store)

    assert tm.history(keys.segment_key) == []
    assert tm.review_item(keys.segment_key) is None
    assert store.lookup([LookupItem(keys, 1)]) == [None]


@pytest.mark.integration
def test_fr620_an_approval_and_its_record_commit_together(pg_conn: Any) -> None:
    from orchestrator.governance.audit import PostgresAuditLog
    from orchestrator.store.postgres_tm import PostgresTM

    audit = PostgresAuditLog(pg_conn)
    store = TranslationStore(
        PostgresTM(pg_conn),
        ResilientCache(InMemoryCache()),
        InMemorySeenCounter(),
        audit=audit,
        atomic=pg_conn.transaction,
    )
    keys = _approve(store, author="reviewer-3")
    (event,) = audit.events()
    assert event.actor == "reviewer-3" and event.subject == f"segment:{keys.segment_key}"
    assert store.lookup([LookupItem(keys, 1)])[0] is not None


# --- termbase publishes ---


def test_fr620_a_termbase_publish_is_recorded_with_what_it_invalidated() -> None:
    store, audit, _ = _store()
    _approve(store)  # uses T-0005
    keys, masked = _keys("Apply online")
    store.record_machine(
        keys=keys,
        masked_source=masked.to_wire(),
        masked_target="DZ: apply",
        model_version="nllb-1",
        term_ids=("T-0005",),
        tag_integrity=True,
    )

    store.invalidate_terms(
        ["T-0005", "T-0001", "T-0005"], actor="dcdd-publisher", termbase_version="2026.10"
    )

    event = audit.events(action=Action.TERMBASE_PUBLISH)[0]
    assert event.actor == "dcdd-publisher"
    assert event.subject == "termbase:2026.10"
    assert event.detail == {
        "terms": ["T-0001", "T-0005"],
        "term_count": 2,
        "machine_invalidated": 1,
        "approved_needing_recheck": 1,
    }


def test_fr620_a_publish_that_changes_nothing_is_still_a_publish() -> None:
    store, audit, _ = _store()
    store.invalidate_terms(["T-9999"], actor="dcdd-publisher")
    (event,) = audit.events()
    assert event.detail["machine_invalidated"] == 0
    assert event.detail["approved_needing_recheck"] == 0


def test_fr620_a_large_publish_lists_some_terms_and_counts_all() -> None:
    store, audit, _ = _store()
    store.invalidate_terms([f"T-{n:04d}" for n in range(200)], actor="dcdd-publisher")
    (event,) = audit.events()
    assert event.detail["term_count"] == 200
    assert len(event.detail["terms"]) == 64


def test_fr620_a_publish_without_a_publisher_does_not_happen() -> None:
    store, audit, _ = _store()
    keys = _approve(store)
    with pytest.raises(AuditError):
        store.invalidate_terms(["T-0005"], actor="")
    assert store.lookup([LookupItem(keys, 1)])[0] is not None  # still served: nothing changed
    assert len(audit.events()) == 1  # the approval only


def test_fr620_a_store_built_without_a_log_still_audits() -> None:
    """There is no code path where an approval goes unrecorded."""
    store = TranslationStore(InMemoryTM(), InMemoryCache(), InMemorySeenCounter())
    _approve(store)
    assert [e.action for e in store.audit.events()] == [Action.REVIEW_APPROVE]
