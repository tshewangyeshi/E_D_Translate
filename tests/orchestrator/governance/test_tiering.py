"""Tier authority: the server decides (FR-500, FR-510, FR-511, FR-512).

The rule under test is one sentence: the effective tier is the strictest of the
site default, the site's path rules, the matched selector and the request's
hint. Every test here is a way someone might try to get a looser one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.governance.sites import (
    PathRule,
    Site,
    SiteConfigError,
    SiteRegistry,
    resolve_tier,
)

ORIGIN = "https://portal.gov.example"


def site(**kw: object) -> Site:
    base: dict[str, object] = {
        "site_id": "portal",
        "origins": frozenset({ORIGIN}),
        "default_tier": 2,
    }
    return Site(**{**base, **kw})  # type: ignore[arg-type]


class TestResolveTier:
    def test_fr512_site_default_applies_when_nothing_else_matches(self) -> None:
        assert resolve_tier(site(), None, None, "/services/renewal") == 2

    def test_fr512_unknown_site_is_tier1(self) -> None:
        assert resolve_tier(None, 3, 3, "/anything") == 1

    def test_fr512_path_rule_makes_content_stricter(self) -> None:
        s = site(path_rules=(PathRule("/legal/*", 1),))
        assert resolve_tier(s, None, None, "/legal/notice") == 1

    def test_fr512_path_rule_cannot_loosen_the_site_default(self) -> None:
        s = site(default_tier=1, path_rules=(PathRule("/public/*", 3),))
        assert resolve_tier(s, None, None, "/public/news") == 1

    def test_fr512_request_hint_cannot_loosen_a_tier1_path(self) -> None:
        """The acceptance criterion: a request claiming Tier 2 on a Tier 1 path."""
        s = site(path_rules=(PathRule("/legal/*", 1),))
        assert resolve_tier(s, 2, 2, "/legal/notice") == 1
        assert resolve_tier(s, 3, 3, "/legal/notice") == 1

    def test_fr512_request_hint_may_make_content_stricter(self) -> None:
        assert resolve_tier(site(), 1, None, "/services/renewal") == 1

    def test_fr512_strictest_of_several_matching_path_rules_wins(self) -> None:
        s = site(path_rules=(PathRule("/legal/*", 3), PathRule("/legal/notice", 1)))
        assert resolve_tier(s, None, None, "/legal/notice") == 1

    def test_fr512_unusable_path_is_tier1_when_the_site_has_path_rules(self) -> None:
        s = site(path_rules=(PathRule("/legal/*", 1),))
        assert resolve_tier(s, 2, None, "/../escape") == 1
        assert resolve_tier(s, 2, None, None) == 1

    def test_fr512_path_is_ignored_when_the_site_configured_no_path_rules(self) -> None:
        # No rules means no path opinion; a missing path must not force Tier 1
        # on every site that has not configured any.
        assert resolve_tier(site(), None, None, None) == 2
        assert resolve_tier(site(), None, None, "/../escape") == 2

    @pytest.mark.parametrize("bad", [0, 4, -1, True, 2.0, "2", None, [2]])
    def test_fr500_an_invalid_hint_is_ignored_not_honoured(self, bad: object) -> None:
        # Ignored, so garbage cannot loosen; the site's own config still applies.
        assert resolve_tier(site(), bad, None, "/x") == 2

    def test_fr512_selector_still_applies_alongside_path_rules(self) -> None:
        s = site(path_rules=(PathRule("/public/*", 3),))
        assert resolve_tier(s, None, 1, "/public/news") == 1


class TestSiteConfigLoading:
    def write(self, tmp_path: Path, raw: object) -> Path:
        p = tmp_path / "sites.json"
        p.write_text(json.dumps({"sites": [raw]}), encoding="utf-8")
        return p

    def valid(self, **extra: object) -> dict[str, object]:
        return {"site_id": "portal", "origins": [ORIGIN], "default_tier": 2, **extra}

    def test_fr601_path_rules_load(self, tmp_path: Path) -> None:
        p = self.write(tmp_path, self.valid(path_rules=[{"path": "/legal/*", "tier": 1}]))
        loaded = SiteRegistry.load(p).get("portal")
        assert loaded is not None
        assert loaded.path_rules == (PathRule("/legal/*", 1),)

    @pytest.mark.parametrize(
        "rules",
        [
            [{"path": "legal/*", "tier": 1}],  # no leading slash
            [{"path": "/legal/*", "tier": 0}],
            [{"path": "/legal/*", "tier": 4}],
            [{"path": "/legal/*", "tier": True}],
            [{"path": "/legal/*"}],  # no tier
            [{"tier": 1}],  # no path
            [{"path": "/legal/../*", "tier": 1}],  # would never match once normalised
            ["/legal/*"],  # not an object
            "everything",  # not a list
        ],
    )
    def test_fr601_an_unusable_path_rule_is_refused_at_load(
        self, tmp_path: Path, rules: object
    ) -> None:
        """Silently dropping a rule would leave the operator believing Tier 1 is in force."""
        with pytest.raises(SiteConfigError):
            SiteRegistry.load(self.write(tmp_path, self.valid(path_rules=rules)))
