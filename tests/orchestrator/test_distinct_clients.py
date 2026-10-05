"""The distinct-clients rule (S3.4). Requirements: NFR-304, NFR-303.

Entity masking keeps numbers and IDs away from the model. It does nothing for
a name or an address, and a citizen-services page can show both. The rule
that protects those is simple: text only one person has been shown stays in
that person's request. It is not sent to the model, not queued, not stored.

The interesting failures are the quiet ones, where one citizen is counted as
several. These tests look for them.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import ClientHasher
from orchestrator.store.cache import SEEN_NS, InMemorySeenCounter, RedisSeenCounter, day_of
from orchestrator.store.lookup import StoreSettings
from orchestrator.testing.rig import ORIGIN, Rig, body, make_client, make_rig

# Person-shaped but invented, and not assignable: distinctive tokens to look
# for, never a real citizen (docs/CLAUDE.md).
ONE_OFF = "Welcome back, Testperson Examplename of Nowhereton"
CITIZEN = "203.0.113.10"


class FakeDay:
    def __init__(self) -> None:
        self.day = date(2026, 9, 29)

    def __call__(self) -> date:
        return self.day

    def tomorrow(self) -> None:
        self.day += timedelta(days=1)


def _post(client: TestClient, text: str = ONE_OFF) -> dict[str, Any]:
    response = client.post("/v1/translate", json=body(text), headers={"Origin": ORIGIN})
    assert response.status_code == 200
    (segment,) = response.json()["segments"]
    return segment


def _nothing_left_the_request(rig: Rig, segment_key: str) -> None:
    assert rig.translator.calls == 0, "the text reached the model"
    assert rig.queue.depth() == 0 and rig.queue.rows == {}, "the text was queued"
    assert rig.tm.history(segment_key) == [], "the text was stored"
    assert rig.tm._segments == {}, "the source text was stored"
    assert rig.cache.data == {}, "the text was cached"
    for word in ("Testperson", "Examplename", "Nowhereton"):
        assert word not in repr(rig.seen.seen), f"{word!r} is in the counter"


# --- a one-off string ---


def test_nfr304_a_one_off_string_never_reaches_the_model() -> None:
    rig = make_rig(clients_to_persist=3)
    citizen = make_client(rig, client_host=CITIZEN)
    for _ in range(10):  # reloading the page is still one person
        segment = _post(citizen)
        assert segment["status"] == "pending_mt"
        assert segment["text"] == ONE_OFF  # the page stays English (NFR-410)

    _nothing_left_the_request(rig, segment["segment_key"])


def test_nfr304_two_people_are_still_not_enough() -> None:
    """A shared household computer and a phone: two clients, one family's page."""
    rig = make_rig(clients_to_persist=3)
    _post(make_client(rig, client_host=CITIZEN))
    segment = _post(make_client(rig, client_host="203.0.113.11"))

    assert segment["status"] == "pending_mt"
    _nothing_left_the_request(rig, segment["segment_key"])


def test_nfr304_one_ipv6_household_is_one_client() -> None:
    """Privacy addresses rotate inside a /64; each new one is not a new citizen."""
    rig = make_rig(clients_to_persist=3)
    hasher = ClientHasher()  # one API process
    for host in ("2001:db8:1:2::1", "2001:db8:1:2::abcd", "2001:db8:1:2:ffff::9"):
        segment = _post(make_client(rig, client_host=host, hasher=hasher))
    assert segment["status"] == "pending_mt"
    _nothing_left_the_request(rig, segment["segment_key"])


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP, see TODOS.md 'Client-hash salt is per process': each API process "
    "draws its own salt, so one citizen served by three replicas (or across three "
    "restarts in a day) is counted as three clients",
)
def test_nfr304_one_citizen_served_by_several_processes_is_one_client() -> None:
    rig = make_rig(clients_to_persist=3)
    for _ in range(3):  # three replicas, or one process restarted three times
        replica = make_client(rig, client_host=CITIZEN, hasher=ClientHasher())
        segment = _post(replica)
    assert segment["status"] == "pending_mt"
    _nothing_left_the_request(rig, segment["segment_key"])


def test_nfr304_the_threshold_is_three_unless_someone_decides_otherwise() -> None:
    """Production wiring builds the store with these defaults."""
    assert StoreSettings().distinct_clients_to_persist == 3


def test_nfr304_text_many_people_see_is_translated() -> None:
    """The rule must not stop the service doing its job."""
    rig = make_rig(clients_to_persist=3)
    statuses = [
        _post(make_client(rig, client_host=f"203.0.113.{n}"), "Apply online today")["status"]
        for n in (1, 2, 3)
    ]
    assert statuses == ["pending_mt", "pending_mt", "translated"]


# --- one citizen must not be counted as several ---


def test_nfr304_coming_back_on_later_days_does_not_make_one_citizen_many() -> None:
    """The salt rotates daily, so tomorrow the same citizen hashes differently.

    A count that outlived the salt would reach three on the third morning and
    send this citizen's page to the model. Regression: the counter used to
    keep one set per segment and refresh its expiry on every sighting.
    """
    today = FakeDay()
    rig = make_rig(clients_to_persist=3, today=today)
    citizen = make_client(rig, client_host=CITIZEN, hasher=ClientHasher(today=today))

    for _ in range(7):  # a week of checking the same application
        segment = _post(citizen)
        assert segment["status"] == "pending_mt"
        today.tomorrow()

    _nothing_left_the_request(rig, segment["segment_key"])


def test_nfr304_sightings_on_different_days_do_not_add_up() -> None:
    """Hashes from different days cannot be compared, so they are not summed."""
    today = FakeDay()
    seen = InMemorySeenCounter(today)
    assert seen.observe("s1", "a") == 1
    assert seen.observe("s1", "b") == 2
    today.tomorrow()
    assert seen.observe("s1", "c") == 1
    assert list(seen.seen) == ["s1"]  # yesterday's hashes are gone, not kept


@pytest.mark.integration
def test_nfr304_redis_counts_do_not_outlive_the_day(redis_client: Any) -> None:
    today = FakeDay()
    seen = RedisSeenCounter(redis_client, ttl_seconds=60, today=today)
    assert [seen.observe("s1", c) for c in ("a", "a", "b")] == [1, 1, 2]
    today.tomorrow()
    assert seen.observe("s1", "c") == 1

    keys = sorted(k.decode() for k in redis_client.keys(f"{SEEN_NS}*"))
    assert keys == [f"{SEEN_NS}2026-09-29:s1", f"{SEEN_NS}2026-09-30:s1"]
    assert all(0 < redis_client.ttl(k) <= 60 for k in keys)  # and they expire


# --- the client hash ---


def test_nfr304_the_client_hash_changes_every_day() -> None:
    today = FakeDay()
    hasher = ClientHasher(today=today)
    first = hasher.hash(CITIZEN)
    assert hasher.hash(CITIZEN) == first  # stable within the day
    today.tomorrow()
    assert hasher.hash(CITIZEN) != first


def test_nfr303_the_client_hash_cannot_be_recomputed_from_the_address() -> None:
    """An unsalted hash of an IPv4 address is reversible by trying all of them."""
    hasher = ClientHasher()
    hashed = hasher.hash(CITIZEN)
    assert CITIZEN not in hashed
    assert hashed != hashlib.sha256(CITIZEN.encode()).hexdigest()
    assert ClientHasher().hash(CITIZEN) != hashed  # another process, another salt


def test_nfr303_what_the_counter_holds_is_not_an_address() -> None:
    rig = make_rig(clients_to_persist=3)
    _post(make_client(rig, client_host=CITIZEN), "Apply online today")
    assert CITIZEN not in repr(rig.seen.seen)


def test_nfr304_a_request_that_crosses_midnight_is_counted_on_one_day() -> None:
    """Regression: the hash was made with one day's salt and counted in the next day's set.

    The same citizen's next request, hashed with the new salt, then counted as
    a second distinct client.
    """
    today = FakeDay()
    seen = InMemorySeenCounter(today)
    hasher = ClientHasher(today=today)

    before_midnight = hasher.hash(CITIZEN)
    today.tomorrow()  # the clock turns while the request is being served
    assert seen.observe("s1", before_midnight) == 1
    assert seen.observe("s1", hasher.hash(CITIZEN)) == 1  # a new day, a new count
    assert list(seen.seen) == ["s1"]


def test_nfr304_the_hash_names_its_day_and_nothing_else() -> None:
    today = FakeDay()
    hashed = ClientHasher(today=today).hash(CITIZEN)
    day, _, digest = hashed.partition(":")
    assert day == "2026-09-29"
    assert len(digest) == 64 and CITIZEN not in hashed
    assert day_of(hashed) == today.day
    assert day_of("not-a-dated-hash", today) == today.day
