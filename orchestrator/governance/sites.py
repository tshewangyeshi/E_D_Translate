"""Enrolled sites (FR-601) and tier authority (FR-500, FR-512).

The server decides the effective tier. S2.1 covers the site default, the
request's hint and the matched configured selector; server-side path rules
arrive with S3.1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class SiteConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Site:
    site_id: str
    origins: frozenset[str]  # exact scheme://host[:port], no trailing slash
    default_tier: int
    tier1_selectors: tuple[str, ...] = ()
    private_selectors: tuple[str, ...] = ()
    enabled: bool = True


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


def _valid_tier(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value in (1, 2, 3):
        return value
    return None


def resolve_tier(site: Site | None, request_tier: object, selector_tier: object) -> int:
    """Strictest (lowest-numbered) of site default, selector match and request hint (FR-512).

    A request can make content stricter, never looser; an unknown site is Tier 1.
    """
    if site is None:
        return 1
    candidates = [site.default_tier, _valid_tier(selector_tier), _valid_tier(request_tier)]
    return min(t for t in candidates if t is not None)
