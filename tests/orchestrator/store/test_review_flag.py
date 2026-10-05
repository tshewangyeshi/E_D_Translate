"""Tier 2 output is flagged for review, and the flagging is capped (S3.1).

Requirements: FR-511, FR-510, FR-421.

The translation-memory half is a contract, run against the in-memory TM and,
when PostgreSQL is reachable, the real one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator.store.cache import InMemoryCache, InMemorySeenCounter, ResilientCache
from orchestrator.store.keys import SegmentKeys
from orchestrator.store.lookup import LookupItem, StoreSettings, TranslationStore
from orchestrator.store.models import FlagOutcome, RaisedBy, ReviewRequest, ReviewState
from orchestrator.store.tm import InMemoryTM

GFP = "0" * 64


def _seg(n: int) -> str:
    return f"{n:064d}"


def _mt(tm: Any, n: int) -> Any:
    return tm.store_machine(
        segment_key=_seg(n),
        masked_source=f"Pay ⟦CUR:1⟧ for item {n}",
        machine_key=f"m{n:063d}",
        masked_target="DZ: ⟦CUR:1⟧",
        gfp=GFP,
        model_version="nllb-test-1",
        term_ids=(),
        tag_integrity=True,
    )


def _flag(
    tm: Any,
    n: int,
    *,
    site: str = "portal",
    raised_by: RaisedBy = RaisedBy.REQUEST,
    cap: int | None = 500,
    since: datetime | None = None,
) -> FlagOutcome:
    return tm.flag_for_review(
        segment_key=_seg(n),
        version_id=_mt(tm, n).version_id,
        site_id=site,
        raised_by=raised_by,
        daily_cap=cap,
        since=since or datetime.now(UTC) - timedelta(days=1),
    )


# --- the contract, on both backends ---


def test_fr511_flagging_opens_a_pending_review_item(tm: Any) -> None:
    stored = _mt(tm, 1)
    outcome = tm.flag_for_review(
        segment_key=_seg(1),
        version_id=stored.version_id,
        site_id="portal",
        raised_by=RaisedBy.REQUEST,
        daily_cap=500,
        since=datetime.now(UTC) - timedelta(days=1),
    )
    assert outcome is FlagOutcome.CREATED
    item = tm.review_item(_seg(1))
    assert item.state is ReviewState.PENDING_REVIEW
    assert item.current_version == stored.version_id
    assert item.site_id == "portal" and item.raised_by is RaisedBy.REQUEST
    assert item.approved_key is None
    assert item.created_at is not None


def test_fr511_a_segment_is_flagged_once(tm: Any) -> None:
    assert _flag(tm, 1) is FlagOutcome.CREATED
    assert _flag(tm, 1) is FlagOutcome.EXISTS
    assert tm.pending_review("portal") == 1


def test_fr421_flagging_never_reopens_an_approval(tm: Any) -> None:
    """New machine output for an approved segment must not put it back in the queue."""
    approved = tm.approve(
        segment_key=_seg(1),
        masked_source="Pay ⟦CUR:1⟧ for item 1",
        approved_key="a" * 64,
        masked_target="approved ⟦CUR:1⟧",
        gfp=GFP,
        author="reviewer-1",
        term_ids=(),
        site_id="portal",
    )
    assert _flag(tm, 1) is FlagOutcome.EXISTS
    item = tm.review_item(_seg(1))
    assert item.state is ReviewState.APPROVED
    assert item.current_version == approved.version_id


def test_fr510_a_pending_review_item_is_not_an_approval(tm: Any) -> None:
    """The item points at machine output. Tier 1 must never be served it."""
    _flag(tm, 1)
    approved, _ = tm.lookup([_seg(1), "a" * 64, f"m{1:063d}"], [])
    assert approved == {}


def test_fr421_approving_a_flagged_segment_keeps_when_and_why_it_was_opened(tm: Any) -> None:
    _flag(tm, 1)
    opened = tm.review_item(_seg(1))
    tm.approve(
        segment_key=_seg(1),
        masked_source="Pay ⟦CUR:1⟧ for item 1",
        approved_key="a" * 64,
        masked_target="approved ⟦CUR:1⟧",
        gfp=GFP,
        author="reviewer-1",
        term_ids=(),
    )
    item = tm.review_item(_seg(1))
    assert item.state is ReviewState.APPROVED
    assert item.created_at == opened.created_at
    assert item.raised_by is RaisedBy.REQUEST and item.site_id == "portal"
    assert tm.pending_review("portal") == 0


def test_fr511_past_the_cap_items_are_owed_not_dropped(tm: Any) -> None:
    """The reviewers' queue stops growing; the record of what was shown does not."""
    assert [_flag(tm, n, cap=3) for n in range(5)] == [
        FlagOutcome.CREATED,
        FlagOutcome.CREATED,
        FlagOutcome.CREATED,
        FlagOutcome.OWED,
        FlagOutcome.OWED,
    ]
    assert tm.pending_review("portal") == 3
    assert tm.owed("portal") == 2
    assert tm.review_item(_seg(4)).state is ReviewState.OWED


def test_fr511_owed_items_do_not_use_up_tomorrows_cap(tm: Any) -> None:
    for n in range(4):
        _flag(tm, n, cap=2)
    assert tm.owed("portal") == 2
    tomorrow = datetime.now(UTC) + timedelta(hours=1)
    assert _flag(tm, 10, cap=2, since=tomorrow) is FlagOutcome.CREATED


def test_fr511_released_owed_items_join_the_queue_oldest_first(tm: Any) -> None:
    for n in range(5):
        _flag(tm, n, cap=1)
    assert tm.owed("portal") == 4
    assert tm.release_owed("portal", 3) == 3
    assert tm.owed("portal") == 1 and tm.pending_review("portal") == 4
    assert tm.review_item(_seg(4)).state is ReviewState.OWED  # the newest waits
    assert tm.release_owed("portal", 10) == 1
    assert tm.release_owed("portal", 10) == 0
    assert tm.release_owed("health", 10) == 0


def test_fr511_new_output_re_points_an_open_item(tm: Any) -> None:
    """The reviewer is asked about what citizens are shown now, not last month."""
    _flag(tm, 1)
    newer = tm.store_machine(
        segment_key=_seg(1),
        masked_source="Pay ⟦CUR:1⟧ for item 1",
        machine_key=f"x{1:063d}",
        masked_target="DZ newer ⟦CUR:1⟧",
        gfp=GFP,
        model_version="nllb-test-2",
        term_ids=(),
        tag_integrity=True,
    )
    outcome = tm.flag_for_review(
        segment_key=_seg(1),
        version_id=newer.version_id,
        site_id="portal",
        raised_by=RaisedBy.REQUEST,
        daily_cap=500,
        since=datetime.now(UTC) - timedelta(days=1),
    )
    assert outcome is FlagOutcome.EXISTS
    item = tm.review_item(_seg(1))
    assert item.current_version == newer.version_id
    assert item.state is ReviewState.PENDING_REVIEW
    assert tm.pending_review("portal") == 1


def test_fr511_an_older_version_never_re_points_an_item(tm: Any) -> None:
    first = _mt(tm, 1)
    second = _mt(tm, 1)
    for version in (second.version_id, first.version_id):
        tm.flag_for_review(
            segment_key=_seg(1),
            version_id=version,
            site_id="portal",
            raised_by=RaisedBy.REQUEST,
            daily_cap=None,
            since=datetime.now(UTC) - timedelta(days=1),
        )
    assert tm.review_item(_seg(1)).current_version == second.version_id


def test_nfr305_expiring_the_output_closes_its_open_review(tm: Any) -> None:
    """A reviewer must not be asked about text the service no longer serves."""
    _flag(tm, 1)
    _flag(tm, 2, cap=0)  # owed
    tm.expire_machine(datetime.now(UTC) + timedelta(days=1))
    assert tm.review_item(_seg(1)) is None and tm.review_item(_seg(2)) is None
    assert tm.pending_review() == 0 and tm.owed() == 0


def test_fr153_a_term_publish_closes_open_reviews_of_invalidated_output(tm: Any) -> None:
    stored = tm.store_machine(
        segment_key=_seg(1),
        masked_source="Pay the ⟦T:1⟧",
        machine_key=f"m{1:063d}",
        masked_target="DZ ⟦T:1⟧",
        gfp=GFP,
        model_version="nllb-test-1",
        term_ids=("T-0005",),
        tag_integrity=True,
    )
    tm.flag_for_review(
        segment_key=_seg(1),
        version_id=stored.version_id,
        site_id="portal",
        raised_by=RaisedBy.REQUEST,
        daily_cap=None,
        since=datetime.now(UTC) - timedelta(days=1),
    )
    tm.invalidate_terms(["T-0005"])
    assert tm.review_item(_seg(1)) is None


def test_fr421_expiry_leaves_approvals_alone(tm: Any) -> None:
    tm.approve(
        segment_key=_seg(1),
        masked_source="Pay ⟦CUR:1⟧ for item 1",
        approved_key="a" * 64,
        masked_target="approved ⟦CUR:1⟧",
        gfp=GFP,
        author="reviewer-1",
        term_ids=(),
        site_id="portal",
    )
    tm.expire_machine(datetime.now(UTC) + timedelta(days=1))
    assert tm.review_item(_seg(1)).state is ReviewState.APPROVED


def test_fr511_lookup_says_whether_output_is_under_review(tm: Any) -> None:
    _mt(tm, 1)
    _mt(tm, 2)
    _flag(tm, 2)
    _, machine = tm.lookup([], [f"m{1:063d}", f"m{2:063d}"])
    assert machine[f"m{1:063d}"].review is False
    assert machine[f"m{2:063d}"].review is True


def test_fr511_the_cap_is_per_site(tm: Any) -> None:
    """One site being flooded must not silence the reviewers of another."""
    for n in range(3):
        _flag(tm, n, site="portal", cap=3)
    assert _flag(tm, 10, site="portal", cap=3) is FlagOutcome.OWED
    assert _flag(tm, 11, site="health", cap=3) is FlagOutcome.CREATED


def test_fr511_operator_work_is_not_capped_and_does_not_use_up_the_cap(tm: Any) -> None:
    """Pre-warming a site must neither be cut short nor leave page views unflagged."""
    for n in range(10):
        assert (
            _flag(tm, n, raised_by=RaisedBy.OPERATOR, cap=None) is FlagOutcome.CREATED
        ), "operator work is uncapped"
    assert _flag(tm, 100, raised_by=RaisedBy.REQUEST, cap=3) is FlagOutcome.CREATED


def test_fr511_the_cap_counts_a_rolling_day(tm: Any) -> None:
    for n in range(3):
        _flag(tm, n, cap=3)
    assert _flag(tm, 10, cap=3) is FlagOutcome.OWED
    # Seen from tomorrow, today's items are outside the window.
    tomorrow = datetime.now(UTC) + timedelta(hours=1)
    assert _flag(tm, 11, cap=3, since=tomorrow) is FlagOutcome.CREATED


def test_fr511_a_cap_of_zero_queues_nothing_but_records_everything(tm: Any) -> None:
    assert _flag(tm, 1, cap=0) is FlagOutcome.OWED
    assert tm.pending_review() == 0 and tm.owed() == 1


def test_fr511_pending_review_counts_per_site_and_overall(tm: Any) -> None:
    _flag(tm, 1, site="portal")
    _flag(tm, 2, site="portal")
    _flag(tm, 3, site="health")
    assert tm.pending_review("portal") == 2
    assert tm.pending_review("health") == 1
    assert tm.pending_review() == 3
    assert tm.pending_review("nowhere") == 0


# --- the store: flag and translation are stored together ---


def _keys(n: int) -> SegmentKeys:
    return SegmentKeys(_seg(n), f"a{n:063d}", f"m{n:063d}", GFP)


def _record(store: TranslationStore, n: int, review: ReviewRequest | None) -> Any:
    return store.record_machine(
        keys=_keys(n),
        masked_source=f"Pay ⟦CUR:1⟧ for item {n}",
        masked_target="DZ: ⟦CUR:1⟧",
        model_version="nllb-test-1",
        term_ids=(),
        tag_integrity=True,
        review=review,
    )


REQUEST = ReviewRequest("portal", RaisedBy.REQUEST)
OPERATOR = ReviewRequest("portal", RaisedBy.OPERATOR)


def _store(tm: Any, cap: int = 500, **kw: Any) -> TranslationStore:
    return TranslationStore(
        tm,
        ResilientCache(InMemoryCache()),
        InMemorySeenCounter(),
        StoreSettings(review_items_per_site_per_day=cap),
        **kw,
    )


def test_fr511_storing_for_tier2_flags_and_counts_the_outcome(tm: Any) -> None:
    store = _store(tm, cap=2)
    for n in range(3):
        _record(store, n, REQUEST)
    _record(store, 0, REQUEST)  # the same segment again

    assert store.review_flags == {"created": 2, "owed": 1, "exists": 1}
    assert tm.pending_review("portal") == 2 and tm.owed("portal") == 1


def test_fr511_the_store_applies_the_cap_to_requests_only(tm: Any) -> None:
    store = _store(tm, cap=1)
    _record(store, 100, REQUEST)  # page views have used the site's cap up
    _record(store, 101, REQUEST)
    for n in range(4):
        _record(store, n, OPERATOR)  # a pre-warm must still be flagged in full
    assert store.review_flags == {"created": 5, "owed": 1}
    assert tm.pending_review("portal") == 5


def test_fr511_storing_without_a_review_request_flags_nothing(tm: Any) -> None:
    """Tier 3 output is stored and served without joining the review queue."""
    store = _store(tm)
    _record(store, 1, None)
    assert tm.review_item(_seg(1)) is None and store.review_flags == {}


def test_fr511_an_owed_translation_is_still_stored_and_served(tm: Any) -> None:
    store = _store(tm, cap=0)
    _record(store, 1, REQUEST)
    assert store.lookup([LookupItem(_keys(1), 2)])[0] is not None
    assert tm.review_item(_seg(1)).state is ReviewState.OWED


def test_fr510_flagged_machine_output_is_never_served_at_tier1(tm: Any) -> None:
    store = _store(tm)
    _record(store, 1, REQUEST)
    assert store.lookup([LookupItem(_keys(1), 2)])[0] is not None
    assert store.lookup([LookupItem(_keys(1), 1)]) == [None]


class _FlagFails(InMemoryTM):
    def flag_for_review(self, **_: Any) -> FlagOutcome:
        raise ConnectionError("review queue unavailable")


def test_fr511_the_cache_is_untouched_when_flagging_fails() -> None:
    cache = InMemoryCache()
    store = TranslationStore(_FlagFails(), ResilientCache(cache), InMemorySeenCounter())
    with pytest.raises(ConnectionError):
        _record(store, 1, REQUEST)
    assert cache.data == {}


@pytest.mark.integration
def test_fr511_a_translation_that_cannot_be_flagged_is_not_stored(pg_conn: Any) -> None:
    """One transaction: Tier 2 output exists unflagged only where the cap said so."""
    from orchestrator.store.postgres_tm import PostgresTM

    class FlagFails(PostgresTM):
        def flag_for_review(self, **_: Any) -> FlagOutcome:
            raise ConnectionError("review queue unavailable")

    tm = FlagFails(pg_conn)
    store = _store(tm, atomic=pg_conn.transaction)
    with pytest.raises(ConnectionError):
        _record(store, 1, REQUEST)
    assert tm.history(_seg(1)) == []
    assert store.lookup([LookupItem(_keys(1), 2)]) == [None]
