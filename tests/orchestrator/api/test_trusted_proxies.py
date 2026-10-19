"""Who the client is behind GovTech's gateway. Requirements: FR-103, NFR-304.

Behind a proxy every citizen arrives from the proxy's address. Read naively,
that makes the whole country one client: the distinct-clients rule never
reaches three, so nothing is ever translated, and one busy reader throttles
everyone. Read carelessly, X-Forwarded-For lets any caller pick its own
identity and walk away from its rate limit. These tests hold both lines.
"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.api.ratelimit import RateLimiter, client_address, parse_trusted_proxies
from orchestrator.testing.rig import ORIGIN, body, make_client, make_rig
from orchestrator.wiring import ConfigError, Settings

GATEWAY = "172.30.10.137"
TRUSTED = parse_trusted_proxies(f"{GATEWAY}, 127.0.0.1")
CITIZEN = "203.0.113.10"


# --- reading the chain ---


def test_fr103_without_trusted_proxies_the_peer_is_the_client() -> None:
    assert client_address(GATEWAY, [CITIZEN], ()) == GATEWAY


def test_fr103_a_header_from_an_untrusted_peer_is_ignored() -> None:
    """A caller cannot choose its identity by sending the header itself."""
    assert client_address(CITIZEN, ["198.51.100.7"], TRUSTED) == CITIZEN


def test_fr103_behind_the_gateway_the_reported_address_is_the_client() -> None:
    assert client_address(GATEWAY, [CITIZEN], TRUSTED) == CITIZEN


def test_fr103_the_chain_is_read_from_the_right_through_trusted_hops_only() -> None:
    # The citizen sent a forged entry; the gateway appended the real address,
    # and a local reverse proxy appended the gateway's.
    chain = ["198.51.100.7", f"{CITIZEN}, {GATEWAY}"]
    assert client_address("127.0.0.1", chain, TRUSTED) == CITIZEN


def test_fr103_an_unreadable_entry_falls_back_to_the_last_trusted_hop() -> None:
    assert client_address(GATEWAY, ["not-an-address"], TRUSTED) == GATEWAY


def test_fr103_a_chain_of_trusted_hops_only_gives_the_first_hop() -> None:
    assert client_address("127.0.0.1", [GATEWAY], TRUSTED) == GATEWAY


def test_fr103_no_header_behind_the_gateway_gives_the_gateway() -> None:
    assert client_address(GATEWAY, [], TRUSTED) == GATEWAY


def test_fr103_networks_and_ipv6_are_accepted() -> None:
    trusted = parse_trusted_proxies("10.0.0.0/8, ::1")
    assert client_address("10.1.2.3", [CITIZEN], trusted) == CITIZEN
    assert client_address("::1", ["2001:db8::5"], trusted) == "2001:db8::5"


@pytest.mark.parametrize("spec", ["gateway.example", "10.0.0.0/33", "1.2.3"])
def test_fr103_an_unusable_proxy_setting_stops_the_start(spec: str) -> None:
    env = {
        "DZWEB_PG_DSN": "postgresql://x",
        "DZWEB_REDIS_URL": "redis://x",
        "DZWEB_TERMBASE": "t.json",
        "DZWEB_SITES": "s.json",
        "DZWEB_TRUSTED_PROXIES": spec,
    }
    with pytest.raises(ConfigError, match="DZWEB_TRUSTED_PROXIES"):
        Settings.from_env(env)


def test_fr103_the_setting_is_optional() -> None:
    env = {
        "DZWEB_PG_DSN": "postgresql://x",
        "DZWEB_REDIS_URL": "redis://x",
        "DZWEB_TERMBASE": "t.json",
        "DZWEB_SITES": "s.json",
    }
    assert Settings.from_env(env).trusted_proxies == ()


# --- through the API ---


def _post(client: Any, forwarded: str | None) -> Any:
    headers = {"Origin": ORIGIN}
    if forwarded is not None:
        headers["X-Forwarded-For"] = forwarded
    return client.post("/v1/translate", json=body("Apply online today"), headers=headers)


def test_nfr304_three_citizens_behind_the_gateway_are_three_clients() -> None:
    rig = make_rig(clients_to_persist=3)
    client = make_client(rig, client_host=GATEWAY, trusted_proxies=TRUSTED)
    statuses = [_post(client, f"203.0.113.{n}").json()["segments"][0]["status"] for n in (1, 2, 3)]
    assert statuses == ["pending_mt", "pending_mt", "translated"]


def test_nfr304_one_caller_forging_the_header_stays_one_client() -> None:
    rig = make_rig(clients_to_persist=3)
    client = make_client(rig, client_host=CITIZEN, trusted_proxies=TRUSTED)
    statuses = [_post(client, f"198.51.100.{n}").json()["segments"][0]["status"] for n in (1, 2, 3)]
    assert statuses == ["pending_mt"] * 3
    assert rig.translator.calls == 0


def test_fr103_each_citizen_behind_the_gateway_has_their_own_limit() -> None:
    rig = make_rig()
    client = make_client(
        rig,
        client_host=GATEWAY,
        trusted_proxies=TRUSTED,
        client_limiter=RateLimiter(per_minute=1, burst=1),
    )
    assert _post(client, "203.0.113.1").status_code == 200
    assert _post(client, "203.0.113.1").status_code == 429
    assert _post(client, "203.0.113.2").status_code == 200


def test_fr103_forging_the_header_does_not_escape_the_limit() -> None:
    rig = make_rig()
    client = make_client(
        rig,
        client_host=CITIZEN,
        trusted_proxies=TRUSTED,
        client_limiter=RateLimiter(per_minute=1, burst=1),
    )
    assert _post(client, "198.51.100.1").status_code == 200
    assert _post(client, "198.51.100.2").status_code == 429
