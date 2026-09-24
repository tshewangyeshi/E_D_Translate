"""Enrolled sites (FR-601) and tier authority (FR-500, FR-512).

The server decides the effective tier: the strictest of the site default, the
site's path rules, the matched configured selector and the request's hint. A
request can make content stricter, never looser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from orchestrator.governance.paths import normalise_path, path_matches


class SiteConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PathRule:
    pattern: str  # "/legal/notice" or "/legal/*"
    tier: int


@dataclass(frozen=True)
class Site:
    site_id: str
    origins: frozenset[str]  # exact scheme://host[:port], no trailing slash
    default_tier: int
    tier1_selectors: tuple[str, ...] = ()
    private_selectors: tuple[str, ...] = ()
    path_rules: tuple[PathRule, ...] = ()
    enabled: bool = True

    def tier_for_path(self, path: object) -> int | None:
        """Strictest matching path rule, or None when no rule applies.

        An unusable path is Tier 1, not "no match": a path we cannot normalise
        is one we cannot prove is outside a Tier 1 rule.
        """
        if not self.path_rules:
            return None
        normalised = normalise_path(path)
        if normalised is None:
            return 1
        matched = [r.tier for r in self.path_rules if path_matches(r.pattern, normalised)]
        return min(matched) if matched else None


class SiteRegistry:
    def __init__(self, sites: list[Site]) -> None:
        self._by_id = {s.site_id: s for s in sites}
        if len(self._by_id) != len(sites):
            raise SiteConfigError("duplicate site_id")

    @staticmethod
    def load(path: Path) -> SiteRegistry:
        data = json.loads(path.read_text(encoding="utf-8"))
        sites = []
        for raw in data.get("sites", []):
            tier = raw.get("default_tier", 1)
            if tier not in (1, 2, 3) or isinstance(tier, bool):
                raise SiteConfigError(f"{raw.get('site_id')}: default_tier must be 1, 2 or 3")
            origins = frozenset(o.rstrip("/") for o in raw["origins"])
            if not origins or not all(o.startswith(("https://", "http://")) for o in origins):
                raise SiteConfigError(f"{raw['site_id']}: origins must be http(s) URLs")
            sites.append(
                Site(
                    site_id=raw["site_id"],
                    origins=origins,
                    default_tier=tier,
                    tier1_selectors=tuple(raw.get("tier1_selectors", ())),
                    private_selectors=tuple(raw.get("private_selectors", ())),
                    path_rules=_path_rules(raw),
                    enabled=bool(raw.get("enabled", True)),
                )
            )
        return SiteRegistry(sites)

    def get(self, site_id: str) -> Site | None:
        site = self._by_id.get(site_id)
        return site if site is not None and site.enabled else None

    def origin_enrolled(self, origin: str | None) -> bool:
        """True if any enabled site lists this origin (CORS preflight has no body)."""
        if origin is None:
            return False
        return any(s.enabled and origin.rstrip("/") in s.origins for s in self._by_id.values())

    def allows(self, site_id: str, origin: str | None) -> Site | None:
        """The enrolled, enabled site if ``origin`` is one of its origins (NFR-301), else None."""
        site = self.get(site_id)
        if site is None or origin is None:
            return None
        return site if origin.rstrip("/") in site.origins else None


def _path_rules(raw: dict[str, object]) -> tuple[PathRule, ...]:
    """Parse and validate ``path_rules``. A rule the operator cannot rely on is refused."""
    site_id = raw.get("site_id")
    rules = raw.get("path_rules", [])
    if not isinstance(rules, list):
        raise SiteConfigError(f"{site_id}: path_rules must be a list")
    out = []
    for entry in rules:
        if not isinstance(entry, dict):
            raise SiteConfigError(f"{site_id}: each path rule must be an object")
        pattern, tier = entry.get("path"), entry.get("tier")
        if not isinstance(pattern, str) or not pattern.startswith("/"):
            raise SiteConfigError(f"{site_id}: path rule 'path' must start with '/'")
        if tier not in (1, 2, 3) or isinstance(tier, bool):
            raise SiteConfigError(f"{site_id}: path rule tier must be 1, 2 or 3")
        # Rules are matched against already-normalised paths, so a pattern that
        # is not itself in normal form ("/legal/../*", "/legal/") can never match.
        # Refuse it at load: silently keeping it would leave the operator
        # believing a Tier 1 rule is in force when nothing enforces it.
        probe = pattern.replace("*", "x")
        if normalise_path(probe) != probe.lower():
            raise SiteConfigError(
                f"{site_id}: path rule {pattern!r} is not in normal form and would never match"
            )
        out.append(PathRule(pattern, tier))
    return tuple(out)


def _valid_tier(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value in (1, 2, 3):
        return value
    return None


def resolve_tier(
    site: Site | None,
    request_tier: object,
    selector_tier: object,
    path: object = None,
) -> int:
    """Strictest (lowest-numbered) of site default, path rule, selector and hint (FR-512).

    A request can make content stricter, never looser; an unknown site is Tier 1.
    An invalid hint is ignored rather than honoured, so garbage cannot loosen a
    tier; the site's own configuration still applies.
    """
    if site is None:
        return 1
    candidates = [
        site.default_tier,
        site.tier_for_path(path),
        _valid_tier(selector_tier),
        _valid_tier(request_tier),
    ]
    return min(t for t in candidates if t is not None)
