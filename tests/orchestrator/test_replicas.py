"""Several API replicas behave as one (S2.1, S3.4). Requirements: FR-103, NFR-303, NFR-304.

With one process, in-memory rate limits and a process-local salt were
enough. With several, a client could spend the full limit once per replica,
and one citizen could look like a different client to each replica, so text
only that citizen saw could pass the N-distinct-clients rule. Here: limits
live in Redis under salted hashes, and every replica derives the same daily
salt from one configured key, which is never stored.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from orchestrator.api.app import ClientHasher, shared_limiters
from orchestrator.api.ratelimit import SharedRateLimiter
from orchestrator.testing.rig import ORIGIN, body, make_client, make_rig
from orchestrator.wiring import Settings
from tests.orchestrator.conftest import requires_redis

KEY = b"test-client-hash-key-not-a-real-one"
CITIZEN = "203.0.113.10"
ONE_OFF = "Welcome back, Testperson Examplename of Nowhereton"


# --- one client hash for every replica (S3.4) ---


def test_nfr304_replicas_with_one_key_hash_a_client_alike() -> None:
    assert ClientHasher(key=KEY).hash(CITIZEN) == ClientHasher(key=KEY).hash(CITIZEN)


def test_nfr304_each_day_has_its_own_hash() -> None:
    def on(day: date) -> str:
        return ClientHasher(today=lambda: day, key=KEY).hash(CITIZEN)

    assert on(date(2026, 10, 5)).split(":")[1] != on(date(2026, 10, 6)).split(":")[1]


def test_nfr304_without_a_key_each_process_salts_on_its_own() -> None:
    assert ClientHasher().hash(CITIZEN) != ClientHasher().hash(CITIZEN)


def test_nfr304_one_citizen_through_two_replicas_is_one_client() -> None:
    """The open half of S3.4: a one-off string must not pass N=3 by visiting replicas."""
    rig = make_rig(clients_to_persist=3)
    replicas = [make_client(rig, hasher=ClientHasher(key=KEY)) for _ in range(3)]
    for replica in replicas * 2:  # six requests, three replicas, one citizen
        response = replica.post("/v1/translate", json=body(ONE_OFF), headers={"Origin": ORIGIN})
        (segment,) = response.json()["segments"]
        assert segment["status"] == "pending_mt", "one citizen counted as several clients"
    assert rig.translator.calls == 0, "the text reached the model"


def test_nfr304_the_failure_this_prevents() -> None:
    """Without the shared key, three replicas count one citizen three times."""
    rig = make_rig(clients_to_persist=3)
    replicas = [make_client(rig, hasher=ClientHasher()) for _ in range(3)]
    statuses = [
        replica.post("/v1/translate", json=body(ONE_OFF), headers={"Origin": ORIGIN}).json()[
            "segments"
        ][0]["status"]
        for replica in replicas
    ]
    assert statuses[-1] == "translated"


# --- rate limits shared through Redis (S2.1) ---


def _limiter(client: Any, burst: float = 3) -> SharedRateLimiter:
    return SharedRateLimiter(
        client, "test", per_minute=60, burst=burst, key_hash=ClientHasher(key=KEY).hash
    )


@pytest.mark.integration
@requires_redis
def test_fr103_two_replicas_spend_one_bucket(redis_client: Any) -> None:
    a, b = _limiter(redis_client), _limiter(redis_client)
    spent = [a.allow(CITIZEN), b.allow(CITIZEN), a.allow(CITIZEN), b.allow(CITIZEN)]
    assert spent == [True, True, True, False]  # burst 3, shared, not 3 per replica
    assert a.allow("198.51.100.7")  # another client has its own bucket


@pytest.mark.integration
@requires_redis
def test_nfr303_no_client_address_is_written_to_redis(redis_client: Any) -> None:
    _limiter(redis_client).allow(CITIZEN)
    keys = [k.decode() for k in redis_client.keys("*")]
    assert keys and all(CITIZEN not in k for k in keys)
    assert all(CITIZEN.encode() not in str(redis_client.hgetall(k)).encode() for k in keys)


@pytest.mark.integration
@requires_redis
def test_fr103_a_bucket_disappears_once_it_would_be_full_again(redis_client: Any) -> None:
    _limiter(redis_client, burst=3).allow(CITIZEN)
    (key,) = redis_client.keys("dzweb:ratelimit:test:*")
    assert 0 < redis_client.ttl(key) <= 4  # 3 tokens at 1 a second, plus one


class BrokenRedis:
    def register_script(self, script: str) -> Any:
        def run(**kwargs: Any) -> Any:
            raise ConnectionError("redis down")

        return run


def test_fr103_redis_down_falls_back_to_this_replicas_own_limit() -> None:
    limiter = _limiter(BrokenRedis(), burst=2)
    assert [limiter.allow(CITIZEN) for _ in range(3)] == [True, True, False]
    assert limiter.failures == 3


def test_fr103_the_api_builds_three_shared_limiters() -> None:
    limiters = shared_limiters(BrokenRedis(), ClientHasher(key=KEY))
    assert set(limiters) == {"origin_limiter", "client_limiter", "feedback_limiter"}
    assert all(isinstance(v, SharedRateLimiter) for v in limiters.values())


def test_nfr303_printed_settings_show_no_secret() -> None:
    settings = Settings.from_env(
        {
            "DZWEB_PG_DSN": "postgresql://dzweb:db-password-value@db/dzweb",
            "DZWEB_REDIS_URL": "redis://127.0.0.1:1/0",
            "DZWEB_TERMBASE": "termbase.json",
            "DZWEB_SITES": "sites.json",
            "DZWEB_OPS_TOKEN": "ops-token-value",
            "DZWEB_CLIENT_HASH_KEY": "hash-key-value",
        }
    )
    shown = repr(settings)
    for secret in ("db-password-value", "ops-token-value", "hash-key-value"):
        assert secret not in shown
