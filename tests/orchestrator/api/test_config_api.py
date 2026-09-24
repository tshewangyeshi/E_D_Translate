"""GET /v1/config (FR-216, FR-512).

The widget fetches this before offering a toggle. Two things matter: enrolled
callers get the selectors they need, and the response never hands out anything
that would let a caller reason around the server's tier decision.
"""

from __future__ import annotations

from orchestrator.testing.rig import LEGAL_ORIGIN, ORIGIN, make_client, make_rig

HEADERS = {"Origin": ORIGIN}


def test_fr216_config_returns_the_selectors_the_widget_needs() -> None:
    client = make_client(make_rig())
    response = client.get("/v1/config?site=portal", headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["site"] == "portal"
    assert data["default_tier"] == 2
    assert data["tier1_selectors"] == [".fees"]
    assert data["max_segments"] == 64


def test_fr512_config_does_not_disclose_server_side_path_rules() -> None:
    """Tier resolution stays on the server; the widget gets selectors, not rules.

    Asserted as an exact key set, so a field added later has to be considered
    here rather than shipping by accident.
    """
    client = make_client(make_rig())
    data = client.get("/v1/config?site=portal", headers=HEADERS).json()
    assert set(data) == {
        "site",
        "default_tier",
        "tier1_selectors",
        "private_selectors",
        "glossary_version",
        "max_segments",
    }


def test_nfr301_config_refuses_an_unenrolled_origin() -> None:
    client = make_client(make_rig())
    response = client.get(
        "/v1/config?site=portal", headers={"Origin": "https://not-enrolled.example"}
    )
    assert response.status_code == 403


def test_nfr301_config_refuses_an_origin_enrolled_for_a_different_site() -> None:
    """Being enrolled somewhere is not being enrolled here."""
    client = make_client(make_rig())
    response = client.get("/v1/config?site=portal", headers={"Origin": LEGAL_ORIGIN})
    assert response.status_code == 403


def test_nfr301_config_refuses_an_unknown_site() -> None:
    client = make_client(make_rig())
    assert client.get("/v1/config?site=nonesuch", headers=HEADERS).status_code == 403
    assert client.get("/v1/config", headers=HEADERS).status_code == 403


def test_fr102_config_revalidates_with_an_etag() -> None:
    client = make_client(make_rig())
    first = client.get("/v1/config?site=portal", headers=HEADERS)
    etag = first.headers["ETag"]
    again = client.get("/v1/config?site=portal", headers={**HEADERS, "If-None-Match": etag})
    assert again.status_code == 304


def test_nfr301_config_preflight_allows_an_enrolled_origin_only() -> None:
    client = make_client(make_rig())
    ok = client.options("/v1/config", headers={**HEADERS, "Access-Control-Request-Method": "GET"})
    assert ok.status_code == 204
    assert ok.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "GET" in ok.headers["Access-Control-Allow-Methods"]

    blocked = client.options("/v1/config", headers={"Origin": "https://not-enrolled.example"})
    assert blocked.status_code == 403
