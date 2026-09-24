"""The tier gate over HTTP (FR-510, FR-512).

`test_tiering.py` proves the resolution rule. This proves the rule is actually
reached from a real request, which is the part that silently rots: the path
travelled from the request body to `resolve_tier` through three call sites.
"""

from __future__ import annotations

from orchestrator.testing.rig import ORIGIN, body, make_client, make_rig

HEADERS = {"Origin": ORIGIN}
# The rig's "portal" site is Tier 2 by default with a Tier 1 rule on /legal/*.


def test_fr510_tier1_path_is_not_machine_translated() -> None:
    """The S3.1 acceptance criterion, end to end."""
    client = make_client(make_rig())
    response = client.post(
        "/v1/translate",
        json=body("Fees are non-refundable.", path="/legal/notice"),
        headers=HEADERS,
    )
    assert response.status_code == 200
    segment = response.json()["segments"][0]
    assert segment["status"] == "tier_blocked"
    assert segment["text"] == "Fees are non-refundable."  # English, unchanged


def test_fr512_a_tier2_claim_cannot_unlock_a_tier1_path() -> None:
    client = make_client(make_rig())
    response = client.post(
        "/v1/translate",
        json=body("Fees are non-refundable.", path="/legal/notice", tier=2),
        headers=HEADERS,
    )
    assert response.json()["segments"][0]["status"] == "tier_blocked"


def test_fr512_encoded_and_traversal_paths_cannot_escape_the_rule() -> None:
    client = make_client(make_rig())
    for path in (
        "/LEGAL/notice",
        "/legal//notice",
        "/legal/./notice",
        "/%6c%65%67%61%6c/notice",
        "/legal/notice?print=1",
        "/legal/%252e%252e/elsewhere",  # refused outright, so Tier 1
    ):
        response = client.post(
            "/v1/translate",
            json=body("Fees are non-refundable.", path=path, tier=2),
            headers=HEADERS,
        )
        assert response.json()["segments"][0]["status"] == "tier_blocked", path


def test_fr512_a_tier2_path_on_the_same_site_still_translates() -> None:
    """The gate must block Tier 1, not everything: a real regression risk."""
    client = make_client(make_rig())
    response = client.post(
        "/v1/translate",
        json=body("Apply for a passport.", path="/services/renewal"),
        headers=HEADERS,
    )
    segment = response.json()["segments"][0]
    assert segment["status"] == "translated"
    assert segment["text"].startswith("DZ:")


def test_fr512_traversal_out_of_legal_is_tiered_as_the_page_served() -> None:
    """/legal/../services is served as /services, so it is Tier 2."""
    client = make_client(make_rig())
    response = client.post(
        "/v1/translate",
        json=body("Apply for a passport.", path="/legal/../services/renewal"),
        headers=HEADERS,
    )
    assert response.json()["segments"][0]["status"] == "translated"
