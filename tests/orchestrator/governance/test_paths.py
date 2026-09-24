"""Path normalisation for tier rules (FR-500, FR-512).

The request supplies the path, so these are adversarial tests: each one asks
whether a caller can make Tier 1 text look like it came from somewhere else.
"""

from __future__ import annotations

import pytest

from orchestrator.governance.paths import MAX_PATH_LENGTH, normalise_path, path_matches


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/legal/notice", "/legal/notice"),
        ("legal/notice", "/legal/notice"),  # leading slash supplied
        ("/legal//notice", "/legal/notice"),  # empty segments collapse
        ("/legal/./notice", "/legal/notice"),
        ("/legal/notice/", "/legal/notice"),  # trailing slash is not a distinct page
        ("/", "/"),
        ("/legal/notice?print=1", "/legal/notice"),  # query is not part of the path
        ("/legal/notice#section-2", "/legal/notice"),
        ("/LEGAL/Notice", "/legal/notice"),  # folded: see the module docstring
        ("/legal%2Fnotice", "/legal/notice"),  # encoded separator decodes first
        ("/services/%E0%BD%80", "/services/ཀ"),  # Tibetan in a URL is legitimate
    ],
)
def test_fr500_normalise_path_accepts_unambiguous_paths(raw: str, expected: str) -> None:
    assert normalise_path(raw) == expected


def test_fr512_traversal_resolves_to_the_page_the_server_would_serve() -> None:
    # The site serves this as /public, so /public is the tier that applies.
    assert normalise_path("/legal/../public") == "/public"
    assert normalise_path("/legal/%2e%2e/public") == "/public"


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("/../etc/passwd", "escapes the root: the site would not serve it"),
        ("/legal/../../x", "escapes the root"),
        ("/legal/%252e%252e/public", "double encoded: intent unknowable"),
        ("/legal%2%2f", "invalid percent escape"),
        ("/legal\\notice", "backslash is a separator on some stacks only"),
        ("/legal\x00/notice", "NUL"),
        ("/legal\nnotice", "control character"),
        ("x" * (MAX_PATH_LENGTH + 1), "over length"),
        ("", "empty"),
        (None, "not a string"),
        (12, "not a string"),
    ],
)
def test_fr512_untrustworthy_paths_are_refused(raw: object, why: str) -> None:
    assert normalise_path(raw) is None, why


def test_fr512_percent_encoding_cannot_hide_a_tier1_prefix() -> None:
    """The evasion this exists to stop: dressing up /legal so the rule misses it."""
    rule = "/legal/*"
    for attempt in ("/%6c%65%67%61%6c/notice", "/LEGAL/notice", "/legal//notice", "/legal/./x"):
        normalised = normalise_path(attempt)
        assert normalised is not None
        assert path_matches(rule, normalised), attempt


class TestPathMatches:
    def test_exact(self) -> None:
        assert path_matches("/legal/notice", "/legal/notice")
        assert not path_matches("/legal/notice", "/legal/notice/extra")

    def test_prefix_covers_the_prefix_itself(self) -> None:
        assert path_matches("/legal/*", "/legal")
        assert path_matches("/legal/*", "/legal/notice")
        assert path_matches("/legal/*", "/legal/deep/page")

    def test_prefix_does_not_leak_to_a_sibling(self) -> None:
        assert not path_matches("/legal/*", "/legalese")
        assert not path_matches("/legal/*", "/public")

    def test_root_wildcard_covers_everything(self) -> None:
        assert path_matches("/*", "/anything/at/all")
